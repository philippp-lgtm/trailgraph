"""Stored files: add a file to a graph (files slice 1; contracts B3, B7.8, B13).

graph_cli stores, addresses and references files; it never reads what is in
them. ``FileService.add``:

1. The input must be a regular file (after symlinks), else ``file.unreadable``.
2. The type is ``files:File`` or a concrete subclass; a model without the
   ``files`` module is ``file.module_missing``.
3. The bytes are streamed once: hashed, counted against ``max_bytes`` and
   staged before the graph lock (a dry-run hashes in memory and stages
   nothing); the media type comes from the first bytes and the name.
4. Known content returns the active file that holds it, with warning
   ``file.already_present`` and without a tx. A concurrent add of the same
   content loses on ``unique_violation`` and returns the winner the same way.
5. The origin is ``{root, path}`` against the configured roots of this
   machine; an absolute path is never recorded. Without a matching root the
   file has no origin and the result warns ``file.no_root``.
6. One ``create`` goes through ``WriteService.apply`` with the staged blob,
   which is published at W.11b. The staged copy is discarded afterwards in
   every case.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Final, Protocol

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, CreateOp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead, StagedBlob
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import check_facets
from graph_cli.services import media_types
from graph_cli.services.models import ModelCache
from graph_cli.services.write import WriteRequest, WriteResult, WriteService

FILE_TYPE: Final = "files:File"
MEDIA_TYPE_DATATYPE: Final = "files:MediaType"
DEFAULT_MAX_BYTES: Final = 100 * 1024 * 1024
CHUNK_BYTES: Final = 1024 * 1024
_ACTIVE: Final = frozenset({"active"})


class Origins(Protocol):
    """Named file roots of this machine (``config.toml [roots]``)."""

    def origin_of(self, path: Path) -> dict[str, JsonValue] | None:
        """``{root, path}`` of an absolute, resolved path; ``None`` outside every root."""
        ...

    def resolve(self, origin: Mapping[str, JsonValue]) -> Path:
        """The origin's path on this machine; ``file.root_unknown`` if its root is not set."""
        ...


class NoOrigins:
    """No roots configured."""

    def origin_of(self, path: Path) -> dict[str, JsonValue] | None:
        return None

    def resolve(self, origin: Mapping[str, JsonValue]) -> Path:
        raise GraphCliError(
            "file.root_unknown",
            f"the file root {origin.get('root')!r} is not configured on this machine",
            details={"root": origin.get("root")},
        )


@dataclass(frozen=True, slots=True)
class FileAddRequest:
    actor: ActorContext
    graph: str  # name or id
    path: Path  # as given; the file name is its last part
    request: RequestMeta
    type: str | None = None
    title: str | None = None
    attrs: Mapping[str, JsonValue] = field(default_factory=dict)
    attrs_text: Mapping[str, str] = field(default_factory=dict)
    media_type: str | None = None
    source_url: str | None = None
    retrieved_at: str | None = None
    no_origin: bool = False
    reason: str | None = None
    idempotency_key: str | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class FileAddResult:
    entry: GraphEntry
    entity: dict[str, JsonValue]  # {id, alias, type, rev}
    file: dict[str, JsonValue]  # {hash, size, media_type, file_name, origin}
    existing: bool
    write: WriteResult | None  # None when the file was already present
    warnings: tuple[WarningRecord, ...] = ()

    @property
    def seq(self) -> int | None:
        return None if self.write is None else self.write.seq

    def to_json(self) -> dict[str, JsonValue]:
        write: dict[str, JsonValue] = (
            self.write.to_json()
            if self.write is not None
            else {
                "tx_id": None,
                "seq": None,
                "event_id": None,
                "dry_run": False,
                "idempotent_replay": False,
                "id": self.entity["id"],
                "version": self.entity["rev"],
                "entities": [],
                "findings": [],
            }
        )
        return {**write, "entity": self.entity, "file": self.file, "existing": self.existing}


class HashedBlob:
    """A dry-run blob: hashed and counted, never staged; it counts as present."""

    def __init__(self, digest: str, size: int) -> None:
        self.hash = digest
        self.size = size

    def discard(self) -> None:
        return None


class _Input:
    """One pass over the input file: chunks, and the first bytes for the media type."""

    def __init__(self, handle: BinaryIO, path: Path) -> None:
        self._handle = handle
        self._path = path
        self.head = b""

    def chunks(self) -> Iterator[bytes]:
        while True:
            try:
                chunk = self._handle.read(CHUNK_BYTES)
            except OSError as exc:
                raise unreadable(self._path, exc.strerror or str(exc)) from exc
            if not chunk:
                return
            if len(self.head) < media_types.SNIFF_BYTES:
                self.head += chunk[: media_types.SNIFF_BYTES - len(self.head)]
            yield chunk


def unreadable(path: Path, why: str) -> GraphCliError:
    return GraphCliError(
        "file.unreadable",
        f"cannot read {str(path)!r}: {why}",
        details={"argument": "path"},
    )


def too_large(max_bytes: int) -> GraphCliError:
    return GraphCliError(
        "file.too_large",
        f"the file is larger than {max_bytes} bytes",
        details={"max_bytes": max_bytes},
    )


def file_types(model: CompiledModel) -> tuple[str, ...]:
    """Concrete classes of the ``files:File`` family; ``file.module_missing`` without it."""
    if FILE_TYPE not in model.classes:
        raise GraphCliError(
            "file.module_missing",
            "the graph's ontology does not import the files module; import files ^1.0 in it",
            details={"type": FILE_TYPE},
        )
    return tuple(c for c in model.subtypes(FILE_TYPE) if not model.classes[c].abstract)


def find_by_content(model: CompiledModel, read: GraphStoreRead, digest: str) -> Revision | None:
    """The active file holding ``digest``; content is unique among active files."""
    query = Query(
        types=tuple(TermRef(t) for t in file_types(model)),
        where=Cmp("content", "eq", digest),
        record_status=_ACTIVE,
        limit=1,
    )
    items = read.snapshot().query(query).items
    return items[0] if items else None


class FileService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        writer: WriteService,
        origins: Origins | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._catalog = catalog
        self._writer = writer
        self._origins: Origins = origins if origins is not None else NoOrigins()
        self.max_bytes = max_bytes
        self._models = ModelCache(catalog)

    def add(self, request: FileAddRequest) -> FileAddResult:
        handle = self._open(request.path)
        with handle:
            entry = self._catalog.resolve(request.graph)
            model = self._models.load(entry)
            type_ref = self._type(model, request.type)
            self._check_media_type(model, request.media_type)
            read, write = self._catalog.store_for(entry.id)
            source = _Input(handle, request.path)
            staged: StagedBlob
            if request.dry_run:
                staged = self._hash(source)
            else:
                staged = write.stage_blob(source.chunks(), max_bytes=self.max_bytes)
            try:
                return self._add(request, entry, model, read, type_ref, source, staged)
            finally:
                staged.discard()

    # ---- steps ---------------------------------------------------------

    def _open(self, path: Path) -> BinaryIO:
        try:
            status = os.stat(path)
        except OSError as exc:
            raise unreadable(path, exc.strerror or str(exc)) from exc
        if not stat.S_ISREG(status.st_mode):
            raise unreadable(path, "not a regular file")
        try:
            return open(path, "rb")
        except OSError as exc:
            raise unreadable(path, exc.strerror or str(exc)) from exc

    def _type(self, model: CompiledModel, requested: str | None) -> str:
        allowed = file_types(model)
        type_ref = requested or FILE_TYPE
        if type_ref not in allowed:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--type must be {FILE_TYPE} or a concrete subclass of it",
                details={"argument": "--type", "value": type_ref, "expected": list(allowed)},
            )
        return type_ref

    def _check_media_type(self, model: CompiledModel, media_type: str | None) -> None:
        if media_type is None:
            return
        problem = check_facets(media_type, model.facets("datatype", MEDIA_TYPE_DATATYPE))
        if problem is not None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--media-type {media_type!r} {problem}",
                details={"argument": "--media-type", "value": media_type},
            )

    def _hash(self, source: _Input) -> HashedBlob:
        hasher, size = hashlib.sha256(), 0
        for chunk in source.chunks():
            size += len(chunk)
            if size > self.max_bytes:
                raise too_large(self.max_bytes)
            hasher.update(chunk)
        return HashedBlob("sha256:" + hasher.hexdigest(), size)

    def _add(
        self,
        request: FileAddRequest,
        entry: GraphEntry,
        model: CompiledModel,
        read: GraphStoreRead,
        type_ref: str,
        source: _Input,
        staged: StagedBlob,
    ) -> FileAddResult:
        file_name = request.path.name
        media_type = request.media_type or media_types.detect(source.head, file_name)
        known = find_by_content(model, read, staged.hash)
        if known is not None:
            return self._existing(entry, known, staged)
        warnings: list[WarningRecord] = []
        origin = None
        if not request.no_origin:
            origin = self._origins.origin_of(request.path.resolve())
            if origin is None:
                warnings.append(
                    WarningRecord(
                        "file.no_root",
                        "no configured file root contains the file; it has no origin",
                        {},
                    )
                )
        attrs: dict[str, JsonValue] = {
            "title": request.title or (Path(file_name).stem or file_name),
            "content": staged.hash,
            "media_type": media_type,
            "file_name": file_name,
            **({"origin": origin} if origin is not None else {}),
            **({"source_url": request.source_url} if request.source_url else {}),
            **({"retrieved_at": request.retrieved_at} if request.retrieved_at else {}),
            **request.attrs,
        }
        changeset = ChangeSet(
            ops=(CreateOp(type=TermRef(type_ref), attrs=attrs, attrs_text=request.attrs_text),),
            reason=request.reason,
            idempotency_key=request.idempotency_key,
        )
        write_request = WriteRequest(
            actor=request.actor,
            graph=str(entry.id),
            changeset=changeset,
            dry_run=request.dry_run,
            request=request.request,
            blobs=(staged,),
        )
        try:
            result = self._writer.apply(write_request)
        except GraphCliError as exc:
            winner = _lost_race(exc) and find_by_content(model, read, staged.hash)
            if winner:
                return self._existing(entry, winner, staged)
            raise
        (created,) = result.entities
        entity: dict[str, JsonValue] = {
            "id": None if created.id is None else str(created.id),
            "alias": created.alias,
            "type": created.type,
            "rev": created.rev,
        }
        file = _file_json(staged.hash, staged.size, attrs)
        warnings = [*warnings, *result.warnings]
        return FileAddResult(
            entry, entity, file, False, replace(result, warnings=()), tuple(warnings)
        )

    def _existing(self, entry: GraphEntry, known: Revision, staged: StagedBlob) -> FileAddResult:
        entity: dict[str, JsonValue] = {
            "id": str(known.id),
            "alias": known.alias,
            "type": known.type,
            "rev": known.rev,
        }
        warning = WarningRecord(
            "file.already_present",
            f"the graph already stores this file as {known.alias or known.id}; nothing was written",
            {"id": str(known.id)},
        )
        file = _file_json(staged.hash, staged.size, known.attrs)
        return FileAddResult(entry, entity, file, True, None, (warning,))


def _file_json(digest: str, size: int, attrs: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "hash": digest,
        "size": size,
        "media_type": attrs.get("media_type"),
        "file_name": attrs.get("file_name"),
        "origin": attrs.get("origin"),
    }


def _lost_race(error: GraphCliError) -> bool:
    """A refused create whose only error is another active file with this content."""
    if error.code != "write.invalid":
        return False
    findings = error.details.get("findings")
    if not isinstance(findings, list):
        return False
    errors = [f for f in findings if isinstance(f, Mapping) and f.get("severity") == "error"]
    return bool(errors) and all(
        f.get("code") == "data.semantic.unique_violation"
        and isinstance(f.get("target"), Mapping)
        and f["target"].get("path") == "attrs.content"
        for f in errors
    )
