"""The function and action catalog slots: read through the catalog, replaced by the
maintenance port (K6, A8; section 5 ``functions/``, ``actions/``; crash points
``functions.before_rename`` and ``actions.before_rename``). Every test runs for both kinds:
one implementation."""

from collections.abc import Callable
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
from graph_cli.kernel.ports.catalog import KINDS, KineticSlot, SlotKind, sources_hash
from graph_cli.kinetic.action_format import CATALOG_SCHEMA as ACTION_SCHEMA
from graph_cli.kinetic.format import CATALOG_SCHEMA as FUNCTION_SCHEMA
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileMaintenanceSession


@pytest.fixture(params=["functions", "actions"])
def kind(request: pytest.FixtureRequest) -> SlotKind:
    value: SlotKind = request.param
    return value


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def slot(
    inst: Installation, graph_id: GraphId, kind: SlotKind, names: tuple[str, ...] = ("one",)
) -> KineticSlot:
    files = {f"{name}.yaml": f"name: {name}\n".encode() for name in names}
    schema = FUNCTION_SCHEMA if kind == "functions" else ACTION_SCHEMA
    compiled = {"schema": schema, kind: [{"name": name} for name in names]}
    pin = FileCatalog(inst.root).resolve(str(graph_id)).pin
    return KineticSlot(sources_hash(files), hash_value(compiled), pin, compiled, files, kind)


def intent(inst: Installation, session: FileMaintenanceSession, graph_id: GraphId) -> AuditReceipt:
    event = draft(
        graph_stream(str(graph_id)),
        inst.event_id(),
        inst.clock.now(),
        actions=("maint.graph_configure",),
    )
    return session.append_audit(event)


def replacer(session: FileMaintenanceSession, kind: SlotKind) -> Callable[..., None]:
    return session.replace_functions if kind == "functions" else session.replace_actions


def reader(session: FileMaintenanceSession, kind: SlotKind) -> Callable[[], KineticSlot | None]:
    return session.read_functions if kind == "functions" else session.read_actions


def read_app(inst: Installation, graph_id: GraphId, kind: SlotKind) -> KineticSlot | None:
    catalog = FileCatalog(inst.root)
    return (
        catalog.read_functions(graph_id) if kind == "functions" else catalog.read_actions(graph_id)
    )


def publish(inst: Installation, graph_id: GraphId, new: KineticSlot) -> None:
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        replacer(session, new.kind)(new, intent(inst, session, graph_id))


def kind_dir(inst: Installation, graph_id: GraphId, kind: SlotKind) -> Path:
    return inst.root / "graphs" / str(graph_id) / kind


def app_view(published: KineticSlot) -> KineticSlot:
    """What the catalog port returns: the compiled document without the sources."""
    return KineticSlot(
        published.hash,
        published.compiled_hash,
        published.published_against,
        published.compiled,
        kind=published.kind,
    )


def test_the_stored_schemas_are_the_format_schemas() -> None:
    assert KINDS["functions"].catalog_schema == FUNCTION_SCHEMA
    assert KINDS["actions"].catalog_schema == ACTION_SCHEMA
    assert KINDS["functions"].slot_schema == "graph_cli.function_slot/1"
    assert KINDS["actions"].slot_schema == "graph_cli.action_slot/1"


def test_no_slot_reads_as_none(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    assert read_app(inst, graph_id, kind) is None
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert reader(session, kind)() is None
    memory = MemoryCatalog()
    assert (memory.read_functions if kind == "functions" else memory.read_actions)(graph_id) is None


def test_publish_then_replace(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    first = slot(inst, graph_id, kind)
    publish(inst, graph_id, first)
    assert read_app(inst, graph_id, kind) == app_view(first)
    base = kind_dir(inst, graph_id, kind)
    assert [p.name for p in (base / "files").iterdir()] == [first.hash.removeprefix("sha256:")]
    second = slot(inst, graph_id, kind, ("one", "two"))
    publish(inst, graph_id, second)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert reader(session, kind)() == second
    assert [p.name for p in (base / "files").iterdir()] == [second.hash.removeprefix("sha256:")]
    assert [p.name for p in (base / "compiled").iterdir()] == [
        f"{second.compiled_hash.removeprefix('sha256:')}.json"
    ]
    assert sorted(p.name for p in base.iterdir()) == ["compiled", "files", "slot.json"]
    assert second.count == 2 and first.count == 1


def test_the_kinds_do_not_share_files(inst: Installation) -> None:
    graph_id = inst.create()
    publish(inst, graph_id, slot(inst, graph_id, "functions", ("alpha",)))
    assert read_app(inst, graph_id, "actions") is None
    publish(inst, graph_id, slot(inst, graph_id, "actions", ("beta",)))
    functions, actions = read_app(inst, graph_id, "functions"), read_app(inst, graph_id, "actions")
    assert functions is not None and actions is not None
    assert functions.kind == "functions" and actions.kind == "actions"
    assert functions.compiled["functions"] == [{"name": "alpha"}]
    assert actions.compiled["actions"] == [{"name": "beta"}]
    stored = (kind_dir(inst, graph_id, "actions") / "slot.json").read_text()
    assert "graph_cli.action_slot/1" in stored


def test_a_slot_of_the_other_kind_is_refused(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    other: SlotKind = "actions" if kind == "functions" else "functions"
    bad = slot(inst, graph_id, other)
    with (
        inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session,
        pytest.raises(GraphCliError) as caught,
    ):
        replacer(session, kind)(bad, intent(inst, session, graph_id))
    assert caught.value.code == "internal.error"


def test_an_empty_catalog_is_a_published_slot(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    publish(inst, graph_id, slot(inst, graph_id, kind))
    empty = slot(inst, graph_id, kind, ())
    publish(inst, graph_id, empty)
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert reader(session, kind)() == empty
    assert empty.count == 0


def test_replace_checks_hashes_names_and_receipt(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id, kind)
    other = slot(inst, graph_id, kind, ("other",))
    nested = {"a/b.yaml": b"x"}
    flat = {"x.txt": b"x"}

    def made(digest: str, compiled_hash: str, files: dict[str, bytes]) -> KineticSlot:
        return KineticSlot(
            digest, compiled_hash, good.published_against, good.compiled, files, kind
        )

    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        for bad in (
            made(good.hash, good.compiled_hash, dict(other.files)),
            made(good.hash, other.compiled_hash, dict(good.files)),
            made(sources_hash(nested), good.compiled_hash, nested),
            made(sources_hash(flat), good.compiled_hash, flat),
        ):
            with pytest.raises(GraphCliError) as caught:
                replacer(session, kind)(bad, intent(inst, session, graph_id))
            assert caught.value.code == "internal.error"
        receipt = intent(inst, session, graph_id)
        replacer(session, kind)(good, receipt)
        with pytest.raises(GraphCliError) as caught:
            replacer(session, kind)(good, receipt)  # each intent serves one effect
        assert caught.value.details["rule"] == "receipt_reused"


def test_interrupted_publish_keeps_the_old_catalog(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    first, second = slot(inst, graph_id, kind), slot(inst, graph_id, kind, ("one", "two"))
    publish(inst, graph_id, first)
    point = (
        CrashPoint.FUNCTIONS_BEFORE_RENAME
        if kind == "functions"
        else CrashPoint.ACTIONS_BEFORE_RENAME
    )
    inst.faults.armed.add(point)
    with pytest.raises(GraphCliError) as caught:
        publish(inst, graph_id, second)
    assert caught.value.code == "backend.unavailable"
    assert read_app(inst, graph_id, kind) == app_view(first)  # old, never mixed
    base = kind_dir(inst, graph_id, kind)
    assert (base / "slot.json.tmp").exists()
    inst.faults.armed.clear()
    publish(inst, graph_id, second)
    assert read_app(inst, graph_id, kind) == app_view(second)
    assert sorted(p.name for p in base.iterdir()) == ["compiled", "files", "slot.json"]
    assert len(list((base / "files").iterdir())) == 1
    assert len(list((base / "compiled").iterdir())) == 1


def test_interrupted_first_publish_leaves_no_catalog(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    point = (
        CrashPoint.FUNCTIONS_BEFORE_RENAME
        if kind == "functions"
        else CrashPoint.ACTIONS_BEFORE_RENAME
    )
    inst.faults.armed.add(point)
    with pytest.raises(GraphCliError):
        publish(inst, graph_id, slot(inst, graph_id, kind))
    assert read_app(inst, graph_id, kind) is None


def test_damaged_slots(inst: Installation, kind: SlotKind) -> None:
    graph_id = inst.create()
    good = slot(inst, graph_id, kind)
    publish(inst, graph_id, good)
    base = kind_dir(inst, graph_id, kind)
    slot_file = base / "slot.json"
    original = slot_file.read_bytes()
    tree = base / "files" / good.hash.removeprefix("sha256:")
    compiled = base / "compiled" / f"{good.compiled_hash.removeprefix('sha256:')}.json"
    invalid = f"{kind}.slot_invalid"

    def read(sources: bool) -> KineticSlot | None:
        if not sources:
            return read_app(inst, graph_id, kind)
        with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            return reader(session, kind)()

    def code(*, sources: bool = False) -> str:
        with pytest.raises(GraphCliError) as caught:
            read(sources)
        return caught.value.code

    (tree / "one.yaml").write_text("tampered")
    assert code(sources=True) == invalid
    assert read_app(inst, graph_id, kind) == app_view(good)  # the app skips them
    (tree / "one.yaml").write_bytes(good.files["one.yaml"])
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert reader(session, kind)() == good  # positive control
    compiled_bytes = compiled.read_bytes()
    compiled.write_bytes(compiled_bytes.replace(b'"one"', b'"two"'))
    assert code() == invalid
    compiled.unlink()
    assert code() == invalid
    compiled.write_bytes(compiled_bytes)
    slot_file.write_text("{not json")
    assert code() == invalid
    schema = KINDS[kind].slot_schema
    slot_file.write_bytes(original.replace(schema.encode(), schema[:-1].encode() + b"9"))
    assert code() == "format.unsupported_version"
    slot_file.write_bytes(original.replace(good.hash.encode(), b"sha256:" + b"0" * 64))
    assert code(sources=True) == invalid
    # the other kind's schema in the compiled file is refused as well
    other = ACTION_SCHEMA if kind == "functions" else FUNCTION_SCHEMA
    slot_file.write_bytes(original)
    compiled.write_bytes(
        compiled_bytes.replace(KINDS[kind].catalog_schema.encode(), other.encode())
    )
    assert code() == "format.unsupported_version"
