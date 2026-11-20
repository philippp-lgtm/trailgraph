"""Interrupted maintenance operations (contracts W.e, W.f, B5.6; plan M2, M4).

A fault before each effect's rename leaves what a kill -9 would leave. The
verifier then reports only ``intent_without_effect`` (info), and a retry
completes the operation.
"""

from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import FIXTURES, ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.maintenance_cli.commands.graph import app_inventory
from graph_cli_admin.services_admin import audit, graphs, sources, sources_push
from graph_cli_admin.services_admin.actions import publish as publish_actions
from graph_cli_admin.services_admin.authored_views import publish as publish_views
from graph_cli_admin.services_admin.check import check
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.functions import publish as publish_functions
from graph_cli_admin.services_admin.release import release
from graph_cli_admin.services_admin.skill import publish

INVENTORY = ONTOLOGIES / "inventory"
SKILL = FIXTURES / "skills" / "graph-example-graph"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
IWE = "integrity.audit.intent_without_effect"


@pytest.fixture
def faults() -> FaultSwitch:
    return FaultSwitch()


@pytest.fixture
def ctx(tmp_path: Path, faults: FaultSwitch) -> AdminContext:
    return admin_context(tmp_path / "root", faults)


def create(ctx: AdminContext) -> None:
    graphs.create(
        ctx,
        name="example_graph",
        source=INVENTORY / "1.0.0",
        module=None,
        title=None,
        language=None,
    )


def verify_codes(ctx: AdminContext) -> list[str]:
    verification = audit.verify(ctx)
    verification.raise_on_errors()
    return sorted(f.code for f in verification.findings)


def interrupted(faults: FaultSwitch, point: CrashPoint, action: object) -> None:
    faults.armed.add(point)
    with pytest.raises(GraphCliError) as caught:
        action()  # type: ignore[operator]
    assert caught.value.code == "backend.unavailable"
    faults.armed.clear()


def test_create_before_announce_leaves_nothing_visible(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    interrupted(faults, CrashPoint.CREATE_BEFORE_ANNOUNCE, lambda: create(ctx))
    assert ctx.catalog.list_graphs() == []
    assert verify_codes(ctx) == []
    create(ctx)
    assert [e.name for e in ctx.catalog.list_graphs()] == ["example_graph"]
    assert verify_codes(ctx) == []


def test_create_before_rename_leaves_an_announced_intent(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    interrupted(faults, CrashPoint.CREATE_BEFORE_RENAME, lambda: create(ctx))
    assert ctx.catalog.list_graphs() == []
    assert verify_codes(ctx) == [IWE]
    create(ctx)  # the name is free: the graph was never published
    assert verify_codes(ctx) == [IWE]


def test_release_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    create(ctx)
    run = lambda: release(ctx, graph_ref="example_graph", source=INVENTORY / "1.0.1", module=None)  # noqa: E731
    interrupted(faults, CrashPoint.RELEASE_BEFORE_RENAME, run)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.0"
    assert verify_codes(ctx) == [IWE]
    assert run().data["released"] is True
    # The retry installed the same artifact: the first intent's effect now exists.
    assert verify_codes(ctx) == []


def test_pin_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    create(ctx)
    run = lambda: release(ctx, graph_ref="example_graph", source=INVENTORY / "1.0.1", module=None)  # noqa: E731
    interrupted(faults, CrashPoint.MANIFEST_BEFORE_RENAME, run)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.0"
    assert verify_codes(ctx) == [IWE]
    outcome = run()
    assert (outcome.data["released"], outcome.data["pinned"]) == (False, True)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.1"
    assert verify_codes(ctx) == [IWE]  # the first pin intent stays an orphan


def test_skill_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    """D15 Q8: the old slot (here: none) stays; the intent is an interrupted publish."""
    create(ctx)
    run = lambda: publish(  # noqa: E731
        ctx,
        graph_ref="example_graph",
        source=SKILL,
        reason=None,
        dry_run=False,
        inventory=app_inventory(),
    )
    interrupted(faults, CrashPoint.SKILL_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_skill(graph.id) is None
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    slot = ctx.catalog.read_skill(graph.id)
    assert slot is not None and slot.kind == "static"
    assert verify_codes(ctx) == [IWE]  # the first intent stays an orphan


def test_functions_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    """K6: the old slot (here: none) stays; a retry publishes and verifies."""
    create(ctx)
    run = lambda: publish_functions(  # noqa: E731
        ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.FUNCTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_functions(graph.id) is None
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    slot = ctx.catalog.read_functions(graph.id)
    assert slot is not None and slot.count == 2
    assert verify_codes(ctx) == [IWE]  # the first intent stays an orphan
    assert run().data["published"] is False  # unchanged: no third intent
    assert verify_codes(ctx) == [IWE]


def test_functions_replaced_before_rename_keep_the_old_catalog(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    one = tmp_path / "one"
    one.mkdir()
    (one / "items_in_location.yaml").write_bytes(
        (FUNCTIONS / "items_in_location.yaml").read_bytes()
    )
    run = lambda: publish_functions(  # noqa: E731
        ctx, graph_ref="example_graph", source=one, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.FUNCTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    old = ctx.catalog.read_functions(graph.id)
    assert old is not None and old.count == 2
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    new = ctx.catalog.read_functions(graph.id)
    assert new is not None and new.count == 1
    assert verify_codes(ctx) == [IWE]


def test_actions_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    """A8: the old slot (here: none) stays; a retry publishes and verifies."""
    create(ctx)
    run = lambda: publish_actions(  # noqa: E731
        ctx, graph_ref="example_graph", source=ACTIONS, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.ACTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_actions(graph.id) is None
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    slot = ctx.catalog.read_actions(graph.id)
    assert slot is not None and slot.count == 2
    assert verify_codes(ctx) == [IWE]  # the first intent stays an orphan
    assert run().data["published"] is False  # unchanged: no third intent
    assert verify_codes(ctx) == [IWE]


def test_actions_replaced_before_rename_keep_the_old_catalog(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    create(ctx)
    publish_actions(ctx, graph_ref="example_graph", source=ACTIONS, reason=None, dry_run=False)
    one = tmp_path / "one"
    one.mkdir()
    (one / "move_item.yaml").write_bytes((ACTIONS / "move_item.yaml").read_bytes())
    run = lambda: publish_actions(  # noqa: E731
        ctx, graph_ref="example_graph", source=one, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.ACTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    old = ctx.catalog.read_actions(graph.id)
    assert old is not None and old.count == 2
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    new = ctx.catalog.read_actions(graph.id)
    assert new is not None and new.count == 1
    assert verify_codes(ctx) == [IWE]


def test_an_interrupted_actions_publish_leaves_the_functions_slot_alone(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    before = ctx.catalog.read_functions(ctx.catalog.resolve("example_graph").id)
    run = lambda: publish_actions(  # noqa: E731
        ctx, graph_ref="example_graph", source=ACTIONS, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.ACTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_functions(graph.id) == before
    assert ctx.catalog.read_actions(graph.id) is None


def test_views_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    """V9: the old slot (here: none) stays; a retry publishes and verifies; what a kill -9 at
    the rename leaves is an announced intent and nothing else."""
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    run = lambda: publish_views(  # noqa: E731
        ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.VIEWS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_views(graph.id) is None
    assert not (ctx.catalog.graphs_dir / str(graph.id) / "views" / "slot.json").exists()
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    slot = ctx.catalog.read_views(graph.id)
    assert slot is not None and slot.count == 3
    assert verify_codes(ctx) == [IWE]  # the first intent stays an orphan
    assert run().data["published"] is False  # unchanged: no third intent
    assert verify_codes(ctx) == [IWE]


def test_views_replaced_before_rename_keep_the_old_catalog(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    publish_views(ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False)
    one = tmp_path / "one"
    one.mkdir()
    (one / "location_overview.yaml").write_bytes((VIEWS / "location_overview.yaml").read_bytes())
    run = lambda: publish_views(  # noqa: E731
        ctx, graph_ref="example_graph", source=one, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.VIEWS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    old = ctx.catalog.read_views(graph.id)
    assert old is not None and old.count == 3  # old, never mixed
    assert verify_codes(ctx) == [IWE]
    assert run().data["published"] is True
    new = ctx.catalog.read_views(graph.id)
    assert new is not None and new.count == 1
    assert verify_codes(ctx) == [IWE]


def test_an_interrupted_views_publish_leaves_the_other_slots_alone(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    publish_actions(ctx, graph_ref="example_graph", source=ACTIONS, reason=None, dry_run=False)
    graph = ctx.catalog.resolve("example_graph")
    functions, actions = ctx.catalog.read_functions(graph.id), ctx.catalog.read_actions(graph.id)
    run = lambda: publish_views(  # noqa: E731
        ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False
    )
    interrupted(faults, CrashPoint.VIEWS_BEFORE_RENAME, run)
    assert ctx.catalog.read_functions(graph.id) == functions
    assert ctx.catalog.read_actions(graph.id) == actions
    assert ctx.catalog.read_views(graph.id) is None


def test_a_push_interrupted_between_the_release_and_the_functions_is_stale_and_repeatable(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    """K5 defence in depth: the release is pinned, the catalog write is lost. The old catalog
    does not resolve against the new pin (``graph check`` reports ``function.stale``), and
    repeating the push completes the catalog."""
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    work = tmp_path / "work"
    sources.pull(ctx, graph_ref="example_graph", target=work, version=None, force=False)
    major = work / "ontology" / "2.0.0"
    major.mkdir()
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    text = text.replace("located_in", "stored_in").replace("version: 1.0.0", "version: 2.0.0")
    (major / "inventory.yaml").write_text(text)
    path = work / "functions" / "items_in_location.yaml"
    path.write_text(path.read_text().replace("located_in", "stored_in"))

    def run() -> sources_push.Pushed:
        return sources_push.push(
            ctx,
            graph_ref="example_graph",
            source=work,
            reason=None,
            dry_run=False,
            inventory=app_inventory(),
        )

    interrupted(faults, CrashPoint.FUNCTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert graph.pin.version == "2.0.0"
    old = ctx.catalog.read_functions(graph.id)
    assert old is not None and old.published_against.version == "1.0.0"
    with pytest.raises(GraphCliError) as caught:
        check(ctx, graph_ref="example_graph", source=None, module=None)
    assert caught.value.code == "admin.functions_broken"
    details = caught.value.details
    assert details is not None and details["functions"] == ["items_in_location"]
    codes = {p["code"] for p in details["problems"]}  # type: ignore[union-attr, index]
    assert "function.stale" in codes
    assert verify_codes(ctx) == [IWE]
    done = run()
    assert done.data["ontology"]["released"] is False  # pinned already
    assert done.data["functions"]["published"] is True  # type: ignore[index]
    check(ctx, graph_ref="example_graph", source=None, module=None)
    assert ctx.catalog.read_functions(graph.id) is not None


def test_a_push_interrupted_between_the_release_and_the_actions_is_stale_and_repeatable(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    """Defence in depth as for functions: the release is pinned, the actions write is lost.
    The old catalog does not compile against the new pin (``graph check`` reports
    ``action.stale``), and repeating the push completes the catalog."""
    create(ctx)
    publish_actions(ctx, graph_ref="example_graph", source=ACTIONS, reason=None, dry_run=False)
    work = tmp_path / "work"
    sources.pull(ctx, graph_ref="example_graph", target=work, version=None, force=False)
    major = work / "ontology" / "2.0.0"
    major.mkdir()
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    text = text.replace("located_in", "stored_in").replace("version: 1.0.0", "version: 2.0.0")
    (major / "inventory.yaml").write_text(text)
    path = work / "actions" / "move_item.yaml"
    path.write_text(path.read_text().replace("located_in", "stored_in"))

    def run() -> sources_push.Pushed:
        return sources_push.push(
            ctx,
            graph_ref="example_graph",
            source=work,
            reason=None,
            dry_run=False,
            inventory=app_inventory(),
        )

    interrupted(faults, CrashPoint.ACTIONS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert graph.pin.version == "2.0.0"
    old = ctx.catalog.read_actions(graph.id)
    assert old is not None and old.published_against.version == "1.0.0"
    with pytest.raises(GraphCliError) as caught:
        check(ctx, graph_ref="example_graph", source=None, module=None)
    assert caught.value.code == "admin.actions_broken"
    details = caught.value.details
    assert details is not None and details["actions"] == ["move_item"]
    codes = {p["code"] for p in details["problems"]}  # type: ignore[union-attr, index]
    assert "action.stale" in codes
    assert verify_codes(ctx) == [IWE]
    done = run()
    assert done.data["ontology"]["released"] is False  # pinned already
    assert done.data["actions"]["published"] is True  # type: ignore[index]
    check(ctx, graph_ref="example_graph", source=None, module=None)
    assert ctx.catalog.read_actions(graph.id) is not None


def test_a_push_interrupted_between_the_release_and_the_views_is_stale_and_repeatable(
    ctx: AdminContext, faults: FaultSwitch, tmp_path: Path
) -> None:
    """Defence in depth as for functions: the release and the functions are written, the views
    write is lost. The old catalog does not hold against the new pin (``graph check`` reports
    ``admin.views_broken``), and repeating the push completes the catalog."""
    create(ctx)
    publish_functions(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    publish_views(ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False)
    work = tmp_path / "work"
    sources.pull(ctx, graph_ref="example_graph", target=work, version=None, force=False)
    major = work / "ontology" / "2.0.0"
    major.mkdir()
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    text = text.replace("located_in", "stored_in").replace("version: 1.0.0", "version: 2.0.0")
    (major / "inventory.yaml").write_text(text)
    for folder, name in (("functions", "items_in_location"), ("views", "location_overview")):
        path = work / folder / f"{name}.yaml"
        path.write_text(path.read_text().replace("located_in", "stored_in"))

    def run() -> sources_push.Pushed:
        return sources_push.push(
            ctx,
            graph_ref="example_graph",
            source=work,
            reason=None,
            dry_run=False,
            inventory=app_inventory(),
        )

    interrupted(faults, CrashPoint.VIEWS_BEFORE_RENAME, run)
    graph = ctx.catalog.resolve("example_graph")
    assert graph.pin.version == "2.0.0"
    old = ctx.catalog.read_views(graph.id)
    assert old is not None and old.published_against.version == "1.0.0"
    with pytest.raises(GraphCliError) as caught:
        check(ctx, graph_ref="example_graph", source=None, module=None)
    assert caught.value.code == "admin.views_broken"
    details = caught.value.details
    assert details is not None and details["views"] == ["location_overview"]
    assert verify_codes(ctx) == [IWE]
    done = run()
    assert done.data["ontology"]["released"] is False  # pinned already
    assert done.data["views"]["published"] is True  # type: ignore[index]
    check(ctx, graph_ref="example_graph", source=None, module=None)
    republished = ctx.catalog.read_views(graph.id)
    assert republished is not None and republished.published_against.version == "2.0.0"
