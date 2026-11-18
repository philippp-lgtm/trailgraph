"""The text of ``graph view`` for the sections of authored views, and the hint to the next page
of a set view (object views stage 2)."""

from graph_cli.app_cli.commands.view_text import view_hints, view_text

ENTITY = {
    "id": "0192-1",
    "alias": "UNIT-1",
    "type": "org:Team",
    "rev": 2,
    "record_status": "active",
}
ROW = {
    "ref": "UNIT-2",
    "label": "Platform",
    "type": "org:Team",
    "fields": {"name": "Platform", "size": 4},
    "relations": [
        {
            "relation": "org:part_of",
            "direction": "out",
            "count": 2,
            "items": [{"ref": "UNIT-1", "label": "Engineering"}],
            "more": 1,
        }
    ],
}


def test_a_set_view_has_a_title_line_and_a_line_per_row() -> None:
    data = {
        "view": {"name": "org_catalog", "origin": "authored", "hash": "sha256:x"},
        "sections": [
            {
                "kind": "list",
                "id": "units",
                "title": "Units",
                "type": ["org:Team", "org:Department"],
                "items": [ROW],
                "count": 12,
                "from": 4,
                "approx": True,
                "unread": 3,
            },
            {
                "kind": "rows",
                "id": "vacancies",
                "function": {"name": "vacant_positions", "hash": "sha256:y"},
                "columns": ["position", "unit"],
                "items": [{"position": "SRE", "unit": "Engineering"}],
                "count": 5,
                "more": 4,
                "cells_cut": 1,
            },
            {
                "kind": "rows",
                "id": "broken",
                "function": {"name": "gone"},
                "error": {"code": "function.stale", "message": "no longer fits"},
            },
            {
                "kind": "neighbors",
                "id": "tasks",
                "relation": "ops:has_task",
                "direction": "in",
                "count": 0,
            },
            {"kind": "files", "id": "stub", "cut": True},
        ],
        "sections_cut": 2,
        "next": "v1.7.0.4.abcdef01",
        "more": True,
        "oversize": True,
        "bytes": 900,
    }
    lines = view_text(data).splitlines()
    assert lines[0] == "view org_catalog"
    assert (
        lines[1]
        == 'units "Units": list org:Team, org:Department (~12, from row 4, 3 edges not read)'
    )
    assert lines[2] == (
        "  UNIT-2 Platform (org:Team)  name=Platform size=4; org:part_of -> UNIT-1 Engineering (+1)"
    )
    assert lines[3] == "vacancies: rows vacant_positions (5, +4, 1 cells cut)"
    assert lines[4] == "  position=SRE unit=Engineering"
    assert lines[5] == "broken: rows gone failed"
    assert lines[6] == "  function.stale: no longer fits"
    assert lines[7] == "tasks: neighbors ops:has_task in (0)"
    assert "stub: cut to fit the size limit" in lines
    assert "2 more sections cut to fit the size limit" in lines
    assert "oversize: one row is larger than the page limit" in lines
    assert lines[-1] == "more rows follow; next page: --cursor v1.7.0.4.abcdef01"


def test_an_entity_view_keeps_the_card_lines_and_adds_its_sections() -> None:
    data = {
        "view": {"name": "unit_overview", "origin": "authored", "hash": "sha256:x"},
        "entity": ENTITY,
        "sections": [
            {
                "kind": "neighbors",
                "id": "members",
                "relation": "org:in_unit",
                "direction": "in",
                "items": [{"ref": "POS-1", "label": "Lead", "fields": {"title": "Lead"}}],
                "count": 1,
            },
        ],
        "bytes": 300,
    }
    lines = view_text(data).splitlines()
    assert lines[0] == "UNIT-1  (org:Team, rev 2, active)"
    assert lines[1] == "members: neighbors org:in_unit in (1)"
    assert lines[2] == "  POS-1 Lead  title=Lead"


def test_the_hint_of_a_set_view_repeats_what_the_cursor_is_tied_to() -> None:
    data = {
        "view": {"name": "v", "origin": "authored"},
        "sections": [],
        "next": "v1.1.0.2.00ff00ff",
        "bytes": 1,
    }
    [hint] = view_hints(
        data,
        "example_graph",
        name="v",
        params=("a=1", "b=x y"),
        page_bytes=4096,
        pinned=(("--as-of", ("tx:7",)),),
    )
    assert hint["command"] == (
        "graph view --graph example_graph --name v --cursor v1.1.0.2.00ff00ff"
        " --param a=1 --param 'b=x y' --page-bytes 4096 --as-of tx:7"
    )
    assert view_hints({**data, "next": None}, "example_graph", name="v") == ()
    assert (
        view_hints({k: v for k, v in data.items() if k != "next"}, "example_graph", name="v") == ()
    )
