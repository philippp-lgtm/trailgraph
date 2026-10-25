"""Writers on the checked index (plan "Incremental writer checks", contracts B4.11, W.15).

A writer reads the index once the log digest up to its position matches and
verifies only the txs after it; anything in doubt falls back to the full
replay, and a full replay happens at least every ``full_check_every`` txs.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from support.fixtures import TOY_ONTOLOGIES
from support.stores import ALL_STATUSES, StoreEnv, snapshot_state

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.files.index import (
    INDEX_FORMAT,
    IndexedView,
    IndexMeta,
    IndexReader,
    index_difference,
)
from graph_cli.storage.files.store import FileStore

EVERYTHING = Query(record_status=ALL_STATUSES, limit=10**9)


def files_env(tmp_path: Path, ontology: str = "inventory", seed: int = 0) -> StoreEnv:
    return StoreEnv("files", ontology, tmp_path, seed=seed)


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def fresh(env: StoreEnv, **options: int) -> FileStore:
    """A new store instance for the next write, as a new `graph` process opens it."""
    old = store_of(env)
    env.store = FileStore(
        old.layout.root, env.graph_id, syncer=env.syncer, faults=env.faults, **options
    )
    return env.store


def replayed(store: FileStore) -> int:
    """How far this instance replayed the log (0: not at all)."""
    return store._cache._view.seq


def meta_of(env: StoreEnv) -> IndexMeta:
    reader = IndexReader.open(store_of(env).layout.index)
    assert reader is not None
    try:
        return reader.meta
    finally:
        reader.close()


def difference(env: StoreEnv) -> str | None:
    store = FileStore(store_of(env).layout.root, env.graph_id)
    view, spans = store._cache.spans_through(store.head().seq)
    return index_difference(store.layout.index, env.graph_id, view, spans, store.txlog)


def edit_index(env: StoreEnv, sql: str, *args: object) -> None:
    with sqlite3.connect(store_of(env).layout.index) as db:
        db.execute(sql, args)


def active_entity(env: StoreEnv) -> EntityId:
    snapshot = FileStore(store_of(env).layout.root, env.graph_id, use_index=False).snapshot()
    return next(r.id for r in snapshot.query(EVERYTHING).items if r.record_status == "active")


def test_a_writer_reads_the_checked_index_without_replaying(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(8)
    store = fresh(env)
    _, result = env.write(lambda d: d.create())
    assert result.warnings == ()
    assert replayed(store) == 0
    meta = meta_of(env)
    assert (meta.format, meta.seq, meta.full_verified_seq) == (INDEX_FORMAT, 9, 1)
    assert difference(env) is None


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
@pytest.mark.parametrize("seed", [1, 2])
def test_the_writer_view_answers_like_the_replay(tmp_path: Path, ontology: str, seed: int) -> None:
    env = files_env(tmp_path, ontology, seed)
    env.random(10)
    keys = [("key-1", "test:1"), ("key-2", "test:2")]
    for key, principal in keys:
        env.write(env.builder.random_tx, idempotency_key=key, principal=principal)
    store_of(env).use_index = False  # an older writer: the index stays behind
    env.random(4)
    env.write(env.builder.random_tx, idempotency_key="key-3", principal="test:1")
    keys.append(("key-3", "test:1"))
    store = fresh(env)
    head = store.head().seq
    replay = FileStore(store.layout.root, env.graph_id, use_index=False).view_at(head)
    with store.session(timeout_s=5) as session:
        writer = session._writer
        assert writer.index is not None and writer.seq == head
        everything = list(replay.current_revisions())
        assert list(writer.current_revisions()) == everything
        assert writer.alias_counters() == replay.alias_counters()
        for key, principal in [*keys, ("key-1", "test:2"), ("none", "test:1")]:
            found = writer.lookup_idempotency(key, principal)
            assert found == replay.lookup_idempotency(key, principal)
        aliases = set()
        for seq in range(1, head + 1):
            assert writer.tx(seq) == replay.tx(seq)
            aliases.update(alias for _, alias in replay.tx(seq).alias_allocations)
        for alias in [*sorted(aliases), "NONE-0001"]:
            assert writer.resolve_alias(alias) == replay.resolve_alias(alias)
            assert writer.alias_bound(alias) == replay.alias_bound(alias)
        for revision in everything:
            entity = revision.id
            assert writer.current(entity) == replay.current(entity)
            assert writer.incoming(entity) == replay.incoming(entity)
            assert writer.history(entity) == replay.history(entity)
            for rev in range(1, revision.rev + 2):
                assert writer.revision(entity, rev) == replay.revision(entity, rev)
            assert writer.match_suffix(entity.hex[-3:]) == replay.match_suffix(entity.hex[-3:])
        assert writer.fell_back is None
    assert replayed(store) == 0


def test_several_commits_in_one_session_extend_the_index(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(5)
    store = fresh(env)
    with store.session(timeout_s=5) as session:
        for _ in range(3):
            draft = env.builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            env.builder.random_tx(draft)
            tx = draft.build()
            receipt = session.append_audit(env.intent(tx, session.snapshot()))
            assert session.commit(tx, receipt).warnings == ()
            assert meta_of(env).seq == tx.seq
    assert replayed(store) == 0
    assert difference(env) is None


def test_damage_before_the_index_still_stops_writers(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(6)
    segment = store_of(env).txlog.path(store_of(env).txlog.segments()[0][1])
    lines = segment.read_bytes().split(b"\n")
    at = lines[1].rindex(b'"hash":"sha256:') + len(b'"hash":"sha256:')
    flipped = b"1" if lines[1][at : at + 1] == b"0" else b"0"
    lines[1] = lines[1][:at] + flipped + lines[1][at + 1 :]
    segment.write_bytes(b"\n".join(lines))
    store = fresh(env)
    with pytest.raises(GraphCliError) as caught:
        env.write(lambda d: d.create())
    assert caught.value.code == "backend.corrupt"
    assert replayed(store) == 1  # the full replay found it


def test_a_wrong_log_digest_rebuilds_the_index(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(6)
    edit_index(env, "UPDATE meta SET value = ? WHERE key = 'log_digest'", '[["x",0,"0"]]')
    store = fresh(env)
    _, result = env.write(lambda d: d.create())
    assert [w.code for w in result.warnings] == ["storage.index_stale"]
    assert "log digest" in result.warnings[0].message
    assert replayed(store) == 7
    assert meta_of(env).full_verified_seq == 7
    assert difference(env) is None


def _edit_row(env: StoreEnv, entity: EntityId, edit: dict[str, object]) -> None:
    with sqlite3.connect(store_of(env).layout.index) as db:
        (body,) = db.execute("SELECT body FROM entity WHERE id = ?", (entity.bytes,)).fetchone()
        document = json.loads(body)
        for key, value in edit.items():
            if isinstance(value, dict):
                document[key] = {**document[key], **value}
            else:
                document[key] = value
        db.execute("UPDATE entity SET body = ? WHERE id = ?", (json.dumps(document), entity.bytes))


def test_an_edited_row_is_not_copied_into_a_write(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(6)
    entity = active_entity(env)
    _edit_row(env, entity, {"attrs": {"tampered": "yes"}})
    store = fresh(env)
    _, result = env.write(lambda d: d.update(entity))
    assert [w.code for w in result.warnings] == ["storage.index_stale"]
    assert "content hash" in result.warnings[0].message
    written = FileStore(store.layout.root, env.graph_id, use_index=False).snapshot().get(entity)
    assert written is not None and "tampered" not in written.attrs
    assert difference(env) is None


def test_an_edited_rev_is_refused_and_the_index_rebuilt(tmp_path: Path) -> None:
    """A field outside the content hash: the tx line disagrees at commit time."""
    env = files_env(tmp_path)
    env.random(6)
    entity = active_entity(env)
    current = FileStore(store_of(env).layout.root, env.graph_id).snapshot().get(entity)
    assert current is not None
    _edit_row(env, entity, {"rev": current.rev + 5, "prev_rev": current.rev + 4})
    fresh(env)
    with pytest.raises(GraphCliError) as caught:
        env.write(lambda d: d.update(entity))
    assert caught.value.code == "internal.error"
    assert caught.value.details["rule"] == "rev"
    assert difference(env) is None  # rebuilt when the session closed
    fresh(env)
    _, result = env.write(lambda d: d.update(entity))
    assert result.warnings == ()


def test_a_full_replay_at_least_every_n_txs(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    seen = []
    for _ in range(7):
        store = fresh(env, full_check_every=3)
        env.write(lambda d: d.create())
        meta = meta_of(env)
        seen.append((meta.seq, meta.full_verified_seq, replayed(store) > 0))
    assert seen == [
        (1, 1, True),  # no index yet
        (2, 1, False),
        (3, 1, False),
        (4, 1, False),
        (5, 5, True),  # txs 2-4 were checked by digest only
        (6, 5, False),
        (7, 5, False),
    ]
    assert difference(env) is None


def test_an_index_of_format_1_is_read_and_replaced_by_the_next_writer(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(5)
    edit_index(env, "UPDATE meta SET value = 'graph_cli.index/1' WHERE key = 'format'")
    edit_index(env, "DELETE FROM meta WHERE key IN ('log_digest', 'full_verified_seq')")
    edit_index(env, "DROP TABLE idempotency")
    snapshot = FileStore(store_of(env).layout.root, env.graph_id).snapshot()
    assert snapshot.warnings == () and isinstance(snapshot._index, IndexedView)
    assert difference(env) is None
    store = fresh(env)
    _, result = env.write(lambda d: d.create())
    assert result.warnings == ()
    assert replayed(store) == 6
    assert meta_of(env).format == INDEX_FORMAT
    assert difference(env) is None


def test_the_digest_follows_the_log_across_segments(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    for _ in range(14):
        store = fresh(env, segment_bytes=4000)
        env.write(env.builder.random_tx)
    segments = [name for _, name in store.txlog.segments()]
    assert len(segments) > 2
    assert [d.segment for d in meta_of(env).digest or ()] == segments
    assert replayed(store) == 0
    assert difference(env) is None
