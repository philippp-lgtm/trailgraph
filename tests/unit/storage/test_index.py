"""The disposable index (contracts B4.1, B13.7, W.15; plan "Disposable index").

Every answer through the index must equal the replay answer, at every seq
the index can serve, whether the index is current or behind the log.
"""

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from support.fixtures import TOY_ONTOLOGIES
from support.stores import ALL_STATUSES, StoreEnv
from support.trees import tree_hash

from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.query_ast import NeighborQuery, Query
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.index import (
    IndexedView,
    IndexReader,
    IndexUnusable,
    TxSpan,
    index_difference,
)
from graph_cli.storage.files.store import FileStore

EVERYTHING = Query(record_status=ALL_STATUSES, limit=10**9)


def files_env(tmp_path: Path, ontology: str = "inventory", seed: int = 0) -> StoreEnv:
    return StoreEnv("files", ontology, tmp_path, seed=seed)


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def reader_of(env: StoreEnv, *, use_index: bool = True) -> FileStore:
    """A fresh store instance on the same graph, as a new `graph` process would open it."""
    return FileStore(store_of(env).layout.root, env.graph_id, use_index=use_index)


def index_seq(env: StoreEnv) -> int:
    reader = IndexReader.open(store_of(env).layout.index)
    assert reader is not None
    try:
        return reader.meta.seq
    finally:
        reader.close()


def assert_same_answers(indexed: FileStore, replay: FileStore, seq: int) -> None:
    """Every scan of the snapshot at ``seq`` answers the same with and without the index."""
    left, right = indexed.snapshot(at_seq=seq), replay.snapshot(at_seq=seq)
    assert left.warnings == right.warnings
    everything = right.query(EVERYTHING).items
    assert left.query(EVERYTHING).items == everything
    for kind in sorted({r.type for r in everything}):
        q = Query(types=(kind,), record_status=ALL_STATUSES, limit=10**9)
        assert left.query(q).items == right.query(q).items
    aliases = set()
    for tx_seq in range(1, seq + 1):
        tx = right.tx(tx_seq)
        assert left.tx(tx_seq) == tx
        assert tx is not None
        aliases.update(alias for _, alias in tx.alias_allocations)
    for alias in [*sorted(aliases), "NONE-0001"]:
        assert left.resolve_alias(alias) == right.resolve_alias(alias)
    for revision in everything:
        entity = revision.id
        assert left.incoming(entity) == right.incoming(entity)
        for edge in revision.edges:
            assert left.incoming(edge.target, edge.rel) == right.incoming(edge.target, edge.rel)
        q = NeighborQuery(start=entity, rels=None, direction="both", depth=2)
        assert left.neighbors(q) == right.neighbors(q)
        assert left.match_suffix(entity.hex[-3:]) == right.match_suffix(entity.hex[-3:])
    assert left.match_suffix("0") == right.match_suffix("0")


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
@pytest.mark.parametrize("seed", [1, 2])
def test_index_answers_like_the_replay_at_every_seq(
    tmp_path: Path, ontology: str, seed: int
) -> None:
    env = files_env(tmp_path, ontology, seed)
    env.random(12)
    store = store_of(env)
    store.use_index = False  # an older writer: the index stays behind
    env.random(5)
    assert index_seq(env) == 12
    replay = reader_of(env, use_index=False)
    indexed = reader_of(env)
    for seq in range(0, 18):
        assert_same_answers(indexed, replay, seq)
    store.use_index = True
    env.random(3)  # the next writer catches up incrementally
    assert index_seq(env) == 20
    for seq in (12, 19, 20):
        assert_same_answers(reader_of(env), reader_of(env, use_index=False), seq)
    assert difference(env) is None


def spans_of(env: StoreEnv) -> tuple[GraphView, list[TxSpan]]:
    store = reader_of(env)
    return store._cache.spans_through(store.head().seq)


def difference(env: StoreEnv) -> str | None:
    """What `verify` reports about the index of ``env``."""
    store = store_of(env)
    return index_difference(store.layout.index, env.graph_id, *spans_of(env), store.txlog)


def test_reads_use_the_index_without_replaying(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(10)
    reader = reader_of(env)
    snapshot = reader.snapshot()
    assert isinstance(snapshot._index, IndexedView)
    snapshot.query(EVERYTHING)
    snapshot.resolve_alias("NONE-0001")
    snapshot.tx(3)
    assert reader._cache.view_through(0).seq == 0


def test_a_missing_index_is_silent(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(6)
    shutil.rmtree(store_of(env).layout.index.parent)
    snapshot = reader_of(env).snapshot()
    assert snapshot.warnings == ()
    assert (
        snapshot.query(EVERYTHING).items
        == reader_of(env, use_index=False).snapshot().query(EVERYTHING).items
    )
    env.random(1)  # the next writer builds it again
    assert index_seq(env) == 7


def _stale(env: StoreEnv) -> str:
    snapshot = reader_of(env).snapshot()
    (warning,) = snapshot.warnings
    assert warning.code == "storage.index_stale"
    expected = reader_of(env, use_index=False).snapshot().query(EVERYTHING).items
    assert snapshot.query(EVERYTHING).items == expected
    return warning.message


def test_an_index_of_another_graph_is_stale(tmp_path: Path) -> None:
    env = files_env(tmp_path / "a")
    env.random(4)
    other = files_env(tmp_path / "b", seed=7)
    other.random(4)
    shutil.copyfile(store_of(other).layout.index, store_of(env).layout.index)
    assert "another graph" in _stale(env)
    env.random(1)  # the writer rebuilds it
    assert reader_of(env).snapshot().warnings == ()


def test_garbage_is_stale(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(4)
    store_of(env).layout.index.write_bytes(b"not a database")
    assert "unreadable" in _stale(env)


def test_an_index_ahead_of_the_log_is_stale(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(3)
    layout = store_of(env).layout
    saved = tmp_path / "saved"
    shutil.copytree(layout.txlog, saved / "txlog")
    shutil.copyfile(layout.head, saved / "head.json")
    env.random(2)
    shutil.rmtree(layout.txlog)
    shutil.copytree(saved / "txlog", layout.txlog)
    shutil.copyfile(saved / "head.json", layout.head)
    assert "ahead" in _stale(env)


def test_an_index_bound_elsewhere_is_stale(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(4)
    path = store_of(env).layout.index
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE meta SET value = 'sha256:' || substr(value, 8, 63) || '0' WHERE key = 'hash'"
        )
    assert "does not match" in _stale(env)


def test_a_damaged_row_falls_back_to_the_replay(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(6)
    with sqlite3.connect(store_of(env).layout.index) as db:
        db.execute("UPDATE entity SET body = '{' WHERE id = (SELECT id FROM entity LIMIT 1)")
    snapshot = reader_of(env).snapshot()
    assert snapshot.warnings == ()  # the bind check passes; the row fails later
    expected = reader_of(env, use_index=False).snapshot().query(EVERYTHING).items
    assert snapshot.query(EVERYTHING).items == expected
    assert snapshot._index is None
    assert difference(env)


def test_reads_write_nothing_with_an_index(tmp_path: Path) -> None:
    """B13.7: an immutable read leaves no journal, lock or other side file."""
    env = files_env(tmp_path)
    env.random(8)
    root = store_of(env).layout.root
    before = tree_hash(root)
    snapshot = reader_of(env).snapshot()
    for revision in snapshot.query(EVERYTHING).items:
        snapshot.incoming(revision.id)
        snapshot.tx(revision.tx_seq)
    assert sorted(os.listdir(store_of(env).layout.index.parent)) == ["index.sqlite"]
    assert tree_hash(root) == before


def test_a_failed_update_keeps_the_old_index(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(3)
    env.faults.armed = {CrashPoint.INDEX_BEFORE_RENAME}
    _, result = env.write(lambda d: d.create())
    env.faults.armed = set()
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    assert "index" in result.warnings[0].message
    assert index_seq(env) == 3
    assert_same_answers(reader_of(env), reader_of(env, use_index=False), 4)
    env.write(lambda d: d.create())  # the leftover temp file is replaced
    assert index_seq(env) == 5
    assert sorted(os.listdir(store_of(env).layout.index.parent)) == ["index.sqlite"]


def test_an_earlier_snapshot_replays_without_a_warning(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(5)
    snapshot = reader_of(env).snapshot(at_seq=2)
    assert snapshot.warnings == () and snapshot._index is None
    assert len(snapshot.query(EVERYTHING).items) > 0


def test_a_tx_line_moved_under_the_index_is_refused(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(3)
    with sqlite3.connect(store_of(env).layout.index) as db:
        db.execute("UPDATE tx SET start = start + 1 WHERE seq = 2")
    reader = IndexReader.open(store_of(env).layout.index)
    assert reader is not None
    with pytest.raises(IndexUnusable, match="not where the index says"):
        reader.tx(2, store_of(env).txlog)
    reader.close()
    assert reader_of(env).snapshot().tx(2) == reader_of(env, use_index=False).snapshot().tx(2)


def test_entity_ids_are_ordered_like_uuids(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(15)
    reader = IndexReader.open(store_of(env).layout.index)
    assert reader is not None
    ids: list[EntityId] = reader.entity_ids()
    reader.close()
    assert ids == sorted(ids)
