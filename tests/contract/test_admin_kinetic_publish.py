"""Publishing functions, actions and views re-checks under the graph lock (W.6): what the first,
lock-free check saw may be stale when the lock is taken."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support.admin_context import admin_context
from support.fixtures import FIXTURES, ONTOLOGIES

from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin import (
    actions,
    authored_views,
    functions,
    graphs,
    kinetic_slots,
)
from graph_cli_admin.services_admin.context import AdminContext

FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
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


def test_functions_removed_after_the_first_check_still_block_the_view(
    ctx: AdminContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    graph = ctx.catalog.resolve("example_graph")
    slot = ctx.catalog.read_functions(graph.id)
    seen = authored_views.function_entries(slot)
    assert seen
    calls = {"functions": 0, "stamp": 0}

    def published(_ctx: AdminContext, _entry: object) -> tuple[object, ...]:
        calls["functions"] += 1
        return seen if calls["functions"] == 1 else ()  # the functions went away in between

    def stamp(_read: object, _kind: object) -> tuple[str | None, ...]:
        calls["stamp"] += 1
        return (f"state{calls['stamp']}",)

    monkeypatch.setattr(authored_views, "published_functions", published)
    monkeypatch.setattr(kinetic_slots, "watched", stamp)
    with pytest.raises(GraphCliError) as caught:
        authored_views.publish(
            ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False
        )
    assert caught.value.code == "admin.views_invalid"
    assert calls["functions"] == 2  # the lock-time check ran again
    assert ctx.catalog.read_views(graph.id) is None


def test_an_unchanged_function_catalog_is_not_checked_twice(
    ctx: AdminContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    real = authored_views.published_functions
    calls = {"functions": 0}

    def counted(context: AdminContext, entry: object) -> tuple[object, ...]:
        calls["functions"] += 1
        return real(context, entry)  # type: ignore[arg-type]

    monkeypatch.setattr(authored_views, "published_functions", counted)
    authored_views.publish(ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False)
    assert (
        calls["functions"] == 2
    )  # the check, and the warnings of the published slot (no re-check)


def test_a_view_published_after_the_first_check_still_blocks_the_functions(
    ctx: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    authored_views.publish(ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False)
    only = tmp_path / "only"
    only.mkdir()
    (only / "items_by_condition.yaml").write_bytes(
        (FUNCTIONS / "items_by_condition.yaml").read_bytes()
    )
    # the lock-free read sees no views: the publish of the views "came in between"
    reads = {"n": 0}

    def no_views(_ctx: AdminContext, _entry: object) -> None:
        reads["n"] += 1
        return None if reads["n"] == 1 else ctx.catalog.read_views(graph.id)

    graph = ctx.catalog.resolve("example_graph")
    monkeypatch.setattr(functions, "published_views", no_views)
    calls = {"stamp": 0}
    monkeypatch.setattr(
        kinetic_slots,
        "watched",
        lambda *_a: (calls.__setitem__("stamp", calls["stamp"] + 1) or f"state{calls['stamp']}",),
    )
    with pytest.raises(GraphCliError) as caught:
        functions.publish(ctx, graph_ref="example_graph", source=only, reason=None, dry_run=False)
    assert caught.value.code == "admin.views_broken"
    slot = ctx.catalog.read_functions(graph.id)
    assert slot is not None and slot.count == 2  # the old catalog stays


def one_function(tmp_path: Path) -> Path:
    """The functions folder without ``items_by_condition``."""
    folder = tmp_path / "one_function"
    folder.mkdir()
    (folder / "items_in_location.yaml").write_bytes(
        (FUNCTIONS / "items_in_location.yaml").read_bytes()
    )
    return folder


def between(
    ctx: AdminContext, monkeypatch: pytest.MonkeyPatch, action: Callable[[], object]
) -> None:
    """Run ``action`` once, right before the publish under test takes the graph lock: no stamp is
    replaced, so the real comparison of what the first check saw with what the lock sees runs."""
    real = ctx.maintenance.maintenance_session
    done: list[bool] = []

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not done:
            done.append(True)
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(ctx.maintenance, "maintenance_session", wrapped)


def test_functions_that_shrink_before_the_views_take_the_lock_refuse_the_views(
    ctx: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    shrunk = one_function(tmp_path)
    between(
        ctx,
        monkeypatch,
        lambda: functions.publish(
            ctx, graph_ref="example_graph", source=shrunk, reason=None, dry_run=False
        ),
    )
    with pytest.raises(GraphCliError) as caught:
        authored_views.publish(
            ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False
        )
    assert caught.value.code == "admin.views_invalid"
    graph = ctx.catalog.resolve("example_graph")
    assert ctx.catalog.read_views(graph.id) is None
    functions_slot = ctx.catalog.read_functions(graph.id)
    assert functions_slot is not None and functions_slot.count == 1


def test_views_published_before_the_functions_take_the_lock_refuse_the_functions(
    ctx: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions.publish(ctx, graph_ref="example_graph", source=FUNCTIONS, reason=None, dry_run=False)
    shrunk = one_function(tmp_path)
    between(
        ctx,
        monkeypatch,
        lambda: authored_views.publish(
            ctx, graph_ref="example_graph", source=VIEWS, reason=None, dry_run=False
        ),
    )
    with pytest.raises(GraphCliError) as caught:
        functions.publish(ctx, graph_ref="example_graph", source=shrunk, reason=None, dry_run=False)
    assert caught.value.code == "admin.views_broken"
    graph = ctx.catalog.resolve("example_graph")
    functions_slot, views_slot = (
        ctx.catalog.read_functions(graph.id),
        ctx.catalog.read_views(graph.id),
    )
    assert functions_slot is not None and functions_slot.count == 2
    assert views_slot is not None and views_slot.count == 3
