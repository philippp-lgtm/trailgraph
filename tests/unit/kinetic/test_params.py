"""The parameter module shared by functions and actions: kinds, optional parameters, problems."""

from decimal import Decimal
from typing import Any

import pytest
from support.actions import VALID as ACTION_VALID
from support.fixtures import compiled_model
from support.functions import VALID as FUNCTION_VALID

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic import bind, params
from graph_cli.kinetic.action_format import load_action
from graph_cli.kinetic.format import load_function
from graph_cli.kinetic.params import ParamDef, Problem

MODEL = compiled_model("inventory")


def p(name: str, kind: str, **extra: Any) -> ParamDef:
    return ParamDef(name, kind, "A parameter.", **extra)


def fail(call: Any, code: str) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_the_function_entry_points_are_the_function_kind() -> None:
    params_ = [p("n", "integer")]
    error = fail(lambda: bind.bind_cli("fn", params_, [], MODEL), "function.parameter_missing")
    assert dict(error.details) == {"function": "fn", "parameter": "n", "type": "integer"}
    error = fail(
        lambda: bind.bind_json("fn", params_, {"x": 1}, MODEL), "function.parameter_unknown"
    )
    assert error.details["function"] == "fn"
    assert bind.placeholder is params.placeholder


def test_the_action_kind_has_its_own_codes_and_detail_key() -> None:
    params_ = [p("n", "integer")]
    error = fail(
        lambda: params.bind_cli("action", "act", params_, [], MODEL), "action.parameter_missing"
    )
    assert dict(error.details) == {"action": "act", "parameter": "n", "type": "integer"}
    error = fail(
        lambda: params.bind_json("action", "act", params_, {"n": 1, "x": 1}, MODEL),
        "action.parameter_unknown",
    )
    assert error.details["action"] == "act" and "function" not in error.details
    error = fail(
        lambda: params.bind_json("action", "act", params_, {"n": "one"}, MODEL),
        "action.parameter_invalid",
    )
    assert error.details["action"] == "act"
    assert "'n' of act" in error.message


def test_an_optional_parameter_that_is_not_given_is_absent() -> None:
    params_ = [p("a", "string", optional=True), p("b", "decimal", default="1.5")]
    assert params.bind_cli("action", "act", params_, [], MODEL) == {"b": Decimal("1.5")}
    assert params.bind_json("action", "act", params_, {"a": "x"}, MODEL) == {
        "a": "x",
        "b": Decimal("1.5"),
    }
    assert params.bind_cli("action", "act", params_, [("a", "y")], MODEL)["a"] == "y"


def test_optional_changes_required_and_the_signature_but_not_the_function_json() -> None:
    plain = p("a", "string")
    optional = p("a", "string", optional=True)
    assert (plain.required, optional.required) == (True, False)
    assert optional.signature == "a?: string"
    assert "optional" not in plain.to_json()
    assert optional.to_json()["optional"] is True
    assert ParamDef.from_json(optional.to_json(), "") == optional
    assert ParamDef.from_json(plain.to_json(), "") == plain


def test_problem_json_names_the_subject_by_its_kind() -> None:
    function = Problem("f", "function.stale", "m").to_json()
    action = Problem("a", "action.stale", "m", kind="action").to_json()
    assert list(function) == ["function", "code", "message", "path", "line", "details"]
    assert action["action"] == "a" and "function" not in action
    assert Problem("f", "function.stale", "m").function == "f"


def test_required_false_is_an_action_key_only() -> None:
    text = (FUNCTION_VALID / "members_of_unit.yaml").read_text()
    changed = text.replace(
        "summary: The unit to list.", "summary: The unit to list.\n    required: false"
    )
    assert changed != text
    defn, problems = load_function("members_of_unit.yaml", changed)
    assert defn is None
    assert [(q.code, q.path) for q in problems] == [
        ("function.unknown_key", "params.unit.required")
    ]


def test_required_false_with_a_default_is_refused_in_an_action() -> None:
    text = (ACTION_VALID / "hand_over_position.yaml").read_text()
    changed = text.replace("required: false", "required: false\n    default: 2026-01-01")
    defn, problems = load_action("hand_over_position.yaml", changed)
    assert defn is None
    (problem,) = problems
    assert (problem.code, problem.path) == ("action.invalid_value", "params.since.required")
    assert "default" in problem.message


def test_required_false_makes_the_parameter_optional_in_an_action() -> None:
    defn, problems = load_action(
        "hand_over_position.yaml", (ACTION_VALID / "hand_over_position.yaml").read_text()
    )
    assert problems == [] and defn is not None
    assert [(q.name, q.optional) for q in defn.params] == [
        ("position", False),
        ("person", False),
        ("since", True),
    ]
    assert defn.params[2].default is None and not defn.params[2].required
