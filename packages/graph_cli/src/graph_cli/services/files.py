"""Stored files: add a file to a graph (files slice 1; contracts B3, B7.8, B13).

graph_cli stores, addresses and references files; it never reads what is in
them. ``FileService.add``:

1. The input must be a regular file (after symlinks), else ``file.unreadable``.
2. The type is ``files:File`` or a concrete subclass; a model without the
   ``files`` module is ``file.module_missing``.
3. The bytes are streamed once: hashed, counted against ``max_bytes`` and
   staged before the graph lock (a dry-run hashes in memory and stages
   nothing), the ``stage`` phase of the command's timings; the media type
   comes from the first bytes and the name.
4. Known content returns the active file that holds it, with warning
   ``file.already_present`` and without a tx. A concurrent add of the same
   content loses on ``unique_violation`` and returns the winner the same way.
5. The origin is ``{root, path}`` against the configured roots of this
   machine; an absolute path is never recorded. Without a matching root the
   file has no origin and the result warns ``file.no_root``.
6. One ``create`` goes through ``WriteService.apply`` with the staged blob,
   which is published at W.11b. The staged copy is discarded afterwards in
   every case.

Reads (``path``, ``open``, ``check``) are ``read.get`` on the graph and then on
``graph:<id>/type:<curie>`` of the file, so a policy can deny file content
per type. They write nothing to the graph; ``check`` hashes the origin files. They
hand the ids they return to the activity sink with the request's command and actor
(see ``services.read``).

``open`` is the one way to read a stored file's bytes (object storage O8): the file's
metadata and its chunks from ``read_blob`` (verified while they stream when the
backend checks them), with the stored copy's local path when the backend keeps one.
The server's download route and the live view stream through it (``open_stored`` is
the part without the app's authorization, which the view replaces with its own).
``path`` answers the stored copy's path: the backend's own file, else a read-only copy
in this machine's file cache (``FileCache``, injected by the app wiring), else
``file.no_local_path``. On the server (``paths=False``) the stored copy's path means
nothing to the caller: ``path`` answers ``null`` and touches no bytes.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Final, Protocol

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, CreateOp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink, ReadSignal
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.ports.storage import CAPABILITY_BLOB_LOCAL_PATH, GraphStoreRead, StagedBlob
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import check_facets
from graph_cli.ontology_layer.vocabulary import FILE_TYPE
from graph_cli.services import media_types
from graph_cli.services.models import ModelCache
from graph_cli.services.read import GraphRead, Page, ReadService
from graph_cli.services.refs import resolve_ref
from graph_cli.services.write import WriteRequest, WriteResult, WriteService

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


class FileCache(Protocol):
    """Read-only copies of stored files on this machine (``<state>/files/<graph id>/<hex>``),
    for a backend without local paths and for the remote client (object storage O8)."""

    def prune(self) -> None:
        """Remove the copies nobody used for a day; never fails."""
        ...

    def copy(
        self,
        graph: str,
        digest: str,
        fill: Callable[[BinaryIO], None],
        *,
        mismatch: Callable[[], GraphCliError] | None = None,
    ) -> Path:
        """The copy of ``digest``, used now; written by ``fill`` and hash-checked when it
        is not there or no longer has its hash. ``mismatch`` is the error for written bytes
        that do not have the hash (default ``backend.corrupt``)."""
        ...


class FileStream:
    """The bytes of a stored file, started: the first chunk is read when it is built, so a
    missing or refused object (and the hash mismatch of a file of one chunk) is raised
    before anything was sent; a later failure is raised by the iteration. ``close`` ends
    the read (an object store's connection) when the bytes are not all wanted."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        try:
            self._first: bytes | None = next(chunks)
        except StopIteration:
            self._first = None
        except BaseException:
            self.close()
            raise

    def __iter__(self) -> FileStream:
        return self

    def __next__(self) -> bytes:
        first = self._first
        if first is not None:
            self._first = None
            return first
        return next(self._chunks)

    def close(self) -> None:
        close = getattr(self._chunks, "close", None)
        if close is not None:
            close()


@dataclass(frozen=True, slots=True)
class OpenedFile:
    """A stored file ready to be read (``open_stored``); nothing is read yet."""

    id: str
    alias: str | None
    type: str
    hash: str
    size: int
    media_type: str | None
    file_name: str | None
    origin: Mapping[str, JsonValue] | None
    local_path: Path | None  # the backend's own read-only file, when it keeps one
    read: GraphStoreRead = field(repr=False)

    def start(self) -> FileStream:
        """The verified bytes, started (``FileStream``)."""
        return FileStream(iter(self.read.read_blob(self.hash)))


def open_stored(read: GraphStoreRead, revision: Revision, ref: str) -> OpenedFile:
    """The stored file of a ``files:File`` revision the caller may read: ``backend.corrupt``
    when its blob is absent, ``backend.unavailable`` when the store cannot be asked."""
    digest = str(revision.attrs.get("content"))
    stored = read.blob(digest)
    if stored is None:
        raise GraphCliError(
            "backend.corrupt",
            f"the stored copy of {ref} is missing; run graph-admin files verify",
            details={"id": str(revision.id), "blob": digest},
        )
    local = (
        read.blob_local_path(digest) if CAPABILITY_BLOB_LOCAL_PATH in read.capabilities() else None
    )
    media_type, file_name = revision.attrs.get("media_type"), revision.attrs.get("file_name")
    origin = revision.attrs.get("origin")
    return OpenedFile(
        id=str(revision.id),
        alias=revision.alias,
        type=revision.type,
        hash=digest,
        size=stored.size,
        media_type=media_type if isinstance(media_type, str) else None,
        file_name=file_name if isinstance(file_name, str) else None,
        origin=origin if isinstance(origin, Mapping) else None,
        local_path=local,
        read=read,
    )


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
    read_seq: int | None = None  # the state that showed the file present

    @property
    def seq(self) -> int | None:
        return self.read_seq if self.write is None else self.write.seq

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
                "entity_count": 0,
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


def module_missing() -> GraphCliError:
    return GraphCliError(
        "file.module_missing",
        "the graph's ontology does not import the files module; import files ^1.0 in it",
        details={"type": FILE_TYPE},
    )


def file_types(model: CompiledModel) -> tuple[str, ...]:
    """Concrete classes of the ``files:File`` family; ``file.module_missing`` without it."""
    if FILE_TYPE not in model.classes:
        raise module_missing()
    return tuple(c for c in model.subtypes(FILE_TYPE) if not model.classes[c].abstract)


def _family_text(view: GraphRead, types: tuple[str, ...]) -> str:
    """What the "not a stored file" message names: ``files:File``, or for a caller who does
    not see that type (it reads the file classes it sees) those classes, so the message never
    names a hidden type."""
    if not view.read_view.types_limited or FILE_TYPE in view.model.classes:
        return FILE_TYPE
    return ", ".join(types)


def find_by_content(
    model: CompiledModel,
    read: GraphStoreRead,
    digest: str,
    visible: frozenset[str] | None = None,
) -> Revision | None:
    """The active file holding ``digest``; content is unique among active files. A caller who
    sees only some types (``visible``) finds a holder of those types only: a hidden file
    holding the same bytes is not there for it (type read limits plan V6)."""
    types = tuple(t for t in file_types(model) if visible is None or t in visible)
    if not types:
        return None
    query = Query(
        types=tuple(TermRef(t) for t in types),
        where=Cmp("content", "eq", digest),
        record_status=_ACTIVE,
        limit=1,
    )
    items = read.snapshot().query(query).items
    return items[0] if items else None


CHECK_STATUSES: Final = ("same", "changed", "missing", "root_unknown", "no_origin")


class FileService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        writer: WriteService,
        reader: ReadService | None = None,
        origins: Origins | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        activity: ActivitySink = NULL_ACTIVITY,
        cache: FileCache | None = None,
        paths: bool = True,
    ) -> None:
        self._catalog = catalog
        self._writer = writer
        self._reader = reader
        self._origins: Origins = origins if origins is not None else NoOrigins()
        self.max_bytes = max_bytes
        self._activity = activity
        self._cache = cache
        self._paths = paths  # False on the server: a path there means nothing to the caller
        self._timings = writer.timings  # one command, one set of timings
        self._models = ModelCache(catalog, self._timings)

    def add(self, request: FileAddRequest) -> FileAddResult:
        handle = self._open(request.path)
        with handle:
            entry = self._catalog.resolve(request.graph)
            model = self._models.load(entry)
            visible = (
                None if self._reader is None else self._reader.visible_types(request.actor, entry)
            )
            type_ref = self._type(model, request.type, visible)
            self._check_media_type(model, request.media_type)
            read, write = self._catalog.store_for(entry.id)
            source = _Input(handle, request.path)
            staged: StagedBlob
            with self._timings.phase("stage"):
                if request.dry_run:
                    staged = self._hash(source)
                else:
                    staged = write.stage_blob(source.chunks(), max_bytes=self.max_bytes)
            try:
                return self._add(request, entry, model, read, type_ref, source, staged, visible)
            finally:
                staged.discard()

    # ---- reads ---------------------------------------------------------

    def path(
        self,
        view: GraphRead,
        ref: str,
        *,
        origin: bool,
        actor: ActorContext,
        request: RequestMeta,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """The stored copy's path (read only), or with ``origin`` the origin on this machine."""
        revision, warnings = self._file(view, ref, actor, request)
        opened = open_stored(self._catalog.store_for(view.entry.id)[0], revision, ref)
        path: str | None
        if not origin and self._cache is not None:
            self._cache.prune()  # copies nobody used for a day go with the next file path
        if origin:
            path = str(self._origin_path(opened, ref))
        elif not self._paths:
            path = None
        elif opened.local_path is not None:
            path = str(opened.local_path)
        elif self._cache is not None:
            path = str(self._cached(str(view.entry.id), opened))
        else:
            raise GraphCliError(
                "file.no_local_path",
                "this backend keeps stored files without a local path",
                details={"id": opened.id},
            )
        self._activity.reads(ReadSignal(view.entry.id, request.command, actor, [revision.id]))
        return self.answer(opened, path), warnings

    def open(
        self, view: GraphRead, ref: str, *, actor: ActorContext, request: RequestMeta
    ) -> tuple[OpenedFile, list[WarningRecord]]:
        """The stored file to stream, authorized like ``path`` (object storage O8)."""
        revision, warnings = self._file(view, ref, actor, request)
        opened = open_stored(self._catalog.store_for(view.entry.id)[0], revision, ref)
        self._activity.reads(ReadSignal(view.entry.id, request.command, actor, [revision.id]))
        return opened, warnings

    def answer(self, opened: OpenedFile, path: str | None = None) -> dict[str, JsonValue]:
        """What ``file path`` answers about ``opened``, with ``path``."""
        return {
            "id": opened.id,
            "alias": opened.alias,
            "type": opened.type,
            "hash": opened.hash,
            "size": opened.size,
            "media_type": opened.media_type,
            "path": path,
            "origin": self._origin_json(opened.origin),
        }

    def _origin_path(self, opened: OpenedFile, ref: str) -> Path:
        if opened.origin is None:
            raise GraphCliError(
                "file.origin_missing", f"{ref} has no recorded origin", details={"id": opened.id}
            )
        resolved = self._origins.resolve(opened.origin)  # file.root_unknown
        if not resolved.is_file():
            raise GraphCliError(
                "file.origin_missing",
                f"the origin of {ref} is not there: {resolved}",
                details={"id": opened.id, "path": str(resolved)},
            )
        return resolved

    def _cached(self, graph: str, opened: OpenedFile) -> Path:
        """A read-only copy in this machine's file cache, written from the verified bytes."""
        assert self._cache is not None

        def fill(sink: BinaryIO) -> None:
            stream = opened.start()
            try:
                for chunk in stream:
                    sink.write(chunk)
            finally:
                stream.close()

        return self._cache.copy(graph, opened.hash, fill)

    def check(
        self,
        view: GraphRead,
        refs: Sequence[str],
        *,
        limit: int,
        offset: int,
        actor: ActorContext,
        request: RequestMeta,
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        """Compare origins with the stored content; without ``refs`` every active file
        with an origin, paged like ``find``."""
        warnings: list[WarningRecord] = []
        if refs:
            revisions = []
            for ref in refs:
                revision, found = self._file(view, ref, actor, request)
                warnings += found
                revisions.append(revision)
            page = Page(len(revisions), len(revisions), max(limit, len(revisions)), 0)
        else:
            types = self._family(view)
            for type_ref in types:
                self._authorize_type(view, type_ref, actor, request)
            query = Query(
                types=tuple(TermRef(t) for t in types),
                where=Cmp("origin", "exists"),
                record_status=_ACTIVE,
                limit=limit,
                offset=offset,
            )
            result = view.snapshot.query(query)
            revisions = list(result.items)
            page = Page(result.total, len(revisions), limit, offset)
        items: list[JsonValue] = []
        counts = dict.fromkeys(CHECK_STATUSES, 0)
        for revision in revisions:
            status, where = self._status(revision)
            counts[status] += 1
            items.append(
                {
                    "id": str(revision.id),
                    "alias": revision.alias,
                    "status": status,
                    "origin": where,
                    "hash": revision.attrs.get("content"),
                }
            )
        counts_json: dict[str, JsonValue] = dict(counts)
        signal = ReadSignal(view.entry.id, request.command, actor, [r.id for r in revisions])
        self._activity.reads(signal)
        return {"items": items, "counts": counts_json}, page, warnings

    def _file(
        self, view: GraphRead, ref: str, actor: ActorContext, request: RequestMeta
    ) -> tuple[Revision, list[WarningRecord]]:
        """A ``files:File`` family entity, authorized for ``read.get`` on its type."""
        types = self._family(view)
        entity, found = resolve_ref(ref, view.snapshot)
        revision = view.snapshot.get(entity)
        if revision is None:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        if revision.type not in types:
            raise GraphCliError(
                "usage.invalid_argument",
                f"{ref} is a {revision.type}, not a stored file ({_family_text(view, types)})",
                details={"argument": "ref", "value": ref, "actual": revision.type},
            )
        self._authorize_type(view, revision.type, actor, request)
        return revision, list(found)

    def _family(self, view: GraphRead) -> tuple[str, ...]:
        """The concrete file classes the caller reads. The family comes from the stored model:
        the model view of a type limited caller lost the hidden classes that visible ones extend
        (a scan stays a file when plain files are hidden). A caller who sees none of the family
        reads a graph without the files module."""
        family = file_types(self._models.load_for_read(view.entry)[0])
        if not view.read_view.types_limited:
            return family
        seen = tuple(t for t in family if view.read_view.shows_type(t))
        if not seen:
            raise module_missing()
        return seen

    def _authorize_type(
        self, view: GraphRead, type_ref: str, actor: ActorContext, request: RequestMeta
    ) -> None:
        if self._reader is None:
            raise GraphCliError("internal.error", "the file service has no reader")
        resource = f"graph:{view.entry.id}/type:{type_ref}"
        self._reader.authorize(actor, "read.get", resource, request, view.entry)

    def _origin(self, revision: Revision) -> Mapping[str, JsonValue] | None:
        value = revision.attrs.get("origin")
        return value if isinstance(value, Mapping) else None

    def _origin_json(self, where: Mapping[str, JsonValue] | None) -> JsonValue:
        """``{root, path, resolved}``; ``resolved`` is null when the root is not set here."""
        if where is None:
            return None
        try:
            resolved: JsonValue = str(self._origins.resolve(where))
        except GraphCliError as exc:
            if exc.code != "file.root_unknown":
                raise
            resolved = None
        return {"root": where.get("root"), "path": where.get("path"), "resolved": resolved}

    def _status(self, revision: Revision) -> tuple[str, JsonValue]:
        where = self._origin(revision)
        origin = self._origin_json(where)
        if where is None:
            return "no_origin", origin
        assert isinstance(origin, Mapping)
        resolved = origin.get("resolved")
        if not isinstance(resolved, str):
            return "root_unknown", origin
        digest = hash_file(Path(resolved))
        if digest is None:
            return "missing", origin
        return ("same" if digest == revision.attrs.get("content") else "changed"), origin

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

    def _type(
        self, model: CompiledModel, requested: str | None, visible: frozenset[str] | None
    ) -> str:
        """The class of the new file. A caller who sees only some types may name those: a
        hidden file class is no file class to it, and the message names the ones it sees."""
        allowed = file_types(model)
        if visible is not None:
            allowed = tuple(t for t in allowed if t in visible)
            if not allowed:
                raise module_missing()
        type_ref = requested or FILE_TYPE
        if type_ref not in allowed:
            family = (
                f"{FILE_TYPE} or a concrete subclass of it"
                if FILE_TYPE in allowed
                else "one of " + ", ".join(allowed)
            )
            raise GraphCliError(
                "usage.invalid_argument",
                f"--type must be {family}",
                details={
                    "argument": "--type",
                    "value": type_ref if visible is None else requested,  # not a type it lacks
                    "expected": list(allowed),
                },
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
        visible: frozenset[str] | None,
    ) -> FileAddResult:
        file_name = request.path.name
        media_type = request.media_type or media_types.detect(source.head, file_name)
        known = find_by_content(model, read, staged.hash, visible)
        if known is not None:
            return self._existing(entry, read, known, staged)
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
            winner = _lost_race(exc) and find_by_content(model, read, staged.hash, visible)
            if winner:
                return self._existing(entry, read, winner, staged)
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

    def _existing(
        self, entry: GraphEntry, read: GraphStoreRead, known: Revision, staged: StagedBlob
    ) -> FileAddResult:
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
        return FileAddResult(entry, entity, file, True, None, (warning,), read.head().seq)


def hash_file(path: Path) -> str | None:
    """The sha256 of a regular file, ``None`` when it is not there or cannot be read."""
    hasher = hashlib.sha256()
    try:
        if not path.is_file():
            return None
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                hasher.update(chunk)
    except OSError:
        return None
    return "sha256:" + hasher.hexdigest()


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
