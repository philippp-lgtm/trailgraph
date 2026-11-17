"""Revisions, edges and tx records with their persisted JSON form (contracts B4, C.9).

The JSON mapping lives here so that every backend writes byte-identical
documents (B4 portability test). Absent optional values are omitted (C.6).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import HASH_RE, JsonObject, JsonValue, hash_value
from graph_cli.kernel.clock import format_timestamp, parse_timestamp
from graph_cli.kernel.errors import EMPTY, GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, GraphId, TermRef, TxId, parse_id
from graph_cli.kernel.json_access import (
    JsonShapeError,
    as_list,
    as_object,
    opt_int,
    opt_obj,
    opt_str,
    req_int,
    req_list,
    req_obj,
    req_str,
)

REVISION_SCHEMA: Final = "graph_cli.revision/1"
TX_SCHEMA: Final = "graph_cli.tx/1"

RecordStatus = Literal["active", "superseded", "retracted"]
RECORD_STATUSES: Final[tuple[RecordStatus, ...]] = ("active", "superseded", "retracted")


@dataclass(frozen=True, slots=True)
class OntologyPin:
    bundle: str
    version: str
    artifact_hash: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"bundle": self.bundle, "version": self.version, "artifact_hash": self.artifact_hash}

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "") -> OntologyPin:
        return cls(
            req_str(data, "bundle", path),
            req_str(data, "version", path),
            req_str(data, "artifact_hash", path),
        )


@dataclass(frozen=True, slots=True)
class ActionRef:
    """The published action a request ran: its name and the hash of its compiled entry
    (actions plan A7)."""

    name: str
    hash: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "hash": self.hash}

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "") -> ActionRef:
        return cls(req_str(data, "name", path), req_str(data, "hash", path))


SOURCE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_SOURCE_TEXT: Final = 128
# In-process callers only (never parsed from a document): the extraction service names itself.
SOURCE_KINDS: Final = ("extractor",)
_SOURCE_KEYS: Final = frozenset({"name", "version", "run", "kind"})


def is_source_text(value: object) -> bool:
    """Whether ``value`` is a ``version`` or ``run`` of a source: 1 to ``MAX_SOURCE_TEXT``
    printable characters, no blank at either end. One definition for the document parser and
    the audit schema."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_SOURCE_TEXT
        and value.isprintable()
        and value == value.strip()
    )


def is_source_ref_json(value: JsonValue) -> bool:
    """Whether ``value`` has the persisted shape of a ``SourceRef``: ``name`` plus optional
    ``version``, ``run`` and ``kind``, nothing else."""
    return (
        isinstance(value, Mapping)
        and "name" in value
        and set(value) <= _SOURCE_KEYS
        and isinstance(value["name"], str)
        and SOURCE_NAME_RE.match(value["name"]) is not None
        and all(is_source_text(value[key]) for key in ("version", "run") if key in value)
        and ("kind" not in value or value["kind"] in SOURCE_KINDS)
    )


@dataclass(frozen=True, slots=True)
class SourceRef:
    """The source run a request hands over: the producer's logical ``name``, its ``version``
    (a collector release, a git sha) and the ``run`` (its run id or start time).

    A claim recorded with the authenticated actor, like a reason; it grants nothing. ``kind``
    is set by in-process callers only (``SOURCE_KINDS``); a document cannot name it.
    """

    name: str
    version: str | None = None
    run: str | None = None
    kind: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"name": self.name}
        for key in ("version", "run", "kind"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "") -> SourceRef:
        return cls(
            req_str(data, "name", path),
            opt_str(data, "version", path),
            opt_str(data, "run", path),
            opt_str(data, "kind", path),
        )


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """What was asked: command name and ``args_digest`` (C.10); ``action`` names the
    published action of a ``do`` (actions plan A7), ``source`` the source run of a document
    that names one (reconcile plan R-K10).

    Lives in the kernel (contract draft: ``services.write``) because tx records
    and authorized txs carry it.
    """

    command: str
    args_digest: str
    action: ActionRef | None = None
    source: SourceRef | None = None

    @classmethod
    def of(
        cls,
        command: str,
        args: Mapping[str, JsonValue],
        action: ActionRef | None = None,
        source: SourceRef | None = None,
    ) -> RequestMeta:
        """C.10: the digest covers the request DTO, not raw argv."""
        return cls(command, hash_value({"command": command, "args": dict(args)}), action, source)

    def to_json(self) -> dict[str, JsonValue]:
        """The ``request`` object of an audit event or a tx record; ``action`` and ``source``
        only when set, so requests without one keep their bytes."""
        data: dict[str, JsonValue] = {"command": self.command, "args_digest": self.args_digest}
        if self.action is not None:
            data["action"] = self.action.to_json()
        if self.source is not None:
            data["source"] = self.source.to_json()
        return data

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "request.") -> RequestMeta:
        return cls(
            req_str(data, "command", path),
            req_str(data, "args_digest", path),
            ActionRef.from_json(req_obj(data, "action", path), f"{path}action.")
            if "action" in data
            else None,
            SourceRef.from_json(req_obj(data, "source", path), f"{path}source.")
            if "source" in data
            else None,
        )


@dataclass(frozen=True, slots=True)
class EnvInfo:
    """Where an invocation ran: the resolved data root and how it was chosen (B5.14, B12.9).

    ``data_root`` is ``None`` when config redacts it (``[audit] record_data_root``).
    """

    data_root: str | None
    root_source: str

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"root_source": self.root_source}
        if self.data_root is not None:
            data["data_root"] = self.data_root
        return data


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    edge_id: EdgeId
    rel: TermRef
    target: EntityId
    qualifiers: Mapping[str, JsonValue] = field(default=EMPTY)
    target_rev: int | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "edge_id": str(self.edge_id),
            "rel": self.rel,
            "target": str(self.target),
        }
        if self.qualifiers:
            data["qualifiers"] = dict(self.qualifiers)
        if self.target_rev is not None:
            data["target_rev"] = self.target_rev
        return data

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "") -> EdgeRecord:
        return cls(
            edge_id=EdgeId(_uuid(data, "edge_id", path)),
            rel=TermRef(req_str(data, "rel", path)),
            target=EntityId(_uuid(data, "target", path)),
            qualifiers=opt_obj(data, "qualifiers", path),
            target_rev=opt_int(data, "target_rev", path),
        )


@dataclass(frozen=True, slots=True)
class Revision:
    id: EntityId
    rev: int
    prev_rev: int | None
    type: TermRef
    record_status: RecordStatus
    attrs: Mapping[str, JsonValue]
    edges: tuple[EdgeRecord, ...]
    alias: str | None
    valid_from: str | None
    valid_to: str | None
    created_at: datetime
    recorded_at: datetime
    tx_id: TxId
    tx_seq: int
    ontology_version: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RevisionMeta:
    rev: int
    tx_seq: int
    tx_id: TxId
    recorded_at: datetime
    content_hash: str
    record_status: RecordStatus


def content_projection(
    *,
    type: str,
    record_status: str,
    alias: str | None,
    attrs: Mapping[str, JsonValue],
    edges: tuple[EdgeRecord, ...],
    valid_from: str | None,
    valid_to: str | None,
) -> dict[str, JsonValue]:
    """C.9 content projection: identical content gives an identical hash."""
    data: dict[str, JsonValue] = {
        "type": type,
        "record_status": record_status,
        "attrs": dict(attrs),
        "edges": [edge.to_json() for edge in edges],
    }
    for key, value in (("alias", alias), ("valid_from", valid_from), ("valid_to", valid_to)):
        if value is not None:
            data[key] = value
    return data


def content_hash(projection: Mapping[str, JsonValue]) -> str:
    """C.9: hash of a content projection."""
    return hash_value(projection)


def revision_content_hash(revision: Revision) -> str:
    return content_hash(
        content_projection(
            type=revision.type,
            record_status=revision.record_status,
            alias=revision.alias,
            attrs=revision.attrs,
            edges=revision.edges,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
        )
    )


def strip_revision(
    revision: Revision,
    sees: Callable[[EntityId], bool] | None,
    keep: Collection[str] | None,
    *,
    rehash: bool,
) -> Revision:
    """``revision`` without the edges whose target ``sees`` rejects and without the attributes
    outside ``keep`` (``None``: that part is not stripped), in one copy.

    Returns the same object when nothing is dropped. Otherwise the kept edges keep their order
    and ids, the kept attributes their values, and every other field (``rev``, ``tx_seq``, ...)
    stays, so nothing marks an entity as having lost something. ``rehash`` recomputes
    ``content_hash`` over the stripped content: set it for a revision handed to a caller, leave
    it for one stripped only to be matched (the hash is then stale and must not be read).
    """
    edges = revision.edges
    if sees is not None:
        edges = tuple(edge for edge in revision.edges if sees(edge.target))
    attrs = revision.attrs
    if keep is not None and not all(name in keep for name in attrs):
        attrs = {name: value for name, value in attrs.items() if name in keep}
    if len(edges) == len(revision.edges) and attrs is revision.attrs:
        return revision
    stripped = replace(revision, edges=edges, attrs=attrs)
    if not rehash:
        return stripped
    return replace(stripped, content_hash=revision_content_hash(stripped))


def revision_meta(revision: Revision) -> RevisionMeta:
    return RevisionMeta(
        rev=revision.rev,
        tx_seq=revision.tx_seq,
        tx_id=revision.tx_id,
        recorded_at=revision.recorded_at,
        content_hash=revision.content_hash,
        record_status=revision.record_status,
    )


def revision_to_json(revision: Revision) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "schema": REVISION_SCHEMA,
        "id": str(revision.id),
        "rev": revision.rev,
        "type": revision.type,
        "record_status": revision.record_status,
        "attrs": dict(revision.attrs),
        "edges": [edge.to_json() for edge in revision.edges],
        "created_at": format_timestamp(revision.created_at),
        "recorded_at": format_timestamp(revision.recorded_at),
        "tx_id": str(revision.tx_id),
        "tx_seq": revision.tx_seq,
        "ontology_version": revision.ontology_version,
        "content_hash": revision.content_hash,
    }
    optional = (
        ("prev_rev", revision.prev_rev),
        ("alias", revision.alias),
        ("valid_from", revision.valid_from),
        ("valid_to", revision.valid_to),
    )
    for key, value in optional:
        if value is not None:
            data[key] = value
    return data


def revision_from_json(data: JsonObject, path: str = "") -> Revision:
    """Parse a persisted revision. Unknown keys are preserved by the file, ignored here (V.4)."""
    _check_schema(data, REVISION_SCHEMA, path)
    return Revision(
        id=EntityId(_uuid(data, "id", path)),
        rev=req_int(data, "rev", path),
        prev_rev=opt_int(data, "prev_rev", path),
        type=TermRef(req_str(data, "type", path)),
        record_status=_record_status(req_str(data, "record_status", path)),
        attrs=req_obj(data, "attrs", path),
        edges=tuple(
            EdgeRecord.from_json(as_object(edge, f"{path}edges[{i}]"), f"{path}edges[{i}].")
            for i, edge in enumerate(req_list(data, "edges", path))
        ),
        alias=opt_str(data, "alias", path),
        valid_from=opt_str(data, "valid_from", path),
        valid_to=opt_str(data, "valid_to", path),
        created_at=_timestamp(data, "created_at", path),
        recorded_at=_timestamp(data, "recorded_at", path),
        tx_id=TxId(_uuid(data, "tx_id", path)),
        tx_seq=req_int(data, "tx_seq", path),
        ontology_version=req_str(data, "ontology_version", path),
        content_hash=req_str(data, "content_hash", path),
    )


@dataclass(frozen=True, slots=True)
class TxRecord:
    """A parsed tx line (B4.4). ``hash`` is C.2 over the record."""

    graph_id: GraphId
    tx_id: TxId
    seq: int
    recorded_at: datetime
    event_id: EventId
    actor: ActorContext
    ontology: OntologyPin
    request: RequestMeta
    reason: str | None
    idempotency_key: str | None
    changeset: Mapping[str, JsonValue]
    changeset_hash: str
    resolution: Mapping[str, JsonValue]
    revisions: tuple[Revision, ...]
    alias_allocations: tuple[tuple[EntityId, str], ...]
    prev_hash: str
    hash: str
    blobs: tuple[str, ...] = ()  # hashes of the blobs its revisions reference, sorted


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """A stored blob: its C.2 hash string and size in bytes."""

    hash: str
    size: int


def tx_to_json(tx: TxRecord) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "schema": TX_SCHEMA,
        "graph_id": str(tx.graph_id),
        "tx_id": str(tx.tx_id),
        "seq": tx.seq,
        "recorded_at": format_timestamp(tx.recorded_at),
        "event_id": str(tx.event_id),
        "actor": tx.actor.to_json(),
        "ontology": tx.ontology.to_json(),
        "request": tx.request.to_json(),
        "changeset": dict(tx.changeset),
        "changeset_hash": tx.changeset_hash,
        "resolution": dict(tx.resolution),
        "revisions": [revision_to_json(r) for r in tx.revisions],
        "alias_allocations": [
            {"entity": str(entity), "alias": alias} for entity, alias in tx.alias_allocations
        ],
        "prev_hash": tx.prev_hash,
        "hash": tx.hash,
    }
    if tx.reason is not None:
        data["reason"] = tx.reason
    if tx.idempotency_key is not None:
        data["idempotency_key"] = tx.idempotency_key
    if tx.blobs:  # additive in graph_cli.tx/1: omitted when empty, older bytes unchanged
        data["blobs"] = list(tx.blobs)
    return data


def tx_from_json(data: JsonObject) -> TxRecord:
    _check_schema(data, TX_SCHEMA, "")
    request = req_obj(data, "request")
    allocations = []
    for i, item in enumerate(req_list(data, "alias_allocations")):
        entry = as_object(item, f"alias_allocations[{i}]")
        path = f"alias_allocations[{i}]."
        allocations.append((EntityId(_uuid(entry, "entity", path)), req_str(entry, "alias", path)))
    return TxRecord(
        graph_id=GraphId(_uuid(data, "graph_id")),
        tx_id=TxId(_uuid(data, "tx_id")),
        seq=req_int(data, "seq"),
        recorded_at=_timestamp(data, "recorded_at", ""),
        event_id=EventId(_uuid(data, "event_id")),
        actor=ActorContext.from_json(req_obj(data, "actor")),
        ontology=OntologyPin.from_json(req_obj(data, "ontology"), "ontology."),
        request=RequestMeta.from_json(request),
        reason=opt_str(data, "reason"),
        idempotency_key=opt_str(data, "idempotency_key"),
        changeset=req_obj(data, "changeset"),
        changeset_hash=req_str(data, "changeset_hash"),
        resolution=req_obj(data, "resolution"),
        revisions=tuple(
            revision_from_json(as_object(r, f"revisions[{i}]"), f"revisions[{i}].")
            for i, r in enumerate(req_list(data, "revisions"))
        ),
        alias_allocations=tuple(allocations),
        prev_hash=req_str(data, "prev_hash"),
        hash=req_str(data, "hash"),
        blobs=_blobs(data),
    )


def _blobs(data: JsonObject) -> tuple[str, ...]:
    value = data.get("blobs")
    if value is None:
        return ()
    items = as_list(value, "blobs")
    blobs = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not HASH_RE.match(item):
            raise JsonShapeError(f"blobs[{i}]: not a sha256 hash string")
        blobs.append(item)
    return tuple(blobs)


def _check_schema(data: JsonObject, expected: str, path: str) -> None:
    schema = req_str(data, "schema", path)
    if schema != expected:
        # V.3: an unknown major is refused, never guessed.
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported document schema {schema!r}, expected {expected!r}",
            details={"expected": expected, "actual": schema},
        )


def _uuid(data: JsonObject, key: str, path: str = "") -> UUID:
    text = req_str(data, key, path)
    try:
        return parse_id(text)
    except GraphCliError:
        raise JsonShapeError(f"{path}{key}: not a UUID") from None


def _timestamp(data: JsonObject, key: str, path: str) -> datetime:
    text = req_str(data, key, path)
    try:
        return parse_timestamp(text)
    except GraphCliError:
        raise JsonShapeError(f"{path}{key}: not a timestamp") from None


def _record_status(value: str) -> RecordStatus:
    if value not in RECORD_STATUSES:
        raise GraphCliError("format.unsupported_version", f"unknown record_status {value!r}")
    return value
