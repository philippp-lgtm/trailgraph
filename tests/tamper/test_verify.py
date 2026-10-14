"""T: one tamper scenario per integrity.storage.* code; reindex rebuilds an identical tree.

Contracts B4.1, B13.1, R.6, section 4.2; plan D9, D10.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from support.drafts import intent
from support.stores import StoreEnv, snapshot_state
from support.trees import tree_hash

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.storage import finding_codes
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore


def setup(tmp_path: Path) -> tuple[StoreEnv, FileStore, FileStoreMaintenance]:
    env = StoreEnv("files", "inventory", tmp_path, seed=5)
    env.random(12)
    assert isinstance(env.store, FileStore)
    return env, env.store, FileStoreMaintenance(tmp_path, syncer=env.syncer)


def verify(maintenance: FileStoreMaintenance, graph: GraphId) -> list[str]:
    with maintenance.maintenance_session(graph, timeout_s=5) as session:
        return [f.code for f in session.verify()]


def revision_files(store: FileStore) -> list[Path]:
    return sorted(store.layout.entities.rglob("r*.json"))


def edit_interior_tx_line(store: FileStore) -> None:
    segment = store.txlog.path(store.txlog.segments()[0][1])
    lines = segment.read_bytes().splitlines(keepends=True)
    lines[3] = (
        lines[3].replace(b'"reason"', b'"reasom"')
        if b'"reason"' in lines[3]
        else (lines[3].replace(b'"apply"', b'"apply2"', 1))
    )
    segment.write_bytes(b"".join(lines))


def edit_revision_file(store: FileStore) -> None:
    path = revision_files(store)[0]
    document = json.loads(path.read_text())
    document["attrs"]["tampered"] = "yes"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def delete_revision_file(store: FileStore) -> None:
    revision_files(store)[0].unlink()


def add_revision_file(store: FileStore) -> None:
    source = revision_files(store)[0]
    shutil.copy(source, source.with_name("r000099.json"))


def edit_head(store: FileStore) -> None:
    document = json.loads(store.layout.head.read_text())
    document["alias_counters"] = {"ZZ": 1}
    store.layout.head.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


SCENARIOS: dict[str, Callable[[FileStore], None]] = {
    finding_codes.TX_CHAIN_BROKEN: edit_interior_tx_line,
    finding_codes.REVISION_HASH_MISMATCH: edit_revision_file,
    finding_codes.REVISION_MISSING: delete_revision_file,
    finding_codes.REVISION_UNEXPECTED: add_revision_file,
    finding_codes.HEAD_INCONSISTENT: edit_head,
}


def test_every_storage_code_has_one_scenario() -> None:
    """R.6: registry and tamper scenarios match one to one."""
    assert set(SCENARIOS) == {spec.code for spec in finding_codes.FINDING_CODES}


def test_clean_graph_verifies_clean(tmp_path: Path) -> None:
    env, _, maintenance = setup(tmp_path)
    assert verify(maintenance, env.graph_id) == []


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(tmp_path: Path, code: str) -> None:
    env, store, maintenance = setup(tmp_path)
    SCENARIOS[code](store)
    assert set(verify(maintenance, env.graph_id)) == {code}


def test_more_head_inconsistencies(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    original = store.layout.head.read_text()
    for change in (
        {"seq": 99},
        {"hash": "sha256:" + "0" * 64},
        {"txlog": {"segment": "000000000001.jsonl", "offset": 3}},
        {"audit": {"seq": 2, "hash": "sha256:" + "0" * 64}},
    ):
        document = {**json.loads(original), **change}
        store.layout.head.write_text(json.dumps(document))
        assert verify(maintenance, env.graph_id) == [finding_codes.HEAD_INCONSISTENT], change
    store.layout.head.write_text("{broken")
    assert verify(maintenance, env.graph_id) == [finding_codes.HEAD_INCONSISTENT]


def test_verify_does_not_recover(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    segment = store.txlog.path(store.txlog.segments()[-1][1])
    with open(segment, "ab") as handle:
        handle.write(b'{"torn')
    before = tree_hash(store.layout.root)
    assert verify(maintenance, env.graph_id) == []
    assert tree_hash(store.layout.root) == before
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        report = session.recover()
        assert report.recovered
        assert not session.recover().recovered
    assert segment.read_bytes().endswith(b"\n")


def _derived_tree(store: FileStore) -> str:
    return tree_hash(store.layout.entities) + store.layout.head.read_text()


def test_reindex_rebuilds_an_identical_tree(tmp_path: Path) -> None:
    """C, B4.1: delete entities/ and head.json, rebuild from the log."""
    env, store, maintenance = setup(tmp_path)
    before = _derived_tree(store)
    shutil.rmtree(store.layout.entities)
    store.layout.head.unlink()
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.reindex()
    assert _derived_tree(store) == before
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.reindex()  # also from a complete tree
        assert session.verify() == []
    assert _derived_tree(store) == before


def test_writer_recovery_also_rebuilds_the_derived_tree(tmp_path: Path) -> None:
    _, store, _ = setup(tmp_path)
    before = _derived_tree(store)
    shutil.rmtree(store.layout.entities)
    store.layout.head.unlink()
    with store.session(timeout_s=5):
        pass
    assert _derived_tree(store) == before


def test_reindex_refuses_a_damaged_log(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    edit_interior_tx_line(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        with pytest.raises(GraphCliError) as info:
            session.reindex()
        assert info.value.code == "backend.corrupt"
        with pytest.raises(GraphCliError) as info:
            session.recover()
        assert info.value.code == "backend.corrupt"
        assert [f.code for f in session.verify()] == [finding_codes.TX_CHAIN_BROKEN]


def test_maintenance_session_writes_like_a_write_session(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    env.store = store
    head = store.head()
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.head() == head
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        tx = draft.build(idempotency_key="maint-1")
        session.commit(tx, session.append_audit(intent(tx)))
        assert session.lookup_idempotency("maint-1", "test:1") is not None
    assert store.head().seq == head.seq + 1
    assert verify(maintenance, env.graph_id) == []


def test_maintenance_session_holds_the_graph_lock(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5):
        with pytest.raises(GraphCliError) as info, store.session(timeout_s=0.05):
            pass
        assert info.value.code == "backend.lock_timeout"


def test_unknown_graph_is_unavailable(tmp_path: Path) -> None:
    env, _, maintenance = setup(tmp_path)
    other = GraphId(env.ids.new_id())
    with pytest.raises(GraphCliError) as info, maintenance.maintenance_session(other, timeout_s=1):
        pass
    assert info.value.code == "backend.unavailable"
    assert not (tmp_path / "graphs" / str(other)).exists()
