"""Revisions, edges and tx records with their persisted JSON form (contracts B4, C.9).

The JSON mapping lives here so that every backend writes byte-identical
documents (B4 portability test). Absent optional values are omitted (C.6).

A sealed graph (store format 2, plan T16) stores records of schema version 2:
the structure in the clear, the values in one envelope (``sealed``). A record
carries the envelope it was stored with, so it serializes to the same bytes
again; ``kernel.record_codec`` seals and opens them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Literal, Protocol
from uuid import UUID

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import HASH_RE, JsonObject, JsonValue
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
from graph_cli.kernel.sealing import Sealed, keyed_hash

REVISION_SCHEMA: Final = "graph_cli.revision/1"
TX_SCHEMA: Final = "graph_cli.tx/1"
SEALED_REVISION_SCHEMA: Final = "graph_cli.revision/2"
SEALED_TX_SCHEMA: Final = "graph_cli.tx/2"

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


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """What was asked: command name and ``args_digest`` (C.10); ``action`` names the
    published action of a ``do`` (actions plan A7).

    Lives in the kernel (contract draft: ``services.write``) because tx records
    and authorized txs carry it.
    """

    command: str
    args_digest: str
    action: ActionRef | None = None
    # the request DTO behind the digest, kept in memory to key it again (``keyed``)
    args: Mapping[str, JsonValue] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def of(
        cls,
        command: str,
        args: Mapping[str, JsonValue],
        action: ActionRef | None = None,
        *,
        key: bytes | None = None,
    ) -> RequestMeta:
        """C.10: the digest covers the request DTO, not raw argv; keyed under the tx's MAC
        key in a sealed graph (``kernel.sealing``)."""
        return cls(command, keyed_hash({"command": command, "args": dict(args)}, key), action, args)

    def keyed(self, key: bytes | None) -> RequestMeta:
        """The same request with its digest keyed under ``key`` (plain when ``None``)."""
        if self.args is None:
            raise ValueError("a request read from a record cannot be keyed again")
        return RequestMeta.of(self.command, self.args, self.action, key=key)

    def to_json(self) -> dict[str, JsonValue]:
        """The ``request`` object of an audit event or a tx record; ``action`` only when set,
        so requests without one keep their bytes."""
        data: dict[str, JsonValue] = {"command": self.command, "args_digest": self.args_digest}
        if self.action is not None:
            data["action"] = self.action.to_json()
        return data

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "request.") -> RequestMeta:
        return cls(
            req_str(data, "command", path),
            req_str(data, "args_digest", path),
            ActionRef.from_json(req_obj(data, "action", path), f"{path}action.")
            if "action" in data
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

    def structure_json(self) -> dict[str, JsonValue]:
        """The edge without its qualifiers: what a sealed revision keeps in the clear."""
        data = self.to_json()
        data.pop("qualifiers", None)
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
    shredded: bool = False  # its key is shredded: attrs and qualifiers read as empty (T16)
    # sealed graph: the stored blob of each ``blob`` value (content hash -> blob name, T16)
    blobs: Mapping[str, str] = field(default_factory=dict)
    # the envelope it is stored with in a sealed graph; never part of equality
    sealed: Sealed | None = field(default=None, compare=False)


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


def content_hash(projection: Mapping[str, JsonValue], key: bytes | None = None) -> str:
    """C.9: hash of a content projection; keyed under the entity's MAC key in a sealed
    graph (``kernel.sealing``)."""
    return keyed_hash(projection, key)


def revision_content_hash(revision: Revision, key: bytes | None = None) -> str:
    return content_hash(
        content_projection(
            type=revision.type,
            record_status=revision.record_status,
            alias=revision.alias,
            attrs=revision.attrs,
            edges=revision.edges,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
        ),
        key,
    )


def stored_blob(revision: Revision, digest: str) -> str:
    """The name of the stored blob that holds the content ``digest`` of a ``blob`` value of
    ``revision``. Unsealed graphs store a file under its content hash; sealed ones under the
    hash of its ciphertext, which the revision records in its sealed part (T16)."""
    return revision.blobs.get(digest, digest)


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
    if revision.sealed is not None:
        return _sealed_revision_to_json(revision, revision.sealed)
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


def revision_payload(revision: Revision) -> dict[str, JsonValue]:
    """What a sealed revision encrypts: attributes, edge qualifiers by edge id, valid times,
    and the stored blob of each ``blob`` value."""
    data: dict[str, JsonValue] = {"attrs": dict(revision.attrs)}
    if revision.blobs:
        data["blobs"] = dict(revision.blobs)
    qualifiers: dict[str, JsonValue] = {
        str(edge.edge_id): dict(edge.qualifiers) for edge in revision.edges if edge.qualifiers
    }
    if qualifiers:
        data["qualifiers"] = qualifiers
    for key, value in (("valid_from", revision.valid_from), ("valid_to", revision.valid_to)):
        if value is not None:
            data[key] = value
    return data


def _sealed_revision_to_json(revision: Revision, sealed: Sealed) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "schema": SEALED_REVISION_SCHEMA,
        "id": str(revision.id),
        "rev": revision.rev,
        "type": revision.type,
        "record_status": revision.record_status,
        "edges": [edge.structure_json() for edge in revision.edges],
        "created_at": format_timestamp(revision.created_at),
        "recorded_at": format_timestamp(revision.recorded_at),
        "tx_id": str(revision.tx_id),
        "tx_seq": revision.tx_seq,
        "ontology_version": revision.ontology_version,
        "content_hash": revision.content_hash,
        "sealed": sealed.to_json(),
    }
    for key, value in (("prev_rev", revision.prev_rev), ("alias", revision.alias)):
        if value is not None:
            data[key] = value
    return data


class Opener(Protocol):
    """Opens the envelopes of sealed records (``kernel.record_codec``): the decrypted
    payload, or ``None`` when its key is shredded."""

    def revision_payload(self, entity: EntityId, rev: int, sealed: Sealed) -> JsonObject | None: ...

    def tx_payload(self, tx: TxId, seq: int, sealed: Sealed) -> JsonObject | None: ...


def revision_from_json(data: JsonObject, path: str = "", opener: Opener | None = None) -> Revision:
    """Parse a persisted revision. Unknown keys are preserved by the file, ignored here (V.4).

    Without an ``opener`` only schema 1 is accepted, with one only schema 2 (sealed)."""
    if opener is not None:
        return _sealed_revision_from_json(data, path, opener)
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


_PAYLOAD_KEYS: Final = frozenset({"attrs", "blobs", "qualifiers", "valid_from", "valid_to"})


def _sealed_revision_from_json(data: JsonObject, path: str, opener: Opener) -> Revision:
    _check_schema(data, SEALED_REVISION_SCHEMA, path)
    entity = EntityId(_uuid(data, "id", path))
    rev = req_int(data, "rev", path)
    sealed = Sealed.from_json(req_obj(data, "sealed", path), f"{path}sealed.")
    edges = tuple(
        EdgeRecord.from_json(as_object(edge, f"{path}edges[{i}]"), f"{path}edges[{i}].")
        for i, edge in enumerate(req_list(data, "edges", path))
    )
    if any(edge.qualifiers for edge in edges):
        raise JsonShapeError(f"{path}edges: qualifiers belong in the sealed part")
    payload = opener.revision_payload(entity, rev, sealed)
    attrs: Mapping[str, JsonValue] = {}
    blobs: Mapping[str, str] = {}
    valid_from = valid_to = None
    if payload is not None:
        where = f"{path}sealed(payload)."
        if not set(payload) <= _PAYLOAD_KEYS:
            raise JsonShapeError(f"{where}: unexpected keys {sorted(set(payload) - _PAYLOAD_KEYS)}")
        attrs = req_obj(payload, "attrs", where)
        qualifiers = opt_obj(payload, "qualifiers", where)
        known = {str(edge.edge_id) for edge in edges}
        if not set(qualifiers) <= known:
            raise JsonShapeError(f"{where}qualifiers: names an edge the revision does not have")
        edges = tuple(
            replace(edge, qualifiers=as_object(qualifiers[str(edge.edge_id)], where))
            if str(edge.edge_id) in qualifiers
            else edge
            for edge in edges
        )
        valid_from = opt_str(payload, "valid_from", where)
        valid_to = opt_str(payload, "valid_to", where)
        blobs = _blob_names(opt_obj(payload, "blobs", where), f"{where}blobs")
    return Revision(
        id=entity,
        rev=rev,
        prev_rev=opt_int(data, "prev_rev", path),
        type=TermRef(req_str(data, "type", path)),
        record_status=_record_status(req_str(data, "record_status", path)),
        attrs=attrs,
        edges=edges,
        alias=opt_str(data, "alias", path),
        valid_from=valid_from,
        valid_to=valid_to,
        created_at=_timestamp(data, "created_at", path),
        recorded_at=_timestamp(data, "recorded_at", path),
        tx_id=TxId(_uuid(data, "tx_id", path)),
        tx_seq=req_int(data, "tx_seq", path),
        ontology_version=req_str(data, "ontology_version", path),
        content_hash=req_str(data, "content_hash", path),
        shredded=payload is None,
        blobs=blobs,
        sealed=sealed,
    )


def _blob_names(data: JsonObject, path: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for digest, name in data.items():
        if not HASH_RE.match(digest) or not isinstance(name, str) or not HASH_RE.match(name):
            raise JsonShapeError(f"{path}: expected sha256 hash strings")
        names[digest] = name
    return names


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
    # its key is shredded: change set and resolution read as empty, reason as absent (T16)
    shredded: bool = False
    # the envelope it is stored with in a sealed graph; never part of equality
    sealed: Sealed | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """A stored blob: its C.2 hash string and size in bytes."""

    hash: str
    size: int


def tx_to_json(tx: TxRecord) -> dict[str, JsonValue]:
    if tx.sealed is not None:
        return _sealed_tx_to_json(tx, tx.sealed)
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


def tx_payload(tx: TxRecord) -> dict[str, JsonValue]:
    """What a sealed tx encrypts: change set, resolution and reason."""
    data: dict[str, JsonValue] = {
        "changeset": dict(tx.changeset),
        "resolution": dict(tx.resolution),
    }
    if tx.reason is not None:
        data["reason"] = tx.reason
    return data


def _sealed_tx_to_json(tx: TxRecord, sealed: Sealed) -> dict[str, JsonValue]:
    if any(revision.sealed is None for revision in tx.revisions):
        raise ValueError("a sealed tx holds sealed revisions only")
    data: dict[str, JsonValue] = {
        "schema": SEALED_TX_SCHEMA,
        "graph_id": str(tx.graph_id),
        "tx_id": str(tx.tx_id),
        "seq": tx.seq,
        "recorded_at": format_timestamp(tx.recorded_at),
        "event_id": str(tx.event_id),
        "actor": tx.actor.to_json(),
        "ontology": tx.ontology.to_json(),
        "request": tx.request.to_json(),
        "changeset_hash": tx.changeset_hash,
        "sealed": sealed.to_json(),
        "revisions": [revision_to_json(r) for r in tx.revisions],
        "alias_allocations": [
            {"entity": str(entity), "alias": alias} for entity, alias in tx.alias_allocations
        ],
        "prev_hash": tx.prev_hash,
        "hash": tx.hash,
    }
    if tx.idempotency_key is not None:
        data["idempotency_key"] = tx.idempotency_key
    if tx.blobs:
        data["blobs"] = list(tx.blobs)
    return data


def tx_from_json(data: JsonObject, opener: Opener | None = None) -> TxRecord:
    """Parse a tx line: schema 1 without an ``opener``, schema 2 (sealed) with one."""
    if opener is not None:
        return _sealed_tx_from_json(data, opener)
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


_TX_PAYLOAD_KEYS: Final = frozenset({"changeset", "resolution", "reason"})


def _sealed_tx_from_json(data: JsonObject, opener: Opener) -> TxRecord:
    _check_schema(data, SEALED_TX_SCHEMA, "")
    tx_id = TxId(_uuid(data, "tx_id"))
    seq = req_int(data, "seq")
    sealed = Sealed.from_json(req_obj(data, "sealed"), "sealed.")
    payload = opener.tx_payload(tx_id, seq, sealed)
    changeset: Mapping[str, JsonValue] = {}
    resolution: Mapping[str, JsonValue] = {}
    reason = None
    if payload is not None:
        where = "sealed(payload)."
        if not set(payload) <= _TX_PAYLOAD_KEYS:
            unexpected = sorted(set(payload) - _TX_PAYLOAD_KEYS)
            raise JsonShapeError(f"{where}: unexpected keys {unexpected}")
        changeset = req_obj(payload, "changeset", where)
        resolution = req_obj(payload, "resolution", where)
        reason = opt_str(payload, "reason", where)
    allocations = []
    for i, item in enumerate(req_list(data, "alias_allocations")):
        entry = as_object(item, f"alias_allocations[{i}]")
        path = f"alias_allocations[{i}]."
        allocations.append((EntityId(_uuid(entry, "entity", path)), req_str(entry, "alias", path)))
    return TxRecord(
        graph_id=GraphId(_uuid(data, "graph_id")),
        tx_id=tx_id,
        seq=seq,
        recorded_at=_timestamp(data, "recorded_at", ""),
        event_id=EventId(_uuid(data, "event_id")),
        actor=ActorContext.from_json(req_obj(data, "actor")),
        ontology=OntologyPin.from_json(req_obj(data, "ontology"), "ontology."),
        request=RequestMeta.from_json(req_obj(data, "request")),
        reason=reason,
        idempotency_key=opt_str(data, "idempotency_key"),
        changeset=changeset,
        changeset_hash=req_str(data, "changeset_hash"),
        resolution=resolution,
        revisions=tuple(
            revision_from_json(as_object(r, f"revisions[{i}]"), f"revisions[{i}].", opener)
            for i, r in enumerate(req_list(data, "revisions"))
        ),
        alias_allocations=tuple(allocations),
        prev_hash=req_str(data, "prev_hash"),
        hash=req_str(data, "hash"),
        blobs=_blobs(data),
        shredded=payload is None,
        sealed=sealed,
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
