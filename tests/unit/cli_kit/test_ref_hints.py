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
        ({**REF, "for_source": "ex:rel"}, "graph lookup 'Some Name' --json --for-source ex:rel"),
        ({**REF, "class": "ex:Place"}, "graph lookup 'Some Name' --json --type ex:Place"),
        (
            {**REF, "for": "fn.param", "class": "ex:Place"},
            "graph lookup 'Some Name' --json --for fn.param",  # the slot wins over the class
        ),
        ({**REF, "actual": "plain"}, "graph lookup plain --json"),
        ({**REF, "actual": "it's"}, "graph lookup 'it'\"'\"'s' --json"),
        ({**REF, "actual": "-x"}, "graph lookup --json -- -x"),  # a text that looks like an option
        (
            {**REF, "actual": "--json", "for": "ex:rel"},
            "graph lookup --json --for ex:rel -- --json",
        ),
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
        {"expected": "entity_ref", "actual": "n" * 201},  # too long to echo into a command
        {},
    ],
)
def test_other_usage_errors_keep_the_help(details: dict[str, object]) -> None:
    assert hint_for("usage.invalid_argument", details, program="graph") == "graph --help"  # type: ignore[arg-type]


def test_a_text_of_the_longest_size_still_gets_its_lookup() -> None:
    text = "n" * 200
    assert hint_for("usage.invalid_argument", {**REF, "actual": text}, program="graph") == (
        f"graph lookup {text} --json"
    )


def test_graph_admin_has_no_lookup_to_point_at() -> None:
    assert hint_for("usage.invalid_argument", REF, program="graph-admin") == "graph-admin --help"
    other = {"argument": "--limit", "actual": "x"}
    assert hint_for("usage.invalid_argument", other, program="graph-admin") == "graph-admin --help"
    assert hint_for("entity.not_found", {"ref": "PER-0099"}, program="graph-admin") is None


def test_an_entity_that_is_not_there_has_no_lookup_hint() -> None:
    """The text was a reference (an id, an alias, hex digits) that names nobody: a lookup
    searches names, so it would not find it."""
    for ref in ("PER-0099", "0192f3c0", FULL_ID):
        assert hint_for("entity.not_found", {"ref": ref}, program="graph") is None
