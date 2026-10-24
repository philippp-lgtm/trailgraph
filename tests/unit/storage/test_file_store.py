"""FileStore specifics: layout, goldens, segments, read-only reads, sync order.

Contracts B4, B13.7, B13.9, L.1-L.3.
"""

import os
from pathlib import Path

import pytest
from support.fixtures import GOLDENS, assert_golden
from support.stores import ALL_STATUSES, RecordingSyncer, StoreEnv
from support.trees import tree_hash
from support.txbuilder import TxDraft

from graph_cli.kernel.canonical import parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.common.chain import GENESIS, scan
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.layout import parse_segment_name, segment_name
from graph_cli.storage.files.store import FileStore


def files_env(tmp_path: Path, ontology: str = "inventory", seed: int = 0) -> StoreEnv:
    return StoreEnv("files", ontology, tmp_path, seed=seed)


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def golden_scenario(env: StoreEnv) -> EntityId:
    created: list[EntityId] = []

    def first(d: TxDraft) -> None:
        cls = next(c for c in env.builder.concrete_classes if env.builder.prefix_of(c))
        target = d.create(cls)
        source = d.create(cls)
        d.link(source, "core:source", target)
        created.extend([target, source])

    env.write(first)
    env.write(lambda d: d.update(created[0], {"note": "changed"}))
    return created[0]


def test_layout_and_goldens(tmp_path: Path) -> None:
    """G: tx log, one revision file and head.json are frozen for a fixed scenario."""
    env = files_env(tmp_path)
    entity = golden_scenario(env)
    store = store_of(env)
    layout = store.layout
    assert sorted(os.listdir(layout.txlog)) == [segment_name("", 1)]
    assert sorted(os.listdir(layout.audit)) == [segment_name("events-", 1)]
    entity_dir = layout.entities / entity.hex[-2:] / str(entity)
    assert sorted(os.listdir(entity_dir)) == ["r000001.json", "r000002.json"]
    goldens = GOLDENS / "storage"
    assert_golden(goldens / "txlog.jsonl", (layout.txlog / segment_name("", 1)).read_bytes())
    assert_golden(goldens / "revision.json", (entity_dir / "r000002.json").read_bytes())
    assert_golden(goldens / "head.json", layout.head.read_bytes())
    head = parse_json(layout.head.read_bytes())
    assert isinstance(head, dict)
    assert head["seq"] == 2 and head["txlog"]["segment"] == segment_name("", 1)  # type: ignore[index]


def test_segment_names() -> None:
    assert segment_name("", 42) == "000000000042.jsonl"
    assert parse_segment_name("events-", "events-000000000007.jsonl") == 7
    assert parse_segment_name("", "000000000007.jsonl.tmp") is None
    assert parse_segment_name("", "events-000000000007.jsonl") is None


def test_segment_rotation_and_reading_across_segments(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    store = store_of(env)
    tiny = FileStore(store.layout.root, env.graph_id, syncer=env.syncer, segment_bytes=1)
    env.store = tiny
    env.random(5)
    assert sorted(os.listdir(tiny.layout.txlog)) == [segment_name("", n) for n in range(1, 6)]
    assert [tx.seq for tx in FileStore(tiny.layout.root, env.graph_id).changes_since(0)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    fresh = FileStore(tiny.layout.root, env.graph_id, segment_bytes=1)
    assert fresh.head() == tiny.head()
    assert fresh.snapshot().query(Query(record_status=ALL_STATUSES)).total > 0
    data = b"".join(d for _, d in tiny.txlog.read_all())
    assert scan(data, GENESIS).end.seq == 5
    # A bigger roll size keeps appending to the last segment.
    env.store = FileStore(tiny.layout.root, env.graph_id, syncer=env.syncer)
    env.random(2)
    assert len(os.listdir(tiny.layout.txlog)) == 5


def test_reads_write_nothing(tmp_path: Path) -> None:
    """B13.7: also with a torn tail present and head.json missing."""
    env = files_env(tmp_path)
    env.random(8)
    store = store_of(env)
    segment = store.layout.txlog / segment_name("", 1)
    with open(segment, "ab") as handle:
        handle.write(b'{"torn":')
    store.layout.head.unlink()
    before = tree_hash(store.layout.root)
    reader = FileStore(store.layout.root, env.graph_id)
    snapshot = reader.snapshot()
    assert [w.code for w in snapshot.warnings] == ["storage.recovery_pending"]
    items = snapshot.query(Query(record_status=ALL_STATUSES, limit=1000)).items
    for revision in items:
        assert snapshot.get(revision.id) == revision
        snapshot.history(revision.id)
        snapshot.incoming(revision.id)
    reader.head()
    reader.changes_since(0)
    assert reader.head().seq == 8
    assert tree_hash(store.layout.root) == before


def test_unreadable_head_is_a_warning(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.random(3)
    store = store_of(env)
    store.layout.head.write_bytes(b"{not json")
    snapshot = FileStore(store.layout.root, env.graph_id).snapshot()
    assert [w.code for w in snapshot.warnings] == ["storage.recovery_pending"]
    assert snapshot.seq == 3


def test_missing_graph_directory_is_unavailable(tmp_path: Path) -> None:
    store = FileStore(
        tmp_path / "nope", GraphId(StoreEnv("memory", "inventory", tmp_path).graph_id)
    )
    for call in (store.head, store.snapshot, lambda: store.changes_since(0)):
        with pytest.raises(GraphCliError) as info:
            call()
        assert info.value.code == "backend.unavailable"
    with pytest.raises(GraphCliError) as info, store.session(timeout_s=0):
        pass
    assert info.value.code == "backend.unavailable"
    assert not (tmp_path / "nope").exists()


def _kind(path: str, store: FileStore) -> str:
    relative = Path(path).relative_to(store.layout.root.resolve())
    if relative.parts[0] == "audit":
        return "audit"
    if relative.parts[:2] == ("data", "txlog"):
        return "tx"
    if relative.name == "head.json.tmp":
        return "head"
    return "revision"


def test_sync_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D2: intent before tx, tx before the first revision rename, file before rename,
    directory after rename, head, then the disposable index last."""
    env = files_env(tmp_path)
    env.random(2)
    store = store_of(env)
    syncer = RecordingSyncer(record=True)
    store.syncer = syncer
    events: list[tuple[str, str]] = syncer.calls
    real_replace = os.replace

    def replace(src: object, dst: object) -> None:
        events.append(("rename", str(dst)))
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("graph_cli.storage.files.store.os.replace", replace)
    env.write(lambda d: [d.create(), d.create()])
    kinds = [
        (kind, _kind(path, store) if kind == "file" else Path(path).name) for kind, path in events
    ]
    file_kinds = [k for kind, k in kinds if kind == "file"]
    assert file_kinds[:2] == ["audit", "tx"]
    first_rename = next(i for i, (kind, _) in enumerate(kinds) if kind == "rename")
    assert kinds.index(("file", "tx")) < first_rename
    renames = [path for kind, path in events if kind == "rename"]
    for target in renames:
        rename_at = events.index(("rename", target))
        synced_file = [
            i
            for i, (k, p) in enumerate(events)
            if k == "file" and p.endswith(Path(target).name + ".tmp")
        ]
        assert synced_file and synced_file[0] < rename_at, target
        dir_syncs = [
            i for i, (k, p) in enumerate(events) if k == "dir" and p == str(Path(target).parent)
        ]
        assert dir_syncs and max(dir_syncs) > rename_at, target
    assert [Path(r).name for r in renames][-2:] == ["head.json", "index.sqlite"]


@pytest.mark.parametrize(
    "point",
    [
        CrashPoint.TX_AFTER_SYNC,
        CrashPoint.MATERIALIZE_AFTER_FIRST_FILE,
        CrashPoint.MATERIALIZE_BEFORE_DIR_SYNC,
        CrashPoint.HEAD_BEFORE_RENAME,
    ],
)
def test_deferred_materialization_is_rolled_forward(tmp_path: Path, point: CrashPoint) -> None:
    """B3.5: a failure after the commit point is a warning; readers see the new state
    through the tail overlay; the next writer completes the derived files."""
    env = files_env(tmp_path)
    env.random(3)
    store = store_of(env)
    head_before = store.layout.head.read_bytes()
    env.faults.armed = {point}
    tx, result = env.write(lambda d: [d.create(), d.create()])
    env.faults.armed = set()
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    assert store.layout.head.read_bytes() == head_before
    reader = FileStore(store.layout.root, env.graph_id)
    snapshot = reader.snapshot()
    assert snapshot.seq == 4 and snapshot.warnings == ()
    for revision in tx.revisions:
        assert snapshot.get(revision.id) == revision
        assert snapshot.get_revision(revision.id, 1) == revision
    with store.session(timeout_s=5):
        pass
    for revision in tx.revisions:
        assert store.layout.revision_path(revision.id, revision.rev).exists()
    assert parse_json(store.layout.head.read_bytes())["seq"] == 4  # type: ignore[index]


def test_failure_after_head_rename_is_still_ok(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    env.faults.armed = {CrashPoint.HEAD_AFTER_RENAME}
    _, result = env.write(lambda d: d.create())
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    assert parse_json(store_of(env).layout.head.read_bytes())["seq"] == 1  # type: ignore[index]


def test_snapshot_at_an_older_seq_ignores_newer_revision_files(tmp_path: Path) -> None:
    env = files_env(tmp_path)
    created: list[EntityId] = []
    env.write(lambda d: created.append(d.create()))
    for _ in range(3):
        env.write(lambda d: d.update(created[0]))
    store = FileStore(store_of(env).layout.root, env.graph_id)
    old = store.snapshot(at_seq=2)
    assert old.get(created[0]).rev == 2  # type: ignore[union-attr]
    assert [m.rev for m in old.history(created[0])] == [1, 2]
    assert old.get_revision(created[0], 3) is None
