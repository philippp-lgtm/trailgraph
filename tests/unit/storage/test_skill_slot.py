"""The graph skill slot: read through the catalog, replaced by the maintenance port
(D15 Q2, Q8; section 5 ``skill/``; crash point ``skill.before_rename``)."""

from pathlib import Path

import pytest
from support.drafts import draft
from support.graphs import Installation
from support.services import MemoryCatalog

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import AuditReceipt, graph_stream
from graph_cli.kernel.ports.catalog import SkillSlot, skill_hash
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileMaintenanceSession


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def slot(inst: Installation, graph_id: GraphId, text: str = "one") -> SkillSlot:
    files = {"SKILL.md": f"# {text}\n".encode(), "references/model.md": b"model\n"}
    pin = FileCatalog(inst.root).resolve(str(graph_id)).pin
    return SkillSlot("static", skill_hash(files), pin, files)


def intent(inst: Installation, session: FileMaintenanceSession, graph_id: GraphId) -> AuditReceipt:
    event = draft(
        graph_stream(str(graph_id)),
        inst.event_id(),
        inst.clock.now(),
        actions=("maint.graph_configure",),
    )
    return session.append_audit(event)


def publish(inst: Installation, graph_id: GraphId, new: SkillSlot) -> None:
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        session.replace_skill(new, intent(inst, session, graph_id))


def skill_dir(inst: Installation, graph_id: GraphId) -> Path:
    return inst.root / "graphs" / str(graph_id) / "skill"


def test_no_slot_reads_as_none(inst: Installation) -> None:
    graph_id = inst.create()
    assert FileCatalog(inst.root).read_skill(graph_id) is None
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_skill() is None
    assert MemoryCatalog().read_skill(graph_id) is None


def test_publish_then_replace(inst: Installation) -> None:
    graph_id = inst.create()
    first = slot(inst, graph_id)
    publish(inst, graph_id, first)
    assert FileCatalog(inst.root).read_skill(graph_id) == first
    files_dir = skill_dir(inst, graph_id) / "files"
    assert [p.name for p in files_dir.iterdir()] == [first.hash.removeprefix("sha256:")]
    second = slot(inst, graph_id, "two")
    publish(inst, graph_id, second)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_skill() == second
    assert [p.name for p in files_dir.iterdir()] == [second.hash.removeprefix("sha256:")]
    assert sorted(p.name for p in skill_dir(inst, graph_id).iterdir()) == ["files", "source.json"]


def test_replace_checks_hash_names_and_receipt(inst: Installation) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        for bad in (
            SkillSlot("static", good.hash, good.published_against, {"SKILL.md": b"other"}),
            SkillSlot("static", skill_hash({"../x": b"x"}), good.published_against, {"../x": b"x"}),
            SkillSlot("static", skill_hash({}), good.published_against, {}),
        ):
            with pytest.raises(GraphCliError) as caught:
                session.replace_skill(bad, intent(inst, session, graph_id))
            assert caught.value.code == "internal.error"
        receipt = intent(inst, session, graph_id)
        session.replace_skill(good, receipt)
        with pytest.raises(GraphCliError) as caught:
            session.replace_skill(good, receipt)  # each intent serves one effect
        assert caught.value.details["rule"] == "receipt_reused"


def test_interrupted_publish_keeps_the_old_skill(inst: Installation) -> None:
    graph_id = inst.create()
    first, second = slot(inst, graph_id), slot(inst, graph_id, "two")
    publish(inst, graph_id, first)
    inst.faults.armed.add(CrashPoint.SKILL_BEFORE_RENAME)
    with pytest.raises(GraphCliError) as caught:
        publish(inst, graph_id, second)
    assert caught.value.code == "backend.unavailable"
    assert FileCatalog(inst.root).read_skill(graph_id) == first  # old, never mixed
    assert (skill_dir(inst, graph_id) / "source.json.tmp").exists()
    inst.faults.armed.clear()
    publish(inst, graph_id, second)
    assert FileCatalog(inst.root).read_skill(graph_id) == second
    assert sorted(p.name for p in skill_dir(inst, graph_id).iterdir()) == ["files", "source.json"]
    assert len(list((skill_dir(inst, graph_id) / "files").iterdir())) == 1


def test_interrupted_first_publish_leaves_no_skill(inst: Installation) -> None:
    graph_id = inst.create()
    inst.faults.armed.add(CrashPoint.SKILL_BEFORE_RENAME)
    with pytest.raises(GraphCliError):
        publish(inst, graph_id, slot(inst, graph_id))
    assert FileCatalog(inst.root).read_skill(graph_id) is None


def test_damaged_slots(inst: Installation) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id)
    publish(inst, graph_id, good)
    source = skill_dir(inst, graph_id) / "source.json"
    original = source.read_bytes()
    tree = skill_dir(inst, graph_id) / "files" / good.hash.removeprefix("sha256:")

    def code() -> str:
        with pytest.raises(GraphCliError) as caught:
            FileCatalog(inst.root).read_skill(graph_id)
        return caught.value.code

    (tree / "SKILL.md").write_text("tampered")
    assert code() == "skills.source_invalid"
    (tree / "SKILL.md").write_bytes(good.files["SKILL.md"])
    assert FileCatalog(inst.root).read_skill(graph_id) == good  # positive control
    source.write_text("{not json")
    assert code() == "skills.source_invalid"
    source.write_bytes(original.replace(b"graph_cli.skill_source/1", b"graph_cli.skill_source/9"))
    assert code() == "format.unsupported_version"
    source.write_bytes(original.replace(good.hash.encode(), b"sha256:" + b"0" * 64))
    assert code() == "skills.source_invalid"
