"""Tx-log replay view (contracts B1.8, B1.9, B3.12, B4.6, B4.9, B4.10, B8.8 seam)."""

import pytest
from support.fixtures import TOY_ONTOLOGIES
from support.memlog import MemLog

from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import Revision
from graph_cli.storage.common.view import GraphView, alias_parts


def _state(log: MemLog) -> dict[EntityId, Revision]:
    view = log.at()
    return {entity: view.current(entity) for entity in view.entity_ids()}  # type: ignore[misc]


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_replay_at_every_seq_equals_the_state_after_n_txs(ontology: str) -> None:
    log = MemLog(ontology, seed=3)
    states = [_state(log)]
    for _ in range(40):
        draft = log.draft()
        log.builder.random_tx(draft)
        log.commit(draft.build())
        states.append(_state(log))
    for seq, expected in enumerate(states):
        view = log.at(seq)
        assert view.seq == seq
        assert {e: view.current(e) for e in view.entity_ids()} == expected
        for entity, revision in expected.items():
            assert [m.rev for m in view.history(entity)] == list(range(1, revision.rev + 1))
            assert view.revision(entity, revision.rev) == revision
            assert view.revision(entity, revision.rev + 1) is None


def test_fresh_view_replays_the_same_lines() -> None:
    log = MemLog("notes_and_sources", seed=1)
    for _ in range(10):
        draft = log.draft()
        log.builder.random_tx(draft)
        log.commit(draft.build())
    other = GraphView()
    for seq in range(1, log.head.seq + 1):
        other.apply(log.at().tx(seq))
    assert other.at().entity_ids() == log.at().entity_ids()
    with pytest.raises(ValueError, match="does not follow"):
        other.apply(log.at().tx(1))


def test_at_outside_the_view() -> None:
    view = GraphView()
    assert view.at().seq == 0
    with pytest.raises(ValueError, match="outside"):
        view.at(1)


def test_incoming_follows_current_revisions_only() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    target, source = draft.create(), draft.create()
    log.commit(draft.build())
    draft = log.draft()
    edge_id = draft.link(source, "core:source", target)
    log.commit(draft.build())
    linked_at = log.head.seq
    (incoming,) = log.at().incoming(target)
    assert incoming[0] == source and incoming[1].edge_id == edge_id
    assert log.at().incoming(target, "core:supersedes") == []  # type: ignore[arg-type]
    draft = log.draft()
    draft.unlink(source, edge_id)
    log.commit(draft.build())
    assert log.at().incoming(target) == []
    assert len(log.at(linked_at).incoming(target)) == 1


def test_incoming_is_sorted_by_source_then_edge() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    target = draft.create()
    sources = [draft.create() for _ in range(3)]
    for source in reversed(sources):
        draft.link(source, "core:source", target)
        draft.link(source, "core:source", target)
    log.commit(draft.build())
    found = log.at().incoming(target)
    assert found == sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))
    assert len(found) == 6


def test_alias_counters_are_the_max_allocated_number() -> None:
    log = MemLog("inventory")
    cls = next(c for c in log.builder.concrete_classes if log.builder.prefix_of(c))
    prefix = log.builder.prefix_of(cls)
    draft = log.draft()
    draft.create(cls)
    draft.create(cls)
    log.commit(draft.build())
    assert log.at().alias_counters()[prefix] == 2  # type: ignore[index]
    draft = log.draft()
    draft.create(cls, alias=f"{prefix}-0040")  # explicit alias above the counter (B1.8)
    log.commit(draft.build())
    assert log.at().alias_counters()[prefix] == 40  # type: ignore[index]
    assert log.at(1).alias_counters()[prefix] == 2  # type: ignore[index]
    assert log.at(0).alias_counters() == {}
    draft = log.draft()
    created = draft.create(cls)
    log.commit(draft.build())
    assert log.at().current(created).alias == f"{prefix}-0041"  # type: ignore[union-attr]


def test_resolve_alias_respects_seq() -> None:
    log = MemLog("inventory")
    cls = next(c for c in log.builder.concrete_classes if log.builder.prefix_of(c))
    draft = log.draft()
    entity = draft.create(cls)
    log.commit(draft.build())
    alias = log.at().current(entity).alias  # type: ignore[union-attr]
    assert alias is not None
    assert log.at().resolve_alias(alias) == (entity, True)
    assert log.at(0).resolve_alias(alias) is None
    assert log.at().resolve_alias("NOPE-0001") is None


def test_match_suffix_uses_the_random_tail() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    created = [draft.create() for _ in range(5)]
    log.commit(draft.build())
    view = log.at()
    for entity in created:
        assert view.match_suffix(entity.hex[-12:]) == [entity]
    everything = view.match_suffix("")
    assert everything == sorted(created)
    assert view.match_suffix("", limit=2) == sorted(created)[:2]
    assert log.at(0).match_suffix(created[0].hex[-12:]) == []


def test_idempotency_scope_is_graph_and_principal() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    draft.create()
    record = log.commit(draft.build(idempotency_key="k-1", principal="test:1"))
    view = log.at()
    assert view.lookup_idempotency("k-1", "test:1") == record
    assert view.lookup_idempotency("k-1", "test:2") is None
    assert view.lookup_idempotency("k-2", "test:1") is None
    assert log.at(0).lookup_idempotency("k-1", "test:1") is None


def test_txs_since_pages_in_order() -> None:
    log = MemLog("inventory")
    for _ in range(7):
        draft = log.draft()
        draft.create()
        log.commit(draft.build())
    view = log.at()
    assert [tx.seq for tx in view.txs_since(0, limit=3)] == [1, 2, 3]
    assert [tx.seq for tx in view.txs_since(3, limit=3)] == [4, 5, 6]
    assert [tx.seq for tx in view.txs_since(6, limit=3)] == [7]
    assert view.txs_since(7, limit=3) == ()
    assert [tx.seq for tx in log.at(4).txs_since(2, limit=10)] == [3, 4]


def test_get_many_skips_unknown_ids() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    entity = draft.create()
    log.commit(draft.build())
    unknown = EntityId(log.builder.ids.new_id())
    assert set(log.at().get_many([entity, unknown])) == {entity}


def test_alias_parts() -> None:
    assert alias_parts("ITEM-0007") == ("ITEM", 7)
    assert alias_parts("A1-12345") == ("A1", 12345)
