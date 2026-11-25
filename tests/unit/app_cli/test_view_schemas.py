"""The schemas of the view model (tool manifest plan TM6, review S2): one per section kind and
variant, derived from the code that builds the answer, in the general schema of the ``view``
command. The tests close every schema like the test plugin does, so a key the code adds without
declaring it fails; answers come from the real renderer over the notes ontology (the cards the
byte guard makes at every limit)."""

import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator
from support.card_scenarios import Scenario, scenarios
from support.view_answers import Doc, head, problems

from graph_cli.app_cli import view_schemas as vs
from graph_cli.app_cli.output_schemas import DEFS, standalone
from graph_cli.cli_kit.output_schemas import ref
from graph_cli.kinetic import card
from graph_cli.kinetic.card import generated_spec, render

GENERAL = standalone(ref("view_model"))


# ---- the general schema ------------------------------------------------------------------


def test_the_general_schema_stands_alone_and_is_valid() -> None:
    Draft202012Validator.check_schema(GENERAL)
    assert "$ref" not in json.dumps(GENERAL) and GENERAL["type"] == "object"
    assert {"view", "sections", "bytes"} == set(GENERAL["required"])
    for name in vs.DEFS:
        assert name in DEFS  # the app's definitions carry every definition of the view model


SECTIONS: list[tuple[Doc, bool]] = [
    # fields
    ({"kind": "fields"}, True),
    (
        {
            "kind": "fields",
            "id": "f",
            "title": "T",
            "items": [
                {"name": "name", "role": "label", "value": "A", "chars": 300},
                {"name": "tags", "value": ["a", "b"], "more": 2},
                {"name": "n", "role": "state", "value": None},
            ],
            "other": ["x"],
            "other_more": 3,
        },
        True,
    ),
    ({"kind": "fields", "items": [{"name": "n"}]}, False),  # an item has a value
    ({"kind": "fields", "items": [{"name": "n", "value": 1, "role": "hidden_label"}]}, False),
    ({"kind": "fields", "other": [1]}, False),
    ({"kind": "fields", "surprise": 1}, False),
    # relations
    ({"kind": "relations"}, True),
    (
        {
            "kind": "relations",
            "groups": [
                {
                    "relation": "org:in_unit",
                    "direction": "in",
                    "count": 5,
                    "states": {"active": 4, "planned": 1},
                    "items": [
                        {
                            "ref": "POS-1",
                            "label": "L",
                            "type": "org:Position",
                            "state": "active",
                            "record_status": "retracted",
                            "field": {"name": "title", "value": "T"},
                            "qualifiers": {"since": "2026-01-01", "n": 1},
                        }
                    ],
                    "more": 4,
                    "sampled": 50,
                    "approx": True,
                    "missing": 1,
                }
            ],
            "more_groups": ["a", "b"],
        },
        True,
    ),
    ({"kind": "relations", "groups": [{"relation": "r", "direction": "up", "count": 1}]}, False),
    ({"kind": "relations", "groups": [{"relation": "r", "direction": "in"}]}, False),
    # files and its variant
    ({"kind": "files", "items": [{"ref": "F-1", "title": "T", "page": 1, "last_page": 4}]}, True),
    (
        {"kind": "files", "items": [{"ref": "F-1", "locator": "front"}], "more": 2, "sampled": 5},
        True,
    ),
    ({"kind": "files", "items": [{"title": "no ref"}]}, False),
    (
        {
            "kind": "files",
            "variant": "cited_by",
            "count": 40,
            "page_min": 1,
            "page_max": 7,
            "pages_cited": 7,
            "items": [{"ref": "N-1", "label": "L", "page": 3, "last_page": 4}],
            "more": 39,
            "sampled": 50,
            "approx": True,
        },
        True,
    ),
    ({"kind": "files", "variant": "cited_by"}, False),  # count
    ({"kind": "files", "variant": "other", "count": 1}, False),
    # affordances: the keys of the summary
    ({"kind": "affordances"}, True),
    (
        {
            "kind": "affordances",
            "transitions": [
                {
                    "attr": "status",
                    "allowed": ["done"],
                    "allowed_more": 1,
                    "blocked": [{"state": "closed", "by": ["g"]}],
                    "blocked_more": 2,
                },
                {"attr": None, "allowed": ["x"]},
            ],
            "transitions_more": 1,
            "actions": {"apply": ["a"], "blocked": 2},
            "actions_more": 3,
            "functions": ["f"],
            "functions_more": 1,
            "views": ["v"],
            "views_more": 1,
            "detail": "graph get X --affordances",
        },
        True,
    ),
    ({"kind": "affordances", "actions": {"apply": "a"}}, False),
    # neighbors
    (
        {
            "kind": "neighbors",
            "id": "p",
            "relation": "org:in_unit",
            "direction": "in",
            "count": 3,
            "items": [
                {
                    "ref": "POS-1",
                    "label": "L",
                    "type": "org:Position",
                    "record_status": "superseded",
                    "qualifiers": {"since": "2026"},
                    "fields": {"title": "T", "tags": ["a"]},
                    "chars": {"title": 300},
                    "more": {"tags": 2},
                    "relations": [
                        {
                            "relation": "org:holds",
                            "direction": "in",
                            "count": 2,
                            "items": [
                                {
                                    "ref": "E-1",
                                    "state": "active",
                                    "fields": {"work_email": "x"},
                                    "chars": {"work_email": 90},
                                    "more": {},
                                }
                            ],
                            "more": 1,
                            "sampled": 9,
                            "approx": True,
                            "unread": 4,
                        }
                    ],
                }
            ],
            "more": 2,
            "sampled": 9,
            "approx": True,
            "unread": 4,
        },
        True,
    ),
    ({"kind": "neighbors", "relation": "r", "direction": "out", "count": 0}, False),  # id
    ({"kind": "neighbors", "id": "p", "relation": "r", "direction": "out"}, False),  # count
    # list
    ({"kind": "list", "id": "l", "type": "org:Team", "count": 0}, True),
    (
        {
            "kind": "list",
            "id": "l",
            "type": ["org:Team", "org:Department"],
            "count": 40,
            "from": 20,
            "items": [{"ref": "T-1", "type": "org:Team", "fields": {"name": "N"}}],
        },
        True,
    ),
    ({"kind": "list", "id": "l", "type": 1, "count": 0}, False),
    ({"kind": "list", "id": "l", "count": 0}, False),
    # rows, its error form and the stub
    (
        {
            "kind": "rows",
            "id": "r",
            "function": {"name": "f", "hash": "sha256:f"},
            "columns": ["a", "u.b"],
            "items": [{"a": 1, "u.b": [1]}],
            "count": 9,
            "more": 8,
            "cells_cut": 1,
            "from": 3,
        },
        True,
    ),
    ({"kind": "rows", "id": "r", "function": {"name": "f"}, "columns": [], "count": 0}, True),
    (
        {
            "kind": "rows",
            "id": "r",
            "title": "T",
            "function": {"name": "f", "hash": "sha256:f"},
            "error": {"code": "function.stale", "message": "m"},
        },
        True,
    ),
    ({"kind": "rows", "id": "r", "function": {"name": "f"}, "error": {"code": "c"}}, False),
    ({"kind": "rows", "id": "r", "function": {"name": "f"}}, False),  # neither rows nor error
    ({"kind": "rows", "id": "r", "function": {"hash": "h"}, "columns": [], "count": 0}, False),
    ({"kind": "rows", "id": "r", "function": {"name": "f"}, "error": {}, "count": 1}, False),
    ({"kind": "fields", "id": "f", "cut": True}, True),  # every kind can shrink to a stub
    ({"kind": "rows", "cut": True}, True),
    ({"kind": "files", "id": "f", "cut": True, "title": "T"}, False),  # a stub has no title
    ({"kind": "fields", "id": "f", "cut": False}, False),
    # a kind this program does not know: ignored by an open schema, a bare kind in the tests
    ({"kind": "chart"}, True),
    ({"kind": "chart", "data": [1]}, False),
]


@pytest.mark.parametrize(("section", "valid"), SECTIONS, ids=lambda x: str(x)[:50])
def test_each_section_kind_and_variant_has_its_schema(section: Doc, valid: bool) -> None:
    found = problems(GENERAL, head([section]))
    assert (found == []) is valid, found


def test_an_open_schema_ignores_what_it_does_not_know() -> None:
    """The published schema is open: a key or a kind the code adds later does not break a harness
    that validates (the tests close it, see above)."""
    document = head([{"kind": "fields", "new_key": 1}, {"kind": "chart", "data": [1]}], extra=1)
    assert Draft202012Validator(GENERAL).is_valid(document)
    assert not Draft202012Validator(GENERAL).is_valid(head([{"kind": "fields", "items": 3}]))
    assert not Draft202012Validator(GENERAL).is_valid(head([{"title": "no kind"}]))


def test_the_top_level_keys_of_the_model() -> None:
    page = head(
        [],
        entity={"id": "i", "type": "t", "rev": 1, "record_status": "active"},
        next="v1",
        more=True,
    )
    assert problems(GENERAL, page | {"cut": True, "sections_cut": 2, "oversize": True}) == []
    assert problems(GENERAL, head([], next=3)) != []
    assert problems(GENERAL, {"sections": [], "bytes": 1}) != []  # the view is required
    entity = {"id": "i", "type": "t", "rev": 1, "record_status": "active"}
    full = {**entity, "alias": "A", "label": "L", "superseded_by": ["x"], "retracted_by": ["y"]}
    assert problems(GENERAL, head([], entity=full)) == []
    assert problems(GENERAL, head([], entity={"id": "i"})) != []


# ---- real answers match the general schema -------------------------------------------------


def limits(untrimmed: int) -> list[int]:
    found = [untrimmed]
    limit = min(untrimmed, 12_000)
    while limit > 150:
        found.append(limit)
        limit = int(limit * 0.9)
    return found


def guarded(scenario: Scenario) -> list[Doc]:
    spec = generated_spec(scenario.model, scenario.class_curie)
    spec = replace(spec, byte_limit=10**9)
    full = render(
        spec,
        scenario.entity,
        scenario.snapshot,
        scenario.model,
        affordances=scenario.affordances or None,
    )
    return [dict(card._guard(full, limit).to_json()) for limit in limits(full.size())]


def test_every_card_the_byte_guard_makes_matches_the_general_schema() -> None:
    seen: set[str] = set()
    count = 0
    for scenario in scenarios():
        for document in guarded(scenario):
            assert problems(GENERAL, document) == [], scenario.name
            seen |= {
                s["kind"] + (":" + s["variant"] if "variant" in s else "")
                for s in document["sections"]
            }
            count += 1
    assert {"fields", "relations", "files", "files:cited_by", "affordances"} <= seen
    assert count > 100
