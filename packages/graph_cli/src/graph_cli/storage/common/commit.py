"""Commit preconditions and tx sealing, shared by every backend (contracts B1.6-B1.8, B4, B13.4).

``AuthorizedTx`` arrives fully computed from services: ids, revisions,
timestamps, aliases. Storage assigns only ``prev_hash`` and ``hash`` and
checks structural invariants. A violation is a bug in the caller, so every
check raises ``internal.error`` with ``details.rule``. No ontology semantics
are checked here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Set
from dataclasses import replace

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import HASH_RE, CanonicalError, JsonValue
from graph_cli.kernel.changeset import OP_ACTIONS
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import ALIAS_RE, EntityId, EventId, GraphId
from graph_cli.kernel.ports.audit import AuditReceipt, AuditStream
from graph_cli.kernel.ports.storage import Head
from graph_cli.kernel.records import (
    RECORD_STATUSES,
    Revision,
    TxRecord,
    revision_content_hash,
    tx_to_json,
)
from graph_cli.storage.common.chain import CHAIN_KEYS, ChainLink, seal
from graph_cli.storage.common.view import ViewAt


def _violation(rule: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(
        "internal.error", f"commit refused: {message}", details={"rule": rule, **details}
    )


def check_receipt(
    receipt: AuditReceipt,
    *,
    event_id: EventId | None,
    stream: AuditStream,
    session_token: object,
    used_events: Set[EventId],
) -> None:
    """B5.7: a graph-stream receipt from this session, for this event, used once.

    ``event_id`` is the tx's event; maintenance effects pass ``None``.
    """
    if receipt.session_token is not session_token:
        raise _violation("receipt_session", "the audit receipt is from another session")
    if receipt.stream != stream:
        raise _violation("receipt_stream", "the audit receipt is not from the graph stream")
    if event_id is not None and receipt.event_id != event_id:
        raise _violation("receipt_event", "the audit receipt is for another event")
    if receipt.event_id in used_events:
        raise _violation("receipt_reused", "the audit receipt was already used")


def check_tx(
    tx: AuthorizedTx,
    *,
    graph_id: GraphId,
    head: Head,
    view: ViewAt,
    blob_exists: Callable[[str], bool],
) -> None:
    """Structural preconditions of a commit against the state at ``head`` (D6)."""
    if tx.graph_id != graph_id:
        raise _violation("graph_id", "the tx is for another graph")
    if view.seq != head.seq:
        raise _violation("view_seq", "the view is not at head")
    if tx.seq != head.seq + 1:
        raise _violation("seq", f"seq {tx.seq} does not follow head {head.seq}")
    if head.recorded_at is not None and tx.recorded_at < head.recorded_at:
        raise _violation("recorded_at", "recorded_at moves backwards (B4.3)")
    if not tx.revisions:
        raise _violation("empty", "the tx has no revisions")
    _check_decisions(tx)
    touched: dict[EntityId, Revision] = {}
    for revision in tx.revisions:
        if revision.id in touched:
            raise _violation("one_revision", "an entity has two revisions in one tx (B3.6)")
        touched[revision.id] = revision
        _check_revision(revision, tx=tx, previous=view.current(revision.id))
    for revision in tx.revisions:
        for edge in revision.edges:
            if edge.target not in touched and view.current(edge.target) is None:
                raise _violation(
                    "edge_target",
                    "an edge points at an entity that does not exist",
                    target=str(edge.target),
                )
    _check_aliases(tx, touched, view)
    _check_blobs(tx, blob_exists)


def _check_blobs(tx: AuthorizedTx, blob_exists: Callable[[str], bool]) -> None:
    """Every listed blob is stored before the commit; the list is sorted and unique."""
    if list(tx.blobs) != sorted(set(tx.blobs)):
        raise _violation("blobs", "the blob list is not sorted and unique")
    for digest in tx.blobs:
        if not HASH_RE.match(digest):
            raise _violation("blobs", "a blob hash is not sha256:<hex>", blob=digest)
        if not blob_exists(digest):
            raise _violation("blobs", "a listed blob is not stored", blob=digest)


def _check_decisions(tx: AuthorizedTx) -> None:
    """B6.10: an allow decision covers the action of every op (defence in depth)."""
    ops = tx.changeset.get("ops")
    if not isinstance(ops, list | tuple):
        raise _violation("changeset", "the change set has no ops")
    allowed = tx.allowed_actions()
    for op in ops:
        name = op.get("op") if isinstance(op, Mapping) else None
        action = OP_ACTIONS.get(name) if isinstance(name, str) else None
        if action is None:
            raise _violation("changeset", f"unknown op {name!r}")
        if action not in allowed:
            raise _violation("decisions", f"no allow decision for {action}", action=action)


def _check_revision(revision: Revision, *, tx: AuthorizedTx, previous: Revision | None) -> None:
    entity = str(revision.id)
    if (revision.tx_id, revision.tx_seq) != (tx.tx_id, tx.seq):
        raise _violation("revision_tx", "revision tx_id or tx_seq differs", entity=entity)
    if revision.recorded_at != tx.recorded_at:
        raise _violation("revision_recorded_at", "revision recorded_at differs", entity=entity)
    if revision.record_status not in RECORD_STATUSES:
        raise _violation("record_status", "unknown record_status (V.5)", entity=entity)
    if previous is None:
        if (revision.rev, revision.prev_rev) != (1, None):
            raise _violation("rev", "a new entity starts at rev 1", entity=entity)
        if revision.created_at != revision.recorded_at:
            raise _violation("created_at", "created_at of rev 1 is recorded_at", entity=entity)
    else:
        if (revision.rev, revision.prev_rev) != (previous.rev + 1, previous.rev):
            raise _violation("rev", "rev does not follow the current rev", entity=entity)
        if revision.created_at != previous.created_at:
            raise _violation("created_at", "created_at changed", entity=entity)
        if revision.type != previous.type:
            raise _violation("type_changed", "the entity type is immutable (B1.6)", entity=entity)
    if revision_content_hash(revision) != revision.content_hash:
        raise _violation("content_hash", "content_hash does not match (C.9)", entity=entity)
    edge_ids = [edge.edge_id for edge in revision.edges]
    if len(edge_ids) != len(set(edge_ids)):
        raise _violation("edge_ids", "edge ids repeat within a revision", entity=entity)


def _check_aliases(tx: AuthorizedTx, touched: dict[EntityId, Revision], view: ViewAt) -> None:
    allocated: dict[EntityId, str] = {}
    seen: set[str] = set()
    for entity, alias in tx.alias_allocations:
        if not ALIAS_RE.match(alias):
            raise _violation("alias_grammar", "alias does not match ALIAS_RE", alias=alias)
        if alias in seen or view.alias_bound(alias):
            raise _violation("alias_reused", "alias was allocated before (B1.8)", alias=alias)
        if entity in allocated:
            raise _violation("alias_twice", "two aliases for one entity in one tx", alias=alias)
        if entity not in touched or touched[entity].alias != alias:
            raise _violation(
                "alias_revision", "the allocation does not match the revision", alias=alias
            )
        seen.add(alias)
        allocated[entity] = alias
    for entity, revision in touched.items():
        previous = view.current(entity)
        before = previous.alias if previous is not None else None
        if revision.alias == before or entity in allocated:
            continue
        if revision.alias is None:
            raise _violation("alias_removed", "an alias cannot be removed", entity=str(entity))
        raise _violation("alias_unallocated", "a new alias needs an allocation", entity=str(entity))


def seal_tx(tx: AuthorizedTx, prev: ChainLink) -> tuple[TxRecord, bytes]:
    """Chain the tx after ``prev`` and return the record with its JSONL line (B4.4, B4.5)."""
    record = TxRecord(
        graph_id=tx.graph_id,
        tx_id=tx.tx_id,
        seq=tx.seq,
        recorded_at=tx.recorded_at,
        event_id=tx.event_id,
        actor=tx.actor,
        ontology=tx.ontology,
        request=tx.request,
        reason=tx.reason,
        idempotency_key=tx.idempotency_key,
        changeset=tx.changeset,
        changeset_hash=tx.changeset_hash,
        resolution=tx.resolution,
        revisions=tx.revisions,
        alias_allocations=tx.alias_allocations,
        prev_hash=prev.hash,
        hash="",
        blobs=tx.blobs,
    )
    body = {k: v for k, v in tx_to_json(record).items() if k not in CHAIN_KEYS}
    try:
        document, line = seal(body, prev)
    except CanonicalError as exc:
        raise _violation("canonical", f"the tx is not canonical JSON: {exc}") from None
    if document["seq"] != tx.seq:
        raise _violation("seq", "seq does not follow the chain")
    digest = document["hash"]
    assert isinstance(digest, str)
    return replace(record, hash=digest), line
