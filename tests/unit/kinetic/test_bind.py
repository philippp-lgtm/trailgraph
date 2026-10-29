"""Binding CLI strings and JSON to the declared parameter types."""

from decimal import Decimal
from typing import Any

import pytest
from support.fixtures import compiled_model

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.bind import bind_cli, bind_json, placeholder
from graph_cli.kinetic.format import PARAM_TYPES, ParamDef

MODEL = compiled_model("inventory")
ENUM = next(iter(MODEL.enums))
ENUM_VALUES = MODEL.enums[ENUM].value_names()
ALIAS = "PER-0001"
ID = "0194f0f0-1234-7abc-8def-0123456789ab"


def p(name: str, kind: str, **extra: Any) -> ParamDef:
    return ParamDef(name, kind, "A parameter.", **extra)


def fail(call: Any, code: str) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.details["function"] == "fn"
    return caught.value


def test_cli_text_is_read_by_the_declared_type() -> None:
    params = [
        p("s", "string"),
        p("i", "integer"),
        p("d", "decimal"),
        p("b", "boolean"),
        p("day", "date"),
        p("at", "datetime"),
        p("e", "enum", enum=ENUM),
        p("r", "ref", of_class="inv:Item"),
    ]
    pairs = [
        ("s", "42"),
        ("i", "-7"),
        ("d", "1.50"),
        ("b", "true"),
        ("day", "2026-02-03"),
        ("at", "2026-02-03T04:05:06+01:00"),
        ("e", ENUM_VALUES[0]),
        ("r", ALIAS),
    ]
    assert bind_cli("fn", params, pairs, MODEL) == {
        "s": "42",
        "i": -7,
        "d": Decimal("1.50"),
        "b": True,
        "day": "2026-02-03",
        "at": "2026-02-03T03:05:06.000Z",
        "e": ENUM_VALUES[0],
        "r": ALIAS,
    }


def test_a_string_that_looks_like_json_stays_a_string() -> None:
    assert bind_cli("fn", [p("s", "string")], [("s", "true")], MODEL) == {"s": "true"}
    assert bind_cli("fn", [p("s", "string")], [("s", '"x"')], MODEL) == {"s": '"x"'}


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("integer", "1.5"),
        ("integer", "abc"),
        ("decimal", "1,5"),
        ("boolean", "yes"),
        ("date", "03.02.2026"),
        ("datetime", "2026-02-03"),
        ("enum", "no_such_value"),
        ("ref", "Some Name"),
        ("ref", "1234"),
    ],
)
def test_cli_text_that_does_not_fit_is_invalid(kind: str, text: str) -> None:
    param = p("x", kind, enum=ENUM if kind == "enum" else None)
    error = fail(
        lambda: bind_cli("fn", [param], [("x", text)], MODEL), "function.parameter_invalid"
    )
    assert error.details["parameter"] == "x"
    assert "fn" in error.message


def test_a_ref_takes_every_reference_form_and_no_name() -> None:
    for text in (ALIAS, ID, "0123456789ab", ID.upper()):
        bound = bind_cli("fn", [p("r", "ref")], [("r", text)], MODEL)
        assert isinstance(bound["r"], str)
    error = fail(
        lambda: bind_cli("fn", [p("r", "ref")], [("r", "Sales")], MODEL),
        "function.parameter_invalid",
    )
    assert "looked up" in error.message


def test_json_values_are_checked_by_type() -> None:
    params = [p("i", "integer"), p("d", "decimal"), p("b", "boolean"), p("s", "string")]
    given = {"i": 3, "d": "2.5", "b": False, "s": "x"}
    assert bind_json("fn", params, given, MODEL) == {
        "i": 3,
        "d": Decimal("2.5"),
        "b": False,
        "s": "x",
    }
    assert bind_json("fn", [p("d", "decimal")], {"d": 4}, MODEL) == {"d": Decimal(4)}


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("integer", "3"),
        ("integer", True),
        ("integer", 2**60),
        ("integer", None),
        ("boolean", "true"),
        ("boolean", 1),
        ("string", 5),
        ("string", None),
        ("decimal", True),
        ("date", 20260203),
        ("date", "2026-13-01"),
        ("string", ["a"]),
    ],
)
def test_json_values_of_another_type_are_invalid(kind: str, value: Any) -> None:
    fail(lambda: bind_json("fn", [p("x", kind)], {"x": value}, MODEL), "function.parameter_invalid")


def test_many_takes_repeated_pairs_or_a_list() -> None:
    param = p("tags", "integer", many=True)
    assert bind_cli("fn", [param], [("tags", "1"), ("tags", "2")], MODEL) == {"tags": [1, 2]}
    assert bind_cli("fn", [param], [("tags", "1")], MODEL) == {"tags": [1]}
    assert bind_json("fn", [param], {"tags": [1, 2]}, MODEL) == {"tags": [1, 2]}
    fail(lambda: bind_json("fn", [param], {"tags": 1}, MODEL), "function.parameter_invalid")
    fail(lambda: bind_json("fn", [param], {"tags": [1, "2"]}, MODEL), "function.parameter_invalid")
    fail(
        lambda: bind_cli("fn", [p("x", "integer")], [("x", "1"), ("x", "2")], MODEL),
        "function.parameter_invalid",
    )


def test_defaults_fill_what_is_left_out() -> None:
    params = [
        p("since", "date", default="2000-01-01"),
        p("tags", "string", many=True, default=["a"]),
    ]
    assert bind_cli("fn", params, [], MODEL) == {"since": "2000-01-01", "tags": ["a"]}
    assert bind_json("fn", params, {"since": "2026-02-03"}, MODEL) == {
        "since": "2026-02-03",
        "tags": ["a"],
    }


def test_a_missing_required_parameter_names_its_type() -> None:
    error = fail(
        lambda: bind_cli("fn", [p("unit", "ref", of_class="inv:Item")], [], MODEL),
        "function.parameter_missing",
    )
    assert error.details["parameter"] == "unit"
    assert error.details["type"] == "ref inv:Item"
    fail(lambda: bind_json("fn", [p("n", "integer")], {}, MODEL), "function.parameter_missing")


def test_an_unknown_parameter_suggests_the_declared_ones() -> None:
    error = fail(
        lambda: bind_cli("fn", [p("quantity", "integer")], [("quantiti", "1")], MODEL),
        "function.parameter_unknown",
    )
    assert error.details["did_you_mean"] == ["quantity"]
    assert error.details["expected"] == ["quantity"]
    fail(lambda: bind_json("fn", [], {"x": 1}, MODEL), "function.parameter_unknown")


def test_a_placeholder_exists_for_every_type() -> None:
    for kind in PARAM_TYPES:
        param = p("x", kind, enum=ENUM if kind == "enum" else None)
        value = placeholder(param, MODEL)
        assert value is not None
        assert placeholder(p("x", kind, many=True, enum=param.enum), MODEL) == [value]
    assert placeholder(p("x", "enum", enum=ENUM), MODEL) == ENUM_VALUES[0]
    assert placeholder(p("x", "enum", enum="inv:Missing"), MODEL) == "x"
