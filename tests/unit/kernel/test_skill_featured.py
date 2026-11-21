"""What a graph skill shows of the catalogs, as a digest (graph start plan S6)."""

from typing import Any

from graph_cli.kernel.skill_featured import catalog_digest, kind_digest

PARAM = {"name": "unit", "type": "ref", "class": "org:OrgUnit", "many": False}


def entry(name: str, **more: Any) -> dict[str, Any]:
    return {
        "name": name,
        "module": "organization",
        "summary": "Members.",
        "status": "active",
        "featured": False,
        "params": [PARAM],
        **more,
    }


def functions(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "graph_cli.functions/1", "functions": list(entries)}


def actions(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "graph_cli.action_catalog/1", "actions": list(entries)}


def test_what_the_render_does_not_show_does_not_move_the_digest() -> None:
    base = functions(entry("a", featured=True), entry("b"))
    same = kind_digest("functions", base)
    # a function that is not featured: new, changed, removed, other module, deprecated
    assert kind_digest("functions", functions(entry("a", featured=True), entry("c"))) == same
    assert (
        kind_digest("functions", functions(entry("a", featured=True), entry("b", summary="X")))
        == same
    )
    assert kind_digest("functions", functions(entry("a", featured=True))) == same
    assert (
        kind_digest(
            "functions",
            functions(entry("a", featured=True), entry("b"), entry("x", status="deprecated")),
        )
        == same
    )
    # the order of the file and unrelated keys do not matter
    shuffled = functions(entry("b", hash="x"), entry("a", featured=True, query="MATCH"))
    assert kind_digest("functions", shuffled) == same


def test_what_the_render_shows_moves_the_digest() -> None:
    base = kind_digest("functions", functions(entry("a", featured=True)))
    for changed in (
        entry("a", featured=False),  # unfeatured: the line leaves
        entry("a", featured=True, summary="Other."),
        entry("a", featured=True, module="people"),
        entry("a", featured=True, status="deprecated"),
        entry("a", featured=True, params=[]),
        entry("z", featured=True),
    ):
        assert kind_digest("functions", functions(changed)) != base, changed
    assert (
        kind_digest("functions", functions(entry("a", featured=True), entry("b", featured=True)))
        != base
    )


def test_presence_flips_with_the_first_and_the_last_active_entry() -> None:
    nothing = kind_digest("functions", None)
    assert kind_digest("functions", functions()) == nothing  # an empty catalog is no catalog
    assert kind_digest("functions", functions(entry("a", status="deprecated"))) == nothing
    assert kind_digest("functions", functions(entry("a"))) != nothing


def test_an_action_line_also_shows_the_reason_rule_and_a_view_only_its_presence() -> None:
    base = kind_digest("actions", actions(entry("a", featured=True, reason="optional")))
    assert kind_digest("actions", actions(entry("a", featured=True, reason="required"))) != base
    assert kind_digest("actions", actions(entry("a", reason="required"))) == kind_digest(
        "actions", actions(entry("a", reason="optional"))
    )  # not featured: no line
    views = {"schema": "graph_cli.view_catalog/1", "views": [entry("v", featured=True)]}
    renamed = {"schema": "graph_cli.view_catalog/1", "views": [entry("w", summary="Other.")]}
    assert kind_digest("views", views) == kind_digest("views", renamed)  # views have no lines
    assert kind_digest("views", views) != kind_digest("views", None)


def test_the_kinds_are_told_apart_and_combined() -> None:
    f = kind_digest("functions", functions(entry("a")))
    a = kind_digest("actions", actions(entry("a")))
    v = kind_digest("views", None)
    assert len({f, a, v}) == 3
    assert catalog_digest(f, a, v) != catalog_digest(a, f, v)
    assert catalog_digest(f, a, v) == catalog_digest(f, a, v)


def test_a_reader_who_may_not_use_functions_has_a_digest_of_its_own() -> None:
    nothing = kind_digest("functions", None)
    withheld = {**functions(), "withheld": True}
    assert kind_digest("functions", withheld) != nothing  # another line is rendered
    assert kind_digest("functions", {**functions(entry("a", featured=True)), "withheld": True}) != (
        kind_digest("functions", functions(entry("a", featured=True)))
    )
    assert kind_digest("functions", {**functions(), "withheld": False}) == nothing
