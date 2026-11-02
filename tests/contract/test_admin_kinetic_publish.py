"""Publishing functions and actions re-checks under the graph lock (W.6): what the first,
lock-free check saw may be stale when the lock is taken."""

from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import FIXTURES, ONTOLOGIES

from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin import actions, functions, graphs, kinetic_slots
from graph_cli_admin.services_admin.context import AdminContext

FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
CLASH = FIXTURES / "actions" / "inventory" / "broken"  # items_in_location, named like a function


@pytest.fixture
def ctx(tmp_path: Path) -> AdminContext:
    context = admin_context(tmp_path / "root")
    graphs.create(
        context,
        name="example_graph",
        source=ONTOLOGIES / "inventory" / "1.0.0",
        module=None,
        title=None,
        language=None,
    )
    return context


def only_clash(tmp_path: Path) -> Path:
    folder = tmp_path / "clash"
    folder.mkdir()
    (folder / "items_in_location.yaml").write_bytes((CLASH / "items_in_location.yaml").read_bytes())
    return folder


def test_a_function_published_after_the_first_check_still_blocks_the_action(
    ctx: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    real = kinetic_slots.published_names
    # The lock-free read sees no functions: the publish of the functions "came in between".
    monkeypatch.setattr(kinetic_slots, "published_names", lambda *a, **k: frozenset())
    with pytest.raises(GraphCliError) as caught:
        actions.publish(
            ctx, graph_ref="example_graph", source=only_clash(tmp_path), reason=None, dry_run=False
        )
    assert caught.value.code == "admin.actions_invalid"
    assert caught.value.details["problems"][0]["code"] == "action.name_taken"  # type: ignore[index]
    monkeypatch.setattr(kinetic_slots, "published_names", real)
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_actions(graph.id) is None


def test_an_action_published_after_the_first_check_still_blocks_the_function(
    ctx: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions.publish(
        ctx, graph_ref="example_graph", source=only_clash(tmp_path), reason=None, dry_run=False
    )
    monkeypatch.setattr(kinetic_slots, "published_names", lambda *a, **k: frozenset())
    with pytest.raises(GraphCliError) as caught:
        functions.publish(
            ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False
        )
    assert caught.value.code == "admin.functions_invalid"
    assert caught.value.details["problems"][0]["code"] == "function.name_taken"  # type: ignore[index]
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_functions(graph.id) is None
