"""Decimals in answers are shown in the plain notation that decimal attributes and parameters use
(tool manifest plan TM6): ``1E-7`` is ``0.0000001`` and ``1E+3`` is ``1000``, so a cell can be
passed back as a ``decimal`` parameter and matches the schema of a decimal column."""

import re
from decimal import Decimal

import pytest
from support.fixtures import compiled_model

from graph_cli.kinetic.params import ParamDef, bind_json
from graph_cli.ontology_layer.values import DECIMAL_RE
from graph_cli.query.cypher.values import MAX_PLAIN_EXPONENT, decimal_text, plain_json


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1E-7", "0.0000001"),
        ("1E+3", "1000"),
        ("-1E+5", "-100000"),
        ("1.50", "1.50"),
        ("0.1", "0.1"),
        ("-0", "-0"),
        ("-0.0", "-0.0"),
        ("0E-10", "0.0000000000"),
        ("123456789012345678901234567890", "123456789012345678901234567890"),
        ("12345E-3", "12.345"),
    ],
)
def test_a_decimal_is_shown_in_plain_notation(value: str, text: str) -> None:
    assert decimal_text(Decimal(value)) == text
    assert plain_json(Decimal(value)) == text
    assert DECIMAL_RE.match(text)


def test_a_decimal_inside_a_list_or_a_map_is_shown_the_same_way() -> None:
    assert plain_json([Decimal("1E-7"), {"a": Decimal("1E+3")}]) == ["0.0000001", {"a": "1000"}]


def test_an_exponent_beyond_the_bound_keeps_the_exponent_notation() -> None:
    near = Decimal(f"1E+{MAX_PLAIN_EXPONENT}")
    assert decimal_text(near) == "1" + "0" * MAX_PLAIN_EXPONENT
    assert decimal_text(Decimal(f"1E+{MAX_PLAIN_EXPONENT + 1}")) == f"1E+{MAX_PLAIN_EXPONENT + 1}"
    # a valid literal must not become a text of a billion digits
    assert decimal_text(Decimal("1E+999999999")) == "1E+999999999"
    assert decimal_text(Decimal("1E-999999999")) == "1E-999999999"


@pytest.mark.parametrize("value", ["1E-7", "1E+3", "-0", "0.1", "12345E-3", "-1E+5"])
def test_a_shown_decimal_binds_as_a_decimal_parameter_with_the_same_value(value: str) -> None:
    shown = plain_json(Decimal(value))
    assert isinstance(shown, str)
    param = ParamDef("d", "decimal", "A decimal.")
    bound = bind_json("function", "f", [param], {"d": shown}, compiled_model("inventory"))
    assert Decimal(str(bound["d"])) == Decimal(value)
    assert re.fullmatch(DECIMAL_RE.pattern, shown)
