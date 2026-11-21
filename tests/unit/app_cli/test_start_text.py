"""The text of ``graph start`` (graph start plan S2): at most 1,500 bytes for up to five graphs,
however many graphs, modules and functions the installation has, and every state it can report."""

from typing import Any

import pytest

from graph_cli.app_cli.commands.start import render

BUDGET = 1500


def graph(n: int, *, refused: bool = True) -> dict[str, Any]:
    """A graph as the answer holds it: a name of 24 characters, a long title, the counts of a big
    installation (50 modules, 200 functions), something not allowed of every kind."""
    item: dict[str, Any] = {
        "name": f"example_graph_name_{n:02d}",
        "title": "A title that is far longer than the text will show for any graph",
        "level": "write",
        "skill": f"graph-example-graph-name-{n:02d}",
        "counts": {"modules": 50, "types": 321, "functions": 200, "actions": 100, "views": 100},
    }
    if refused:
        item["not_allowed"] = {"functions": 200, "actions": 100, "views": 100}
    return item


def answer(graphs: int, *, skills: list[dict[str, str]] | None = None, **program: Any) -> Any:
    return {
        "schema": "graph_cli.start/1",
        "program": {
            "name": "graph",
            "version": "10.28.0",
            "state": "outdated",
            **program,
        },
        "skills": skills if skills is not None else [],
        "identity": {"acting": "an_agent_with_a_long_name", "project": "a_project", "roles": []},
        "graphs": [graph(n) for n in range(1, graphs + 1)],
        "hidden_count": 3,
    }


def refreshed(count: int) -> list[dict[str, str]]:
    return [
        {"scope": "project", "skill": f"graph-example-graph-name-{n:02d}", "state": s}
        for n, s in enumerate(["refreshed"] * count, 1)
    ]


@pytest.mark.parametrize("graphs", [0, 1, 3, 5, 6, 10, 40])
def test_the_text_fits_the_budget_for_every_number_of_graphs(graphs: int) -> None:
    text = render(answer(graphs, skills=refreshed(3)))
    assert len(text.encode()) <= BUDGET, (graphs, len(text.encode()), text)
    assert text.count("example_graph_name_") <= 5


def test_ten_graphs_name_five_and_point_to_the_list_for_the_rest() -> None:
    text = render(answer(10))
    assert text.count("example_graph_name_") == 5
    assert "and 5 more: graph graphs" in text
    assert "graph graphs" in text


def test_the_budget_holds_with_every_skill_state_at_once() -> None:
    """Skills in three states at once, a different server build; the lines that name copies are
    the ones that could grow, they are at most three and list two names each (counts of a graph
    of ordinary size: two digits; nothing hidden by the pointer)."""
    skills = [
        {"scope": "project", "skill": f"graph-example-graph-{n:02d}", "state": s}
        for n, s in enumerate(["refreshed", "modified", "foreign", "modified", "refreshed"], 1)
    ]
    data = answer(5, skills=skills)
    data["hidden_count"] = 0
    for item in data["graphs"]:
        item["counts"] = {"modules": 5, "types": 40, "functions": 20, "actions": 10, "views": 10}
        item["not_allowed"] = {"functions": 20, "actions": 10, "views": 10}
    text = render(data)
    assert len(text.encode()) <= BUDGET, (len(text.encode()), text)
    assert "and 1 more" in text  # three copies left alone: two named, one counted


def test_a_graph_the_caller_cannot_open_says_so_and_a_normal_one_is_plain() -> None:
    data = answer(1)
    data["graphs"] = [{**graph(1, refused=False), "counts": None}]
    assert "its model and catalogs are not open to you" in render(data)
    plain = answer(1)
    plain["graphs"] = [graph(1, refused=False)]
    line = next(ln for ln in render(plain).splitlines() if ln.startswith("  example_graph"))
    assert "not allowed" not in line and "functions 200" in line


def test_the_counts_name_what_may_not_be_used_next_to_what_may() -> None:
    text = render(answer(1))
    assert "actions 100 (+100 not allowed)" in text
    assert "functions 200 (+200 not allowed)" in text
