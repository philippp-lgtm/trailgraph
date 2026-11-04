"""Commit preconditions and tx sealing (plan D6; contracts B1.6-B1.8, B3.6, B4.4-B4.6, B5.7)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from support.memlog import MemLog
from support.txbuilder import TxDraft

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import canonical_bytes, hash_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, GraphId, TermRef
from graph_cli.kernel.ports.audit import AuditReceipt, AuditStream, graph_stream
from graph_cli.kernel.records import Revision, revision_content_hash, tx_to_json
from graph_cli.storage.common.chain import GENESIS, ChainLink, verify_line
from graph_cli.storage.common.commit import check_receipt, check_tx, seal_tx

TOKEN = object()


@pytest.fixture
def log() -> MemLog:
    return MemLog("inventory")


def _rule(log: MemLog, tx: AuthorizedTx) -> str:
    with pytest.raises(GraphCliError) as info:
        log.check(tx)
    assert info.value.code == "internal.error"
    rule = info.value.details["rule"]
    assert isinstance(rule, str)
    return rule


def _with_revision(tx: AuthorizedTx, index: int = 0, **changes: object) -> AuthorizedTx:
    revisions = list(tx.revisions)
    revision = replace(revisions[index], **changes)  # type: ignore[arg-type]
    if "content_hash" not in changes:
        revision = replace(revision, content_hash=revision_content_hash(revision))
    revisions[index] = revision
    return replace(tx, revisions=tuple(revisions))


def _seeded(log: MemLog) -> tuple[EntityId, EntityId]:
    """Two committed entities, the second linked to the first when the model allows it."""
    draft = log.draft()
    first = draft.create()
    second = draft.create()
    log.commit(draft.build())
    return first, second


def test_valid_tx_passes_and_seals(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    tx = draft.build()
    record, line = seal_tx(tx, GENESIS, log.codec)
    assert record.prev_hash == GENESIS.hash
    document = verify_line(line, GENESIS)
    assert document["hash"] == record.hash == hash_document(document)
    assert line == canonical_bytes(tx_to_json(record)) + b"\n"
    log.commit(tx)


def test_graph_id(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    other = GraphId(log.builder.ids.new_id())
    assert _rule(log, replace(draft.build(), graph_id=other)) == "graph_id"


def test_seq_follows_head(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    assert _rule(log, replace(draft.build(), seq=2)) == "seq"


def test_recorded_at_never_moves_backwards(log: MemLog) -> None:
    _seeded(log)
    draft = log.draft()
    draft.create()
    tx = draft.build()
    earlier = log.head.recorded_at - timedelta(milliseconds=1)  # type: ignore[operator]
    assert _rule(log, replace(tx, recorded_at=earlier)) == "recorded_at"


def test_empty_tx(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    assert _rule(log, replace(draft.build(), revisions=())) == "empty"


def test_one_revision_per_entity(log: MemLog) -> None:
    draft = log.draft()
    draft.create(alias=None)
    tx = draft.build()
    assert _rule(log, replace(tx, revisions=tx.revisions * 2)) == "one_revision"


@pytest.mark.parametrize(
    ("field", "rule"),
    [("tx_seq", "revision_tx"), ("tx_id", "revision_tx"), ("record_status", "record_status")],
)
def test_revision_tx_fields(log: MemLog, field: str, rule: str) -> None:
    draft = log.draft()
    draft.create()
    tx = draft.build()
    value: object = {"tx_seq": 7, "tx_id": tx.event_id, "record_status": "deleted"}[field]
    assert _rule(log, _with_revision(tx, **{field: value})) == rule


def test_revision_recorded_at(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    tx = draft.build()
    later = tx.recorded_at + timedelta(milliseconds=5)
    assert _rule(log, _with_revision(tx, recorded_at=later)) == "revision_recorded_at"


def test_new_entity_starts_at_rev_1(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    tx = draft.build()
    assert _rule(log, _with_revision(tx, rev=2, prev_rev=1)) == "rev"
    assert _rule(log, _with_revision(tx, created_at=tx.recorded_at - timedelta(1))) == "created_at"


def test_existing_entity_rev_created_at_and_type(log: MemLog) -> None:
    first, _ = _seeded(log)
    draft = log.draft()
    draft.update(first)
    tx = draft.build()
    (revision,) = tx.revisions
    assert revision.rev == 2 and revision.prev_rev == 1
    assert _rule(log, _with_revision(tx, rev=3, prev_rev=2)) == "rev"
    assert _rule(log, _with_revision(tx, created_at=tx.recorded_at)) == "created_at"
    other_type = TermRef(next(c for c in log.builder.concrete_classes if c != revision.type))
    assert _rule(log, _with_revision(tx, type=other_type)) == "type_changed"


def test_content_hash_is_recomputed(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    tx = draft.build()
    assert _rule(log, _with_revision(tx, content_hash="sha256:" + "1" * 64)) == "content_hash"


def _linked(log: MemLog) -> tuple[TxDraft, EntityId]:
    first, second = _seeded(log)
    draft = log.draft()
    assert draft.link(second) is not None or draft.link(first) is not None
    return draft, first


def test_edge_ids_unique_within_a_revision(log: MemLog) -> None:
    draft, _ = _linked(log)
    tx = draft.build()
    (revision,) = tx.revisions
    duplicated = (revision.edges[-1], revision.edges[-1])
    assert _rule(log, _with_revision(tx, edges=revision.edges + duplicated[:1])) == "edge_ids"


def test_edge_target_must_exist(log: MemLog) -> None:
    draft, _ = _linked(log)
    tx = draft.build()
    (revision,) = tx.revisions
    missing = EntityId(log.builder.ids.new_id())
    edge = replace(revision.edges[-1], target=missing, edge_id=EdgeId(log.builder.ids.new_id()))
    assert _rule(log, _with_revision(tx, edges=(*revision.edges, edge))) == "edge_target"


def test_edge_target_created_in_the_same_tx_exists(log: MemLog) -> None:
    draft = log.draft()
    target = draft.create()
    source = draft.create()
    draft.link(source, "core:source", target)
    log.commit(draft.build())


def _aliased(log: MemLog) -> tuple[AuthorizedTx, Revision]:
    draft = log.draft()
    cls = next(c for c in log.builder.concrete_classes if log.builder.prefix_of(c))
    draft.create(cls)
    tx = draft.build()
    (revision,) = tx.revisions
    assert revision.alias is not None and tx.alias_allocations
    return tx, revision


def test_alias_grammar(log: MemLog) -> None:
    tx, revision = _aliased(log)
    broken = replace(_with_revision(tx, alias="bad"), alias_allocations=((revision.id, "bad"),))
    assert _rule(log, broken) == "alias_grammar"


def test_alias_is_never_allocated_twice(log: MemLog) -> None:
    tx, revision = _aliased(log)
    log.commit(tx)
    draft = log.draft()
    other = draft.create(revision.type, alias=revision.alias)
    assert _rule(log, draft.build()) == "alias_reused"
    draft = log.draft()
    draft.create(revision.type, alias="ZZ-0001")
    second = draft.build()
    (fresh,) = second.revisions
    clash = _with_revision(second, alias=revision.alias)
    clash = replace(clash, alias_allocations=((fresh.id, revision.alias),))  # type: ignore[arg-type]
    assert _rule(log, clash) == "alias_reused"
    assert other != fresh.id


def test_alias_twice_in_one_tx(log: MemLog) -> None:
    tx, revision = _aliased(log)
    twice = replace(tx, alias_allocations=((revision.id, revision.alias), (revision.id, "ZZ-0009")))  # type: ignore[arg-type]
    assert _rule(log, twice) == "alias_twice"


def test_allocation_must_match_the_revision(log: MemLog) -> None:
    tx, revision = _aliased(log)
    assert _rule(log, replace(tx, alias_allocations=((revision.id, "ZZ-0001"),))) == (
        "alias_revision"
    )


def test_alias_changes_need_an_allocation(log: MemLog) -> None:
    tx, revision = _aliased(log)
    assert _rule(log, replace(tx, alias_allocations=())) == "alias_unallocated"
    log.commit(tx)
    draft = log.draft()
    draft.update(revision.id)
    update = draft.build()
    assert _rule(log, _with_revision(update, alias=None)) == "alias_removed"
    assert _rule(log, _with_revision(update, alias="ZZ-0002")) == "alias_unallocated"


def test_rename_keeps_old_alias_bound(log: MemLog) -> None:
    tx, revision = _aliased(log)
    log.commit(tx)
    draft = log.draft()
    new_alias = draft.set_alias(revision.id)
    log.commit(draft.build())
    view = log.at()
    assert view.resolve_alias(new_alias) == (revision.id, True)
    assert view.resolve_alias(revision.alias) == (revision.id, False)  # type: ignore[arg-type]
    draft = log.draft()
    draft.set_alias(revision.id, revision.alias)
    assert _rule(log, draft.build()) == "alias_reused"


def test_non_canonical_changeset_is_refused(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    tx = replace(draft.build(), changeset={"ops": [], "ratio": 0.5})  # type: ignore[dict-item]
    with pytest.raises(GraphCliError) as info:
        seal_tx(tx, GENESIS, log.codec)
    assert info.value.details["rule"] == "canonical"


def test_seal_continues_the_given_link(log: MemLog) -> None:
    _seeded(log)
    draft = log.draft()
    draft.create()
    record, line = seal_tx(draft.build(), ChainLink(log.head.seq, log.head.hash), log.codec)
    assert record.prev_hash == log.head.hash
    verify_line(line, ChainLink(log.head.seq, log.head.hash))


class TestReceipt:
    stream = graph_stream("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a")

    def _tx(self, log: MemLog) -> AuthorizedTx:
        draft = log.draft()
        draft.create()
        return draft.build()

    def _receipt(self, tx: AuthorizedTx, **changes: object) -> AuditReceipt:
        receipt = AuditReceipt(self.stream, tx.event_id, 1, "sha256:" + "a" * 64, TOKEN)
        return replace(receipt, **changes)  # type: ignore[arg-type]

    def _rule(self, tx: AuthorizedTx, receipt: AuditReceipt, used: set[EventId]) -> str:
        with pytest.raises(GraphCliError) as info:
            check_receipt(
                receipt,
                event_id=tx.event_id,
                stream=self.stream,
                session_token=TOKEN,
                used_events=used,
            )
        assert info.value.code == "internal.error"
        return str(info.value.details["rule"])

    def test_valid(self, log: MemLog) -> None:
        tx = self._tx(log)
        check_receipt(
            self._receipt(tx),
            event_id=tx.event_id,
            stream=self.stream,
            session_token=TOKEN,
            used_events=set(),
        )

    def test_rules(self, log: MemLog) -> None:
        tx = self._tx(log)
        other_event = EventId(log.builder.ids.new_id())
        cases = {
            "receipt_session": self._receipt(tx, session_token=object()),
            "receipt_stream": self._receipt(tx, stream=AuditStream("installation")),
            "receipt_event": self._receipt(tx, event_id=other_event),
        }
        for rule, receipt in cases.items():
            assert self._rule(tx, receipt, set()) == rule
        assert self._rule(tx, self._receipt(tx), {tx.event_id}) == "receipt_reused"


def test_check_tx_needs_the_view_at_head(log: MemLog) -> None:
    _seeded(log)
    draft = log.draft()
    draft.create()
    with pytest.raises(GraphCliError) as info:
        check_tx(
            draft.build(),
            graph_id=log.graph_id,
            head=log.head,
            view=log.at(0),
            blob_exists=log.blobs.__contains__,
            codec=log.codec,
        )
    assert info.value.details["rule"] == "view_seq"


def test_listed_blobs_must_be_stored_sorted_and_unique(log: MemLog) -> None:
    _seeded(log)
    first, second = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    draft = log.draft()
    draft.create()
    tx = draft.build()
    for blobs in ((second, first), (first, first), (first,), ("sha256:" + "A" * 64,)):
        with pytest.raises(GraphCliError) as info:
            log.check(replace(tx, blobs=blobs))
        assert info.value.details["rule"] == "blobs", blobs
    log.blobs.update({first, second})
    record = log.commit(replace(tx, blobs=(first, second)))
    assert record.blobs == (first, second)
    assert b'"blobs":["sha256:' in log.lines[-1]
