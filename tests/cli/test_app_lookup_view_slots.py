"""S: ``graph lookup --for`` for the ``ref`` inputs of views (tool manifest plan TM5): the
kind-qualified forms ``view:<name>.<param>`` and ``view:<name>._entity``, the qualified forms of
functions and actions, a view that is named like a function, and what a caller who does not see or
may not use a view is told (the same as for a function).

The ``company`` graph holds a department and a team of the same name, so the classes a slot takes
show in what the lookup keeps: ``members_report`` takes a department, ``unit_overview`` any unit and
``legacy_unit_overview`` a team.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome
from support.app import app
from support.lookup_graph import ENV, EXPLORE, FACTORY, NAME, build
from support.read_limits import install, shown, the_same_but_for, without_suggestions


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    return build(run_cli, isolated_env, tmp_path)


def lookup(run_cli: CliRunner, use: str, env: dict[str, str] | None = None) -> Outcome:
    return app(run_cli, "lookup", NAME, "--for", use, env=env or ENV)


def kept(run_cli: CliRunner, use: str, env: dict[str, str] | None = None) -> list[str]:
    """The types of the candidates ``--for use`` keeps, sorted."""
    outcome = lookup(run_cli, use, env)
    assert outcome.code == 0, (use, outcome.envelope)
    assert outcome.data["fit"]["for"] == use
    return sorted(i["type"] for i in outcome.data["items"])


def test_a_view_parameter_fits_the_class_of_its_ref(run_cli: CliRunner, root: Path) -> None:
    outcome = lookup(run_cli, "view:members_report.unit")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["fit"] == {
        "for": "view:members_report.unit",
        "for_source": None,
        "types": ["org:Department"],
    }
    # the team of the same name is counted, not listed
    assert [i["type"] for i in outcome.data["items"]] == ["org:Department"]
    assert outcome.data["texts"] == [{"text": NAME, "total": 1, "exact": 1, "excluded": 1}]
    # the function it runs takes any unit
    assert kept(run_cli, "members_of_unit.unit") == ["org:Department", "org:Team"]


def test_the_entity_of_an_entity_view_fits_the_class_it_takes(
    run_cli: CliRunner, root: Path
) -> None:
    assert kept(run_cli, "view:unit_overview._entity") == ["org:Department", "org:Team"]
    assert kept(run_cli, "view:legacy_unit_overview._entity") == ["org:Team"]  # a deprecated view
    none = lookup(run_cli, "view:position_overview._entity")  # takes a position: no unit fits
    assert none.code == 0 and none.data["items"] == []
    assert none.data["texts"] == [{"text": NAME, "total": 0, "exact": 0, "excluded": 2}]


def test_a_view_named_like_a_function_is_told_by_its_kind(run_cli: CliRunner, root: Path) -> None:
    # the entity of the view is a department; the function of the name takes an employee
    assert kept(run_cli, "view:employer_of._entity") == ["org:Department"]
    plain = lookup(run_cli, "employer_of._entity")
    assert (plain.code, plain.error["code"]) == (2, "function.parameter_unknown")
    assert plain.error["details"]["expected"] == ["person"]
    function = kept(run_cli, "employer_of.person")
    assert function == []  # an employee of that name does not exist, a unit is no fit
    # and the other way round: the view of the name has no parameter of the function
    wrong = lookup(run_cli, "view:employer_of.person")
    assert (wrong.code, wrong.error["code"]) == (2, "view.parameter_unknown")
    assert wrong.error["details"]["expected"] == ["_entity"]


def test_a_view_needs_its_kind_unless_a_function_has_the_name(
    run_cli: CliRunner, root: Path
) -> None:
    # members_report is a view only: without its kind the name is none
    bare = lookup(run_cli, "members_report.unit")
    assert (bare.code, bare.error["code"]) == (1, "function.not_found")
    assert "members_report" not in bare.error["details"]["did_you_mean"]
    # the plain form of a function still works as it did
    assert kept(run_cli, "members_of_unit.unit") == ["org:Department", "org:Team"]


def test_function_and_action_may_be_written_out_too(run_cli: CliRunner, root: Path) -> None:
    assert kept(run_cli, "function:members_of_unit.unit") == ["org:Department", "org:Team"]
    assert kept(run_cli, "action:assign_position.position") == []  # NAME is no position
    wrong_kind = {
        "action:members_of_unit.unit": "action.not_found",
        "function:assign_position.person": "function.not_found",
        "view:assign_position.person": "view.not_found",
        "view:no_such._entity": "view.not_found",
        "action:no_such.person": "action.not_found",
        "function:no_such.unit": "function.not_found",
    }
    for form, code in wrong_kind.items():
        refused = lookup(run_cli, form)
        assert (refused.code, refused.error["code"]) == (1, code), form
    close = lookup(run_cli, "view:unit_overviw._entity").error["details"]["did_you_mean"]
    assert close[0] == "unit_overview"


def test_the_forms_are_checked(run_cli: CliRunner, root: Path) -> None:
    unknown_kind = lookup(run_cli, "tool:members_of_unit.unit")
    assert (unknown_kind.code, unknown_kind.error["code"]) == (2, "usage.invalid_argument")
    assert unknown_kind.error["details"]["argument"] == "--for"
    assert unknown_kind.error["details"]["expected"] == ["function", "action", "view"]
    assert lookup(run_cli, "view:unit_overview.").error["code"] == "view.parameter_unknown"
    assert lookup(run_cli, "view:.x").error["code"] == "view.not_found"
    # a set view has no entity; a function and an action have none either; controls are no inputs
    no_entity = lookup(run_cli, "view:members_report._entity")
    assert no_entity.error["code"] == "view.parameter_unknown"
    assert no_entity.error["details"] == {
        "view": "members_report",
        "parameter": "_entity",
        "expected": ["unit"],
        "did_you_mean": [],
    }
    for form in ("function:members_of_unit._entity", "function:members_of_unit._limit"):
        assert lookup(run_cli, form).error["code"] == "function.parameter_unknown", form
    assert lookup(run_cli, "action:assign_position._reason").error["code"] == (
        "action.parameter_unknown"
    )
    # a parameter that is no reference
    for form in ("view:unit_overview.status", "action:assign_position.since"):
        refused = lookup(run_cli, form)
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), form
        assert refused.error["details"]["argument"] == "--for"
    # a relation, as before, and a relation is not mistaken for a kind
    assert lookup(run_cli, "org:part_of").code == 0


NO_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""
READS_NO_TEAMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [{reads}]
no_project: {{"*": write}}
""".format(
    reads=", ".join(
        sorted(
            name
            for name, found in FACTORY.model.classes.items()
            if not found.abstract and name != "org:Team"
        )
    )
)


def test_a_view_the_caller_may_not_use_is_not_found_like_a_function(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    assert kept(run_cli, "view:members_report.unit", EXPLORE) == ["org:Department"]
    install(run_cli, tmp_path / "policies", NO_FUNCTIONS)
    # members_report runs a function: it is listed as not allowed, the others run none
    listed = app(run_cli, "describe", "views", "--include-not-allowed", env=EXPLORE)
    refused = {v["name"] for v in listed.data["views"] if v.get("allowed") is False}
    assert "members_report" in refused and "employer_of" not in refused
    view = lookup(run_cli, "view:members_report.unit", EXPLORE)
    function = lookup(run_cli, "members_of_unit.unit", EXPLORE)
    assert (view.code, view.error["code"]) == (1, "view.not_found")
    assert (function.code, function.error["code"]) == (1, "function.not_found")
    assert "members_report" not in view.error["details"]["did_you_mean"]
    # a view the caller may use is unaffected, and so is a caller that may use all of them
    assert kept(run_cli, "view:employer_of._entity", EXPLORE) == ["org:Department"]
    assert kept(run_cli, "view:members_report.unit") == ["org:Department"]


def test_a_hidden_view_is_not_found_and_says_no_more_than_for_a_name_that_is_none(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    install(run_cli, tmp_path / "policies", READS_NO_TEAMS)
    hidden = lookup(run_cli, "view:legacy_unit_overview._entity")  # takes a class it cannot see
    missing = lookup(run_cli, "view:no_such_view._entity")
    assert (hidden.code, hidden.error["code"]) == (missing.code, missing.error["code"])
    assert hidden.error["code"] == "view.not_found"
    the_same_but_for(
        without_suggestions(shown(hidden)),
        without_suggestions(shown(missing)),
        ("legacy_unit_overview", "no_such_view"),
    )
    for outcome in (hidden, missing):
        assert "Team" not in str(outcome.error)
        assert "legacy_unit_overview" not in outcome.error["details"]["did_you_mean"]
    function = lookup(run_cli, "members_of_unit.unit")  # a unit is visible, a team is not
    assert function.code == 0 and {i["type"] for i in function.data["items"]} == {"org:Department"}
    # what it may see works
    assert kept(run_cli, "view:members_report.unit") == ["org:Department"]
