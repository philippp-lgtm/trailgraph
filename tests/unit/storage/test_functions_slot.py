"""The function catalog slot: read through the catalog, replaced by the maintenance port
(K6; section 5 ``functions/``; crash point ``functions.before_rename``)."""

from pathlib import Path

import pytest
from support.drafts import draft
from support.graphs import Installation
from support.services import MemoryCatalog

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import AuditReceipt, graph_stream
from graph_cli.kernel.ports.catalog import FunctionsSlot, functions_hash
from graph_cli.kinetic.format import CATALOG_SCHEMA
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.functions_slot import CATALOG_SCHEMA as STORED_SCHEMA
from graph_cli.storage.files.maintenance import FileMaintenanceSession


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def slot(inst: Installation, graph_id: GraphId, names: tuple[str, ...] = ("one",)) -> FunctionsSlot:
    files = {f"{name}.yaml": f"name: {name}\n".encode() for name in names}
    compiled = {"schema": CATALOG_SCHEMA, "functions": [{"name": name} for name in names]}
    pin = FileCatalog(inst.root).resolve(str(graph_id)).pin
    return FunctionsSlot(functions_hash(files), hash_value(compiled), pin, compiled, files)


def intent(inst: Installation, session: FileMaintenanceSession, graph_id: GraphId) -> AuditReceipt:
    event = draft(
        graph_stream(str(graph_id)),
        inst.event_id(),
        inst.clock.now(),
        actions=("maint.graph_configure",),
    )
    return session.append_audit(event)


def publish(inst: Installation, graph_id: GraphId, new: FunctionsSlot) -> None:
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        session.replace_functions(new, intent(inst, session, graph_id))


def functions_dir(inst: Installation, graph_id: GraphId) -> Path:
    return inst.root / "graphs" / str(graph_id) / "functions"


def app_view(published: FunctionsSlot) -> FunctionsSlot:
    """What the catalog port returns: the compiled document without the sources."""
    return FunctionsSlot(
        published.hash, published.compiled_hash, published.published_against, published.compiled
    )


def test_the_stored_schema_is_the_format_schema() -> None:
    assert STORED_SCHEMA == CATALOG_SCHEMA


def test_no_slot_reads_as_none(inst: Installation) -> None:
    graph_id = inst.create()
    assert FileCatalog(inst.root).read_functions(graph_id) is None
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_functions() is None
    assert MemoryCatalog().read_functions(graph_id) is None


def test_publish_then_replace(inst: Installation) -> None:
    graph_id = inst.create()
    first = slot(inst, graph_id)
    publish(inst, graph_id, first)
    assert FileCatalog(inst.root).read_functions(graph_id) == app_view(first)
    base = functions_dir(inst, graph_id)
    assert [p.name for p in (base / "files").iterdir()] == [first.hash.removeprefix("sha256:")]
    second = slot(inst, graph_id, ("one", "two"))
    publish(inst, graph_id, second)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_functions() == second
    assert [p.name for p in (base / "files").iterdir()] == [second.hash.removeprefix("sha256:")]
    assert [p.name for p in (base / "compiled").iterdir()] == [
        f"{second.compiled_hash.removeprefix('sha256:')}.json"
    ]
    assert sorted(p.name for p in base.iterdir()) == ["compiled", "files", "slot.json"]
    assert second.count == 2 and first.count == 1


def test_an_empty_catalog_is_a_published_slot(inst: Installation) -> None:
    graph_id = inst.create()
    publish(inst, graph_id, slot(inst, graph_id))
    empty = slot(inst, graph_id, ())
    publish(inst, graph_id, empty)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_functions() == empty
    assert empty.count == 0


def test_replace_checks_hashes_names_and_receipt(inst: Installation) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id)
    other = slot(inst, graph_id, ("other",))
    nested = {"a/b.yaml": b"x"}
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        for bad in (
            FunctionsSlot(
                good.hash, good.compiled_hash, good.published_against, good.compiled, other.files
            ),
            FunctionsSlot(
                good.hash, other.compiled_hash, good.published_against, good.compiled, good.files
            ),
            FunctionsSlot(
                functions_hash(nested),
                good.compiled_hash,
                good.published_against,
                good.compiled,
                nested,
            ),
            FunctionsSlot(
                functions_hash({"x.txt": b"x"}),
                good.compiled_hash,
                good.published_against,
                good.compiled,
                {"x.txt": b"x"},
            ),
        ):
            with pytest.raises(GraphCliError) as caught:
                session.replace_functions(bad, intent(inst, session, graph_id))
            assert caught.value.code == "internal.error"
        receipt = intent(inst, session, graph_id)
        session.replace_functions(good, receipt)
        with pytest.raises(GraphCliError) as caught:
            session.replace_functions(good, receipt)  # each intent serves one effect
        assert caught.value.details["rule"] == "receipt_reused"


def test_interrupted_publish_keeps_the_old_catalog(inst: Installation) -> None:
    graph_id = inst.create()
    first, second = slot(inst, graph_id), slot(inst, graph_id, ("one", "two"))
    publish(inst, graph_id, first)
    inst.faults.armed.add(CrashPoint.FUNCTIONS_BEFORE_RENAME)
    with pytest.raises(GraphCliError) as caught:
        publish(inst, graph_id, second)
    assert caught.value.code == "backend.unavailable"
    assert FileCatalog(inst.root).read_functions(graph_id) == app_view(first)  # old, never mixed
    base = functions_dir(inst, graph_id)
    assert (base / "slot.json.tmp").exists()
    inst.faults.armed.clear()
    publish(inst, graph_id, second)
    assert FileCatalog(inst.root).read_functions(graph_id) == app_view(second)
    assert sorted(p.name for p in base.iterdir()) == ["compiled", "files", "slot.json"]
    assert len(list((base / "files").iterdir())) == 1
    assert len(list((base / "compiled").iterdir())) == 1


def test_interrupted_first_publish_leaves_no_catalog(inst: Installation) -> None:
    graph_id = inst.create()
    inst.faults.armed.add(CrashPoint.FUNCTIONS_BEFORE_RENAME)
    with pytest.raises(GraphCliError):
        publish(inst, graph_id, slot(inst, graph_id))
    assert FileCatalog(inst.root).read_functions(graph_id) is None


def test_damaged_slots(inst: Installation) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id)
    publish(inst, graph_id, good)
    base = functions_dir(inst, graph_id)
    slot_file = base / "slot.json"
    original = slot_file.read_bytes()
    tree = base / "files" / good.hash.removeprefix("sha256:")
    compiled = base / "compiled" / f"{good.compiled_hash.removeprefix('sha256:')}.json"

    def read(sources: bool) -> FunctionsSlot | None:
        if not sources:
            return FileCatalog(inst.root).read_functions(graph_id)
        with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            return session.read_functions()

    def code(*, sources: bool = False) -> str:
        with pytest.raises(GraphCliError) as caught:
            read(sources)
        return caught.value.code

    (tree / "one.yaml").write_text("tampered")
    assert code(sources=True) == "functions.slot_invalid"
    assert FileCatalog(inst.root).read_functions(graph_id) == app_view(good)  # the app skips them
    (tree / "one.yaml").write_bytes(good.files["one.yaml"])
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_functions() == good  # positive control
    compiled_bytes = compiled.read_bytes()
    compiled.write_bytes(compiled_bytes.replace(b'"one"', b'"two"'))
    assert code() == "functions.slot_invalid"
    compiled.unlink()
    assert code() == "functions.slot_invalid"
    compiled.write_bytes(compiled_bytes)
    slot_file.write_text("{not json")
    assert code() == "functions.slot_invalid"
    slot_file.write_bytes(
        original.replace(b"graph_cli.function_slot/1", b"graph_cli.function_slot/9")
    )
    assert code() == "format.unsupported_version"
    slot_file.write_bytes(original.replace(good.hash.encode(), b"sha256:" + b"0" * 64))
    assert code(sources=True) == "functions.slot_invalid"
