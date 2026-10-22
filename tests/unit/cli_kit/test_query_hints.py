"""Hints of the query codes (B9.8)."""

from graph_cli.cli_kit.hints import hint_for


def test_unsupported_write_points_to_apply() -> None:
    assert hint_for("query.unsupported", {"kind": "write"}, program="graph") == "graph apply -"
    assert hint_for("query.unsupported", {"kind": "clause"}, program="graph") is None


def test_missing_parameter_names_the_flag() -> None:
    hint = hint_for("query.parameter_missing", {"name": "repo"}, program="graph")
    assert hint == "graph query --param repo=<value>"
    assert hint_for("query.parameter_missing", {}, program="graph") is None


def test_ambiguous_term_lists_the_types() -> None:
    assert hint_for("query.ambiguous_term", {}, program="graph") == "graph describe types"
