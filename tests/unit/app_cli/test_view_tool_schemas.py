"""The output schema of a view tool (tool manifest plan TM6, step 4): only the sections the view
declares, each by id; the rows of a ``rows`` section typed by the columns of its function; the keys
of the flavour of the view. Answers come from the real renderers over the example graph: an entity
view trimmed at every limit down to its stubs, the pages of a set view, a ``rows`` section that gave
an error. The schemas are closed like the test plugin closes them."""

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from support import tools
from support.output_schemas import closed
from support.snapshots import DictSnapshot, edge, revision
from support.view_answers import ENTITY, Doc, head, problems

from graph_cli.app_cli import tool_manifest as tm
from graph_cli.app_cli import view_schemas as vs
from graph_cli.app_cli.column_schemas import column_schema, entity_schema
from graph_cli.app_cli.output_schemas import standalone
from graph_cli.kernel.records import Revision
from graph_cli.kinetic.card import ROWS_CELL_CHARS, render, spec_of
from graph_cli.kinetic.return_types import ColumnType
from graph_cli.kinetic.view_page import cursor_key, decode_cursor, render_set
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.ontology_layer.vocabulary import LOWER_NAME_RE

SCENE = tools.company().scene()
BY_NAME = {e.name: e for e in SCENE.views}


def tool_schema(name: str) -> dict[str, Any]:
    """The output schema of the tool of view ``name`` in the manifest of the example graph."""
    (found,) = tm.heads(
        SCENE.model, SCENE.functions, SCENE.actions, SCENE.views, frozenset(), only=f"view_{name}"
    )
    schema = tm.tool_json(found, SCENE.model)["output_schema"]
    assert isinstance(schema, dict)
    return schema


# ---- authored views over the company example ----------------------------------------------


def team() -> Revision:
    return revision(
        "team",
        "org:Team",
        {"name": "Reliability", "alt_labels": ["SRE", "Ops"]},
        [edge("org:part_of", "dept"), edge("org:charged_to", "cc")],
        alias="TEAM-1",
    )


def world() -> list[Revision]:
    found = [
        team(),
        revision("dept", "org:Department", {"name": "Engineering"}, alias="DEPT-1"),
        revision("cc", "org:CostCenter", {"name": "Platform", "code": "CC-10"}, alias="CC-10"),
        revision(
            "pol",
            "ops:Policy",
            {"title": "Security", "status": "in_force"},
            [edge("ops:applies_to", "team")],
            alias="POL-1",
        ),
    ]
    for n in range(8):
        found.append(
            revision(
                f"p{n}",
                "org:Position",
                {"title": f"Role {n}", "status": "active"},
                [edge("org:in_unit", "team")],
                alias=f"POS-{n}",
            )
        )
        found.append(
            revision(
                f"e{n}",
                "ppl:Employee",
                {
                    "name": f"Person {n}",
                    "status": "active",
                    "employee_number": f"E-{n}",
                    "work_email": f"p{n}@example.org",
                },
                [edge("org:holds", f"p{n}", since="2024-02-01")],
                alias=f"EMP-{n}",
            )
        )
    return found


MEMBERS = RowsResult(
    "members_of_unit",
    "sha256:fn",
    ("person", "position", "unit"),
    tuple(
        {"person": f"Person {n}", "position": f"Role {n}", "unit": "Reliability"} for n in range(8)
    ),
)


def unit_views(limit: int | None = None, **given: Any) -> Doc:
    entry = BY_NAME["unit_overview"]
    spec = spec_of(entry, SCENE.model, given)
    if limit is not None:
        spec = replace(spec, byte_limit=limit)
    entity, *others = world()
    snapshot = DictSnapshot(entity, *others)
    rows = {"members": MEMBERS}
    return dict(render(spec, entity, snapshot, SCENE.model, rows=rows).to_json())


def test_an_entity_view_answers_what_its_tool_schema_says() -> None:
    schema = tool_schema("unit_overview")
    document = unit_views()
    assert [s["kind"] for s in document["sections"]] == [
        "fields",
        "relations",
        "neighbors",
        "rows",
        "affordances",
    ]
    assert problems(schema, document) == []
    assert problems(schema, unit_views(status="planned")) == []


def test_a_trimmed_entity_view_matches_its_schema_at_every_limit_down_to_the_stubs() -> None:
    schema = tool_schema("unit_overview")
    seen = set()
    for limit in range(1024, 120, -17):
        document = unit_views(limit)
        assert problems(schema, document) == [], limit
        seen |= {"cut"} if document.get("cut") else set()
        seen |= {"stub"} if any(s.get("cut") for s in document["sections"]) else set()
        seen |= {"sections_cut"} if document.get("sections_cut") else set()
    assert seen == {"cut", "stub", "sections_cut"}


def test_a_rows_section_that_gave_an_error_matches_the_error_form() -> None:
    schema = tool_schema("unit_overview")
    entry = BY_NAME["unit_overview"]
    entity, *others = world()
    snapshot = DictSnapshot(entity, *others)
    spec = spec_of(entry, SCENE.model, {})
    for rows in (
        {},  # not run: view.section_unavailable
        {
            "members": RowsResult(
                "members_of_unit", "sha256:fn", error=("function.stale", "m" * 500)
            )
        },
    ):
        document = dict(render(spec, entity, snapshot, SCENE.model, rows=rows).to_json())
        (failed,) = [s for s in document["sections"] if s["kind"] == "rows"]
        assert set(failed) >= {"error", "function"} and "items" not in failed
        assert problems(schema, document) == []


# ---- set views: pages ---------------------------------------------------------------------


def catalog_world() -> list[Revision]:
    found = [revision("dept", "org:Department", {"name": "Engineering"}, alias="DEPT-1")]
    for u in range(6):
        found.append(
            revision(
                f"team{u}",
                "org:Team",
                {"name": f"Team {u:02d}"},
                [edge("org:part_of", "dept")],
                alias=f"TEAM-{u:02d}",
            )
        )
        found.append(
            revision(
                f"pos{u}",
                "org:Position",
                {"title": f"Role {u:02d}", "status": "active"},
                [edge("org:in_unit", f"team{u}")],
                alias=f"POS-{u:02d}",
            )
        )
        found.append(
            revision(
                f"emp{u}",
                "ppl:Employee",
                {"name": f"Person {u}", "status": "active", "employee_number": f"E-{u}"},
                [edge("org:holds", f"pos{u}", since="2024-01-01")],
                alias=f"EMP-{u:02d}",
            )
        )
    return found


COMPLETIONS = RowsResult(
    "training_completions",
    "sha256:fn",
    ("person", "training", "completed_on"),
    tuple(
        {"person": f"P{i:02d}", "training": "Safety", "completed_on": "2026-01-01"}
        for i in range(5)
    ),
)


def catalog_pages(limit: int, rows: RowsResult = COMPLETIONS) -> list[Doc]:
    entry = BY_NAME["org_catalog"]
    spec = spec_of(entry, SCENE.model, {})
    key = cursor_key(entry.hash, {})
    first, *others = catalog_world()
    snapshot = DictSnapshot(first, *others)
    found: list[Doc] = []
    cursor = None
    while True:
        model = render_set(
            spec,
            snapshot,
            SCENE.model,
            rows={"completions": rows},
            page_bytes=limit,
            cursor=cursor,
            key=key,
        )
        data = dict(model.to_json())
        found.append(data)
        if "next" not in data:
            return found
        cursor = decode_cursor(data["next"])


def test_every_page_of_a_set_view_matches_its_tool_schema() -> None:
    schema = tool_schema("org_catalog")
    for limit in (1024, 1500, 3000, 262_144):
        pages = catalog_pages(limit)
        assert [("next" in p) for p in pages] == [True] * (len(pages) - 1) + [False]
        for page in pages:
            assert problems(schema, page) == [], limit
    assert len(catalog_pages(1024)) > 3


def test_a_page_that_starts_inside_a_section_says_where() -> None:
    schema = tool_schema("org_catalog")
    pages = catalog_pages(1024)
    assert any("from" in s for p in pages for s in p["sections"])
    assert all(problems(schema, p) == [] for p in pages)


def test_a_page_of_a_set_view_with_a_failed_section_matches_the_error_form() -> None:
    schema = tool_schema("org_catalog")
    failed = RowsResult("training_completions", "sha256:fn", error=("function.stale", "m"))
    for page in catalog_pages(3000, failed):
        assert problems(schema, page) == []


# ---- the schema of one view -----------------------------------------------------------------


def alternatives(schema: Mapping[str, Any]) -> list[Doc]:
    found: list[Doc] = schema["properties"]["sections"]["items"]["anyOf"]
    return found


def test_a_view_schema_lists_only_the_sections_the_view_declares() -> None:
    listed = {
        (v["properties"]["kind"]["const"], v["properties"]["id"]["const"])
        for v in alternatives(tool_schema("unit_overview"))
    }
    assert listed == {
        ("fields", "fields"),
        ("relations", "relations"),
        ("neighbors", "positions"),
        ("rows", "members"),
        ("files", "files"),
        ("affordances", "affordances"),
    }
    only = tool_schema("tasks_by_state")
    assert {v["properties"]["kind"]["const"] for v in alternatives(only)} == {"list"}
    assert problems(only, head([{"kind": "fields", "id": "fields"}], name="tasks_by_state")) != []
    assert (
        problems(
            only,
            head([{"kind": "list", "id": "other", "type": "t", "count": 0}], name="tasks_by_state"),
        )
        != []
    )


def test_each_variant_of_a_kind_is_in_the_schema_of_a_view_that_declares_it() -> None:
    schema = tool_schema("unit_overview")

    def asked(*sections: Doc) -> list[str]:
        return problems(schema, head(list(sections), name="unit_overview", entity=ENTITY))

    assert asked({"kind": "files", "id": "files", "items": [{"ref": "F"}]}) == []
    cited = {"kind": "files", "id": "files", "variant": "cited_by", "count": 1}
    assert asked(cited) == []
    assert asked({"kind": "rows", "id": "members", "cut": True}) == []
    assert asked({"kind": "fields", "id": "fields", "cut": True}) == []
    assert asked({"kind": "rows", "id": "rows", "cut": True}) != []  # the section is members
    assert asked({"kind": "list", "id": "members", "type": "t", "count": 0}) != []  # not declared


def test_an_entity_view_has_the_entity_and_no_pages_and_a_set_view_the_reverse() -> None:
    entity = tool_schema("unit_overview")
    sets = tool_schema("tasks_by_state")
    assert set(entity["required"]) == {"view", "entity", "sections", "bytes"}
    assert set(sets["required"]) == {"view", "sections", "bytes"}
    assert {"cut", "sections_cut"} <= set(entity["properties"])
    assert not {"next", "more", "oversize"} & set(entity["properties"])
    assert {"next", "more", "oversize"} <= set(sets["properties"])
    assert not {"entity", "cut", "sections_cut"} & set(sets["properties"])
    assert "_cursor" in sets["properties"]["next"]["description"]
    assert "view.cursor_stale" in sets["properties"]["next"]["description"]
    for schema, name in ((entity, "unit_overview"), (sets, "tasks_by_state")):
        assert schema["properties"]["view"]["properties"]["name"] == {"const": name}
        assert schema["properties"]["view"]["properties"]["origin"] == {"const": "authored"}


def test_a_view_schema_stands_alone_and_is_valid() -> None:
    for entry in SCENE.views:
        schema = tool_schema(entry.name)
        Draft202012Validator.check_schema(schema)
        assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema), entry.name


# ---- rows are typed by the columns of their function ----------------------------------------


def rows_item(schema: Mapping[str, Any], section_id: str) -> Doc:
    """The schema of one row of the section ``section_id`` (its form with rows)."""
    for variant in alternatives(schema):
        found = variant["properties"]
        is_it = found["kind"] == {"const": "rows"} and found["id"] == {"const": section_id}
        if is_it and "columns" in found:
            item: Doc = found["items"]["items"]
            return item
    raise AssertionError(section_id)


def test_the_rows_of_a_section_are_typed_by_the_columns_of_the_function() -> None:
    set_view = tool_schema("org_catalog")
    row = rows_item(set_view, "completions")
    assert set(row["properties"]) == {"person", "training", "completed_on"}
    assert '"format": "date"' in json.dumps(row["properties"]["completed_on"])
    section = {
        "kind": "rows",
        "id": "completions",
        "function": {"name": "training_completions"},
        "columns": ["person", "training", "completed_on"],
        "count": 1,
        "items": [{"person": "P", "training": "T", "completed_on": "2026-01-01"}],
    }

    def asked(items: list[Doc]) -> list[str]:
        document = head([{**section, "items": items}], name="org_catalog", next="v1", more=True)
        return problems(set_view, document)

    assert asked(section["items"]) == []
    assert asked([{"person": "P", "training": "T", "completed_on": 5}]) != []
    assert asked([{"person": "P", "training": "T"}]) != []
    other = {**section, "function": {"name": "another_function"}}  # the section's function is fixed
    assert problems(set_view, head([other], name="org_catalog")) != []
    # a set view cuts nothing, so a value the grammar of its column refuses is refused
    strict = Draft202012Validator(
        closed(set_view), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    odd = head(
        [{**section, "items": [{"person": "P", "training": "T", "completed_on": "later"}]}],
        name="org_catalog",
    )
    assert not strict.is_valid(odd)


def test_an_entity_view_cuts_text_cells_but_only_a_decimal_loses_its_pattern() -> None:
    schema = tool_schema("unit_overview")
    cut = [{"person": "x" * 200 + "…", "position": "y", "unit": None}]
    section = {
        "kind": "rows",
        "id": "members",
        "function": {"name": "members_of_unit"},
        "columns": ["person", "position", "unit"],
        "count": 1,
        "items": cut,
        "cells_cut": 1,
    }
    document = head([section], name="unit_overview", entity=ENTITY)
    assert problems(schema, document) == []
    cells = [{"person": 1, "position": "y", "unit": None}]
    wrong = head([{**section, "items": cells}], name="unit_overview", entity=ENTITY)
    assert problems(schema, wrong) != []


def test_a_set_view_keeps_the_decimal_pattern_and_an_entity_view_does_not() -> None:
    decimal = ColumnType("decimal", nullable=False)
    assert "pattern" in column_schema(decimal, "S.", SCENE.model)
    assert "pattern" not in column_schema(decimal, "S.", SCENE.model, cut=True)
    entity = entity_schema("org:Position", SCENE.model, cut=True)
    assert "pattern" not in json.dumps(entity["properties"]["attrs"])


def test_nothing_but_a_decimal_can_be_longer_than_the_cut_of_an_entity_view() -> None:
    """An entity view cuts a cell at ``ROWS_CELL_CHARS``; the enum, reference and date cells keep
    their grammar because none of them can be that long. This breaks loudly if the cut moves
    below the longest of them."""
    enum_name = int(re.search(r"\{0,(\d+)\}", LOWER_NAME_RE.pattern)[1]) + 1  # type: ignore[index]
    reference = len(f"urn:uuid:{uuid4()}")
    stamp = len("9999-12-31T23:59:59.999999999+00:00")
    assert max(enum_name, reference, stamp) < ROWS_CELL_CHARS
    for model in (SCENE.model, tools.inventory().model, tools.staff().model):
        for found in model.enums.values():
            assert all(len(v.name) <= ROWS_CELL_CHARS for v in found.values)


def test_a_rows_section_without_typed_columns_is_any_object() -> None:
    entry = BY_NAME["unit_overview"]
    sections = vs.view_schema(
        entry.name,
        [vs.SectionShape("rows", "members", "members_of_unit", None)],
        entity=True,
    )
    standalone_schema = standalone(sections)
    item = rows_item(standalone_schema, "members")
    assert item == {"type": "object", "additionalProperties": {}}
    rows = {
        "kind": "rows",
        "id": "members",
        "function": {"name": "members_of_unit"},
        "columns": ["a"],
        "count": 1,
        "items": [{"anything": [1]}],
    }
    assert problems(standalone_schema, head([rows], name=entry.name, entity=ENTITY)) == []


def test_a_view_without_the_function_of_a_rows_section_still_has_a_schema() -> None:
    """The defensive path: a view whose function is not among the functions handed to the manifest
    (describe and call never hand such a catalog) still has a schema, its rows any object."""
    no_functions: tuple[Any, ...] = ()
    (view,) = tm.heads(
        SCENE.model,
        no_functions,
        (),
        (BY_NAME["unit_overview"],),
        frozenset(),
    )
    assert [s.columns for s in view.sections if s.kind == "rows"] == [None]
    schema = tm.tool_json(view, SCENE.model)["output_schema"]
    Draft202012Validator.check_schema(schema)


# ---- a caller who sees less ---------------------------------------------------------------

CARDS = """\
schema: graph_cli.function/1
name: employee_cards
summary: Employees with their shift.
use_when: Who works which shift.
query: |
  MATCH (e:Employee) RETURN e AS employee, e.shift AS shift
returns:
  - column: employee
    summary: The employee.
  - column: shift
    summary: The shift.
examples:
  - question: Who works which shift?
"""
BOARD = """\
schema: graph_cli.view/1
name: {name}
summary: The employees with their shift.
use_when: Who works which shift, as a view.
{takes}sections:
  - kind: rows
    id: cards
    function: employee_cards
"""
STAFF = tools.staff(
    {"employee_cards.yaml": CARDS},
    {
        "board.yaml": BOARD.format(name="board", takes=""),
        "employee_board.yaml": BOARD.format(name="employee_board", takes="takes: stf:Employee\n"),
    },
)


def staff_cells(reader: tools.Scene, view: str) -> dict[str, Any]:
    """The schema of the row of the ``rows`` section of a view tool, for one caller."""
    (found,) = tm.heads(
        reader.model,
        reader.functions,
        reader.actions,
        reader.views,
        frozenset(),
        only=f"view_{view}",
    )
    schema = tm.tool_json(found, reader.model)["output_schema"]
    assert isinstance(schema, dict)
    return {"row": rows_item(schema, "cards"), "text": json.dumps(schema)}


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_rows_of_a_view_tool_show_the_attributes_and_values_the_caller_sees() -> None:
    """The rows of a view tool carry entity attributes and enum values rendered from the model
    of the caller, in a set view and in an entity view (which cuts text): a caller without
    markings has no marked attribute in its schema, one with them has."""
    for view in ("board", "employee_board"):
        everything = staff_cells(STAFF.scene(), view)
        personal = staff_cells(STAFF.reader(held=tools.PERSONAL), view)
        nothing = staff_cells(STAFF.reader(held=tools.NOTHING), view)

        def attrs(found: dict[str, Any]) -> set[str]:
            return set(found["row"]["properties"]["employee"]["properties"]["attrs"]["properties"])

        assert {"email", "salary", "badge_code", "name", "shift"} <= attrs(everything), view
        assert "email" in attrs(personal) and "salary" not in attrs(personal), view
        assert attrs(nothing) == {"name", "phone", "shift", "stage"}, view
        for word in ("salary", "badge_code", "email", "birth_date", "pay_grade"):
            assert word not in nothing["text"], (view, word)
        assert nothing["row"]["properties"]["shift"]["anyOf"][0]["enum"] == ["day", "night"]
