"""Storage maintenance effects: create_graph (W.f), replace_manifest and install_release (W.e).

Plan M2: each effect is published by one rename; a failure before it leaves
the staging state that a kill -9 would leave, and readers ignore it.
"""

from pathlib import Path

import pytest
from support.drafts import draft
from support.fixtures import compiled_model
from support.graphs import Installation, manifest, pin_of, release_files, with_pin

from graph_cli.kernel.canonical import hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditReceipt, graph_stream
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileMaintenanceSession
from graph_cli.storage.files.store import FileStore


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def graph_dir(inst: Installation, graph_id: GraphId) -> Path:
    return inst.root / "graphs" / str(graph_id)


def intent(inst: Installation, session: FileMaintenanceSession, graph_id: GraphId) -> AuditReceipt:
    event = draft(
        graph_stream(str(graph_id)),
        inst.event_id(),
        inst.clock.now(),
        actions=("maint.ontology_release",),
    )
    return session.append_audit(event)


# ---- create_graph -----------------------------------------------------------


def test_create_graph_publishes_a_complete_graph(inst: Installation) -> None:
    graph_id = inst.create()
    root = graph_dir(inst, graph_id)
    assert {p.name for p in root.iterdir()} == {
        ".lock",
        "audit",
        "data",
        "manifest.toml",
        "ontology",
    }
    model = compiled_model("inventory")
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.manifest() == manifest(graph_id, "example_graph", pin_of(model))
        assert session.release_hashes() == {model.bundle_version: model.artifact_hash}
        assert session.verify() == []
    lines = list(inst.audit.read_lines(graph_stream(str(graph_id))))
    assert len(lines) == 1
    genesis = parse_json(lines[0].data)
    assert isinstance(genesis, dict) and genesis["seq"] == 1
    (receipt,) = inst.announced
    assert (receipt.stream, receipt.seq, receipt.hash) == (
        graph_stream(str(graph_id)),
        1,
        genesis["hash"],
    )
    assert inst.audit.head(INSTALLATION_STREAM)[0] == 1
    store = FileStore(root, graph_id, syncer=inst.syncer)
    assert store.head().seq == 0
    with store.session(timeout_s=5) as write:
        assert write.head().seq == 0


def test_announce_runs_before_the_graph_is_published(inst: Installation) -> None:
    graph_id = GraphId(inst.ids.new_id())
    seen: list[tuple[bool, bool]] = []

    def announce(receipt: AuditReceipt) -> AuditReceipt:
        staging = inst.root / "graphs" / f".tmp-{graph_id}"
        seen.append((staging.is_dir(), graph_dir(inst, graph_id).exists()))
        return inst.announce(receipt)

    model = compiled_model("inventory")
    with inst.maintenance.registry(timeout_s=5) as registry:
        registry.create_graph(
            manifest(graph_id, "g", pin_of(model)),
            draft(graph_stream(str(graph_id)), inst.event_id(), inst.clock.now()),
            version=model.bundle_version,
            files=release_files(model),
            announce=announce,
        )
    assert seen == [(True, False)]


def test_registry_lists_manifests_and_ignores_staging(inst: Installation) -> None:
    first = inst.create("first")
    second = inst.create("second", ontology="notes_and_sources")
    (inst.root / "graphs" / ".tmp-leftover").mkdir()
    (inst.root / "graphs" / "not-a-graph").mkdir()
    assert inst.maintenance.graph_ids() == sorted([first, second])
    with inst.maintenance.registry(timeout_s=5) as registry:
        assert sorted(m.entry.name for m in registry.manifests()) == ["first", "second"]


def test_create_graph_refuses_an_existing_id(inst: Installation) -> None:
    graph_id = inst.create()
    with pytest.raises(GraphCliError) as caught:
        inst.create("other", graph_id=graph_id)
    assert caught.value.code == "internal.error"


@pytest.mark.parametrize(
    "files",
    [{"compiled.json": b"{}", "../escape": b"x"}, {"/abs": b"x"}, {"source/a.yaml": b"x"}],
)
def test_create_graph_checks_release_files_first(
    inst: Installation, files: dict[str, bytes]
) -> None:
    with pytest.raises(GraphCliError) as caught:
        inst.create(files=files)
    assert caught.value.code == "internal.error"
    assert list((inst.root / "graphs").iterdir()) == []
    assert inst.announced == []


def test_missing_data_root_is_unavailable(tmp_path: Path) -> None:
    inst = Installation(tmp_path / "root")
    inst.root.rmdir()
    with pytest.raises(GraphCliError) as caught:
        inst.maintenance.graph_ids()
    assert caught.value.code == "backend.unavailable"
    with pytest.raises(GraphCliError) as caught, inst.maintenance.registry(timeout_s=1):
        pass
    assert caught.value.code == "backend.unavailable"
    assert not inst.root.exists()


@pytest.mark.parametrize(
    ("point", "announced"),
    [(CrashPoint.CREATE_BEFORE_ANNOUNCE, False), (CrashPoint.CREATE_BEFORE_RENAME, True)],
)
def test_interrupted_create_leaves_only_staging(
    inst: Installation, point: CrashPoint, announced: bool
) -> None:
    graph_id = GraphId(inst.ids.new_id())
    inst.faults.armed.add(point)
    with pytest.raises(GraphCliError) as caught:
        inst.create(graph_id=graph_id)
    assert caught.value.code == "backend.unavailable"
    assert bool(inst.announced) == announced
    assert inst.audit.head(INSTALLATION_STREAM)[0] == int(announced)
    assert (inst.root / "graphs" / f".tmp-{graph_id}").is_dir()
    assert inst.maintenance.graph_ids() == []
    inst.faults.armed.clear()
    assert inst.create(graph_id=graph_id) == graph_id  # the leftover is replaced
    assert inst.maintenance.graph_ids() == [graph_id]
    assert not (inst.root / "graphs" / f".tmp-{graph_id}").exists()


# ---- replace_manifest and install_release -----------------------------------


def test_replace_manifest(inst: Installation) -> None:
    graph_id = inst.create()
    newer = compiled_model("inventory", "1.0.1")
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        changed = with_pin(session.manifest(), pin_of(newer))
        receipt = intent(inst, session, graph_id)
        session.replace_manifest(changed, receipt)
        assert session.manifest() == changed
        with pytest.raises(GraphCliError) as caught:
            session.replace_manifest(changed, receipt)  # each intent serves one effect
        assert caught.value.details["rule"] == "receipt_reused"
    assert not (graph_dir(inst, graph_id) / "manifest.toml.tmp").exists()


def test_replace_manifest_checks_graph_and_session(inst: Installation) -> None:
    graph_id = inst.create()
    other = inst.create("other")
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        foreign = intent(inst, session, graph_id)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        current = session.manifest()
        with pytest.raises(GraphCliError) as caught:
            session.replace_manifest(current, foreign)
        assert caught.value.details["rule"] == "receipt_session"
    with inst.maintenance.maintenance_session(other, timeout_s=5) as session:
        receipt = intent(inst, session, other)
        with pytest.raises(GraphCliError) as caught:
            session.replace_manifest(current, receipt)
        assert caught.value.code == "internal.error"


def test_interrupted_manifest_switch_keeps_the_old_manifest(inst: Installation) -> None:
    graph_id = inst.create()
    newer = compiled_model("inventory", "1.0.1")
    inst.faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        before = session.manifest()
        with pytest.raises(GraphCliError) as caught:
            session.replace_manifest(
                with_pin(before, pin_of(newer)), intent(inst, session, graph_id)
            )
        assert caught.value.code == "backend.unavailable"
        assert session.manifest() == before


def test_install_release(inst: Installation) -> None:
    graph_id = inst.create()
    newer = compiled_model("inventory", "1.0.1")
    files = {**release_files(newer), "source/inventory.yaml": b"format: x\n"}
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        session.install_release("1.0.1", files, intent(inst, session, graph_id))
        assert session.release_hashes()["1.0.1"] == newer.artifact_hash
        assert session.read_release_file("1.0.1", "source/inventory.yaml") == b"format: x\n"
        with pytest.raises(GraphCliError) as caught:
            session.install_release("1.0.1", files, intent(inst, session, graph_id))
        assert caught.value.code == "internal.error"
        with pytest.raises(GraphCliError) as caught:
            session.read_release_file("1.0.1", "missing.json")
        assert caught.value.code == "ontology.artifact_missing"
        for version, name in (("1.0", "compiled.json"), ("1.0.1", "../manifest.toml")):
            with pytest.raises(GraphCliError) as caught:
                session.read_release_file(version, name)
            assert caught.value.code == "internal.error"
    compiled = graph_dir(inst, graph_id) / "ontology" / "1.0.1" / "compiled.json"
    assert hash_value(parse_json(compiled.read_bytes())) == newer.artifact_hash


def test_interrupted_release_is_not_installed(inst: Installation) -> None:
    graph_id = inst.create()
    newer = compiled_model("inventory", "1.0.1")
    inst.faults.armed.add(CrashPoint.RELEASE_BEFORE_RENAME)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        with pytest.raises(GraphCliError) as caught:
            session.install_release("1.0.1", release_files(newer), intent(inst, session, graph_id))
        assert caught.value.code == "backend.unavailable"
        assert "1.0.1" not in session.release_hashes()
        assert (graph_dir(inst, graph_id) / "ontology" / ".tmp-1.0.1").is_dir()
        inst.faults.armed.clear()
        session.install_release("1.0.1", release_files(newer), intent(inst, session, graph_id))
        assert session.release_hashes()["1.0.1"] == newer.artifact_hash
    assert not (graph_dir(inst, graph_id) / "ontology" / ".tmp-1.0.1").exists()
