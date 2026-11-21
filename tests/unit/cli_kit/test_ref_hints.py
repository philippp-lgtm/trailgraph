"""Hints for a reference argument that is not a reference, and for an entity that is not there:
the lookup of the text the caller gave (the entity-reference contract, no name resolution)."""

import pytest

from graph_cli.cli_kit.hints import hint_for

REF = {"actual": "Some Name", "expected": "entity_ref"}
FULL_ID = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


@pytest.mark.parametrize(
    ("details", "hint"),
    [
        (REF, "graph lookup 'Some Name' --json"),
        ({**REF, "for": "fn.param"}, "graph lookup 'Some Name' --json --for fn.param"),
        ({**REF, "for": "ex:rel"}, "graph lookup 'Some Name' --json --for ex:rel"),
        ({**REF, "class": "ex:Place"}, "graph lookup 'Some Name' --json --type ex:Place"),
        (
            {**REF, "for": "fn.param", "class": "ex:Place"},
            "graph lookup 'Some Name' --json --for fn.param",  # the slot wins over the class
        ),
        ({**REF, "actual": "plain"}, "graph lookup plain --json"),
        ({**REF, "actual": "it's"}, "graph lookup 'it'\"'\"'s' --json"),
    ],
)
def test_a_name_where_a_reference_goes_hints_at_lookup(details: dict[str, str], hint: str) -> None:
    assert hint_for("usage.invalid_argument", details, program="graph") == hint


@pytest.mark.parametrize(
    "details",
    [
        {"argument": "--limit", "actual": "x"},
        {"argument": "--edge-id"},
        {"expected": "a number", "actual": "x"},
        {"expected": "entity_ref", "actual": 3},
        {},
    ],
)
def test_other_usage_errors_keep_the_help(details: dict[str, object]) -> None:
    assert hint_for("usage.invalid_argument", details, program="graph") == "graph --help"  # type: ignore[arg-type]


def test_an_entity_that_is_not_there_hints_at_lookup_unless_the_ref_is_a_full_id() -> None:
    assert hint_for("entity.not_found", {"ref": "PER-0099"}, program="graph") == (
        "graph lookup PER-0099 --json"
    )
    assert hint_for("entity.not_found", {"ref": "0192f3c0"}, program="graph") == (
        "graph lookup 0192f3c0 --json"
    )
    assert hint_for("entity.not_found", {"ref": FULL_ID}, program="graph") is None
    assert hint_for("entity.not_found", {"ref": FULL_ID.upper()}, program="graph") is None
    assert hint_for("entity.not_found", {"id": FULL_ID}, program="graph") is None
    assert hint_for("entity.not_found", {"ref": 3}, program="graph") is None
