"""``graph-admin graph purge``: erase entities of a sealed graph for good (T16).

Under the graph lock: resolve the entities, collect what the purge destroys
(``MaintenanceSession.purge_targets``: the keys of the entities, of every tx
with a revision of one, and with files those of the stored files only they
name), append a ``maint.purge`` intent naming entities, keys and stored files,
then let storage commit a tx that retracts every entity still holding content
with empty values (the intent is its event), shred the keys and delete the
stored files. An installation event anchors the intent. Afterwards the
entities read as ``shredded``; neither the data root nor a backup of it shows
their values again without a copy of the key store from before the purge.

Running the same purge again completes an interrupted one: entities whose
content is gone need no tx, keys shredded already stay as they are and their
first purge event stays recorded. A purge with nothing left to do writes
nothing; a dry run reports what would go. The plaintext copies ``file path``
left in this process's state root are removed too; copies on other machines
expire unused (``catalog.file_copies``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from graph_cli.audit import events
from graph_cli.catalog.file_copies import FileCopyDir
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, RetractOp, changeset_hash, changeset_to_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, TxId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import Head, MaintenanceSession, PurgeTargets
from graph_cli.kernel.records import RequestMeta, Revision, revision_content_hash
from graph_cli.kernel.sealing import KeyRing
from graph_cli.services.refs import resolve_ref
from graph_cli_admin.services_admin.context import AdminContext


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


def purge(
    ctx: AdminContext,
    *,
    graph_ref: str,
    refs: Sequence[str],
    with_files: bool,
    reason: str,
    dry_run: bool,
) -> PurgeOutcome:
    entry = ctx.resolve(graph_ref, "graph purge")
    if not entry.sealed:
        raise GraphCliError(
            "admin.graph_not_sealed",
            f"{entry.name} is not sealed: its values are stored in the clear and cannot be"
            " erased by a purge",
            details={"graph": entry.name},
        )
    if not refs:
        raise GraphCliError("usage.invalid_argument", "name at least one entity to purge")
    args: dict[str, JsonValue] = {
        "graph": str(entry.id),
        "entities": list(refs),
        "with_files": with_files,
        "dry_run": dry_run,
    }
    meta = RequestMeta.of("graph purge", args)
    decisions = ctx.authorize(
        [("maint.purge", f"graph:{entry.id}")], request=meta, graph=entry.id, reason=reason
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        entities = _resolve(session, refs)
        targets = session.purge_targets(entities, with_files=with_files)
        snapshot = session.snapshot()
        head = session.head()
        erase = [
            revision
            for revision in (snapshot.get(entity) for entity in entities)
            if revision is not None and _holds_content(revision)
        ]
        data = _data(entry, targets, erase, dry_run=dry_run)
        if dry_run or not (erase or targets.pending or targets.blobs):
            return PurgeOutcome(entry, data, head.seq)
        ring = ctx.maintenance.store(entry.id).key_ring()
        decisions = ctx.current_decisions(entry, decisions, meta, reason=reason)
        event_id = ctx.event_id()
        tx = None
        if erase:
            tx = _retracting_tx(ctx, entry, ring, erase, decisions, meta, head, event_id, reason)
            kid = ring.tx_key(tx.tx_id)  # minted for its changeset hash: shredded with the rest
            assert kid is not None
            targets = replace(targets, tx_keys=tuple(sorted({*targets.tx_keys, kid})))
            data = _data(entry, targets, erase, dry_run=dry_run)
        change = events.purged(targets.entities, targets.kids, targets.blobs, tx)
        receipt = ctx.intent(
            session, entry, decisions, meta, change, reason=reason, event_id=event_id
        )
        session.purge(tx, targets, receipt)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        seq = session.head().seq
    copies = FileCopyDir(ctx.roots.state_root / "files", ctx.clock)
    data["copies_removed"] = copies.drop(str(entry.id), targets.contents)
    data["purged"] = True
    return PurgeOutcome(entry, data, seq)


def _resolve(session: MaintenanceSession, refs: Sequence[str]) -> list[EntityId]:
    """The entities ``refs`` name, each once; every one must exist."""
    snapshot = session.snapshot()
    found: dict[EntityId, None] = {}
    for ref in refs:
        entity, _ = resolve_ref(ref, snapshot)
        if snapshot.get(entity) is None:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        found[entity] = None
    return list(found)


def _holds_content(revision: Revision) -> bool:
    """Whether the current revision still holds values a retraction would empty."""
    if revision.shredded:
        return False
    return (
        revision.record_status != "retracted"
        or bool(revision.attrs)
        or revision.valid_from is not None
        or revision.valid_to is not None
        or any(edge.qualifiers for edge in revision.edges)
    )


def _retracting_tx(
    ctx: AdminContext,
    entry: GraphEntry,
    ring: KeyRing,
    current: Sequence[Revision],
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    head: Head,
    event_id: EventId,
    reason: str,
) -> AuthorizedTx:
    """The tx that retracts ``current`` with empty values; edges stay without qualifiers."""
    now = ctx.clock.now()
    recorded_at = now if head.recorded_at is None else max(now, head.recorded_at)
    tx_id = TxId(ctx.ids.new_id())
    seq = head.seq + 1
    revisions = tuple(
        _erased(revision, tx_id, seq, recorded_at, entry.pin.version, ring)
        for revision in sorted(current, key=lambda r: r.id)
    )
    cs = ChangeSet(ops=tuple(RetractOp(id=r.id) for r in revisions), reason=reason)
    return AuthorizedTx(
        graph_id=entry.id,
        tx_id=tx_id,
        event_id=event_id,
        seq=seq,
        recorded_at=recorded_at,
        actor=ctx.actor,
        decisions=decisions,
        changeset=changeset_to_json(cs),
        changeset_hash=changeset_hash(cs, ring.mac_key(ring.tx_key(tx_id))),
        resolution={},
        revisions=revisions,
        alias_allocations=(),
        ontology=entry.pin,
        reason=reason,
        idempotency_key=None,
        request=meta,
    )


def _erased(
    current: Revision, tx_id: TxId, seq: int, recorded_at: datetime, version: str, ring: KeyRing
) -> Revision:
    erased = replace(
        current,
        rev=current.rev + 1,
        prev_rev=current.rev,
        record_status="retracted",
        attrs={},
        edges=tuple(replace(edge, qualifiers={}) for edge in current.edges),
        valid_from=None,
        valid_to=None,
        recorded_at=recorded_at,
        tx_id=tx_id,
        tx_seq=seq,
        ontology_version=version,
        content_hash="",
        blobs={},
        sealed=None,
    )
    mac = ring.mac_key(ring.entity_key(current.id))
    return replace(erased, content_hash=revision_content_hash(erased, mac))


def _data(
    entry: GraphEntry, targets: PurgeTargets, erase: Sequence[Revision], *, dry_run: bool
) -> dict[str, JsonValue]:
    return {
        "graph_id": str(entry.id),
        "entities": [str(entity) for entity in targets.entities],
        "retracted": sorted(str(revision.id) for revision in erase),
        "keys": {
            "entity": len(targets.entity_keys),
            "tx": len(targets.tx_keys),
            "file": len(targets.file_keys),
            "pending": len(targets.pending),
        },
        "blobs_removed": events.blob_list(targets.blobs) if targets.blobs else [],
        "copies_removed": 0,
        "dry_run": dry_run,
        "purged": False,
    }
