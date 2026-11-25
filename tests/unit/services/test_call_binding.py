"""What a typed call binds (tool manifest plan TM5, TM7): every value its input schema accepts, per
parameter type and across the fixture catalogs, and the two rules the schema cannot say: ``null`` is
"not given" and a whole number written as a float is an integer."""

from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support import tools
from support.fixtures import compiled_model

from graph_cli.kernel.canonical import MAX_SAFE_INT
from graph_cli.kernel.changeset import FloatLiteral, load_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.params import PARAM_TYPES, ParamDef, bind_input, bind_json
from graph_cli.kinetic.tool_types import param_type
from graph_cli.services import describe_tools as dt

MODEL = compiled_model("inventory")
ENUM = "inv:Condition"
UUID = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
# values of each parameter type the input schema accepts, at the edges of its grammar
SAMPLES: dict[str, list[Any]] = {
    "string": ["", "x", "ünï cödé", "a" * 5000, "true", "1.5", "null", "line\nbreak", "{}"],
    "integer": [0, 1, -1, 12345, MAX_SAFE_INT, -MAX_SAFE_INT],
    "decimal": ["0", "-0", "1.50", "-3.5", "0.000001", "123456789012345678901234567890.5"],
    "boolean": [True, False],
    "date": ["0001-01-01", "2024-02-29", "2000-02-29", "1900-03-01", "9999-12-31"],
    "datetime": [
        "2026-02-03T04:05:06Z",
        "2026-02-03T04:05:06.5Z",
        "2026-02-03T04:05:06.123456789+01:00",
        "2026-11-25T23:59:59-05:30",
    ],
    "enum": list(MODEL.enums[ENUM].value_names()),
    "ref": [UUID, UUID.upper(), f"urn:uuid:{UUID}", "ITEM-0007", "ORG-123456", "1f0e5d6c7b8a"],
}


def definition(type_: str, **extra: Any) -> ParamDef:
    fields: dict[str, Any] = {"name": "p", "type": type_, "summary": "A parameter."} | extra
    if type_ == "ref":
        fields.setdefault("of_class", "inv:Item")
    if type_ == "enum":
        fields.setdefault("enum", ENUM)
    return ParamDef(**fields)


def accepts(param: ParamDef, value: Any) -> bool:
    schema = dt.param_schema(param_type(param), MODEL)
    return Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).is_valid(value)


def test_every_parameter_type_has_samples() -> None:
    assert set(SAMPLES) == set(PARAM_TYPES)


@pytest.mark.parametrize("type_", PARAM_TYPES)
def test_every_value_the_schema_accepts_binds_as_it_does_for_bind_json(type_: str) -> None:
    for many in (False, True):
        param = definition(type_, many=many)
        values = [SAMPLES[type_]] if many else SAMPLES[type_]
        if many:
            values = [SAMPLES[type_][:1], SAMPLES[type_][:3], SAMPLES[type_]]
        for value in values:
            assert accepts(param, value), (type_, many, value)
            found = bind_input("function", "fn", [param], {"p": value}, MODEL)
            assert found == bind_json("function", "fn", [param], {"p": value}, MODEL)
            assert "p" in found


@pytest.mark.parametrize("type_", PARAM_TYPES)
def test_null_is_not_given_for_every_parameter_that_is_not_required(type_: str) -> None:
    for many in (False, True):
        optional = definition(type_, many=many, optional=True)
        assert accepts(optional, None)  # the schema says so for these
        assert bind_input("function", "fn", [optional], {"p": None}, MODEL) == {}
        defaulted = definition(
            type_, many=many, default=[SAMPLES[type_][0]] if many else SAMPLES[type_][0]
        )
        # a default is "not given" too, though its schema is stricter than the binder
        given = bind_input("function", "fn", [defaulted], {"p": None}, MODEL)
        assert given == bind_input("function", "fn", [defaulted], {}, MODEL) and "p" in given
        required = definition(type_, many=many)
        assert not accepts(required, None)
        with pytest.raises(GraphCliError) as caught:
            bind_input("function", "fn", [required], {"p": None}, MODEL)
        assert caught.value.code == "function.parameter_missing"


def test_a_whole_number_written_as_a_float_binds_where_the_schema_takes_an_integer() -> None:
    param = definition("integer")
    for number in SAMPLES["integer"]:
        for text in (f"{number}.0", f"{number}.00", f"{number}e0"):
            value = load_document(text)
            assert isinstance(value, FloatLiteral)
            assert accepts(param, float(text)), text  # JSON Schema says an integer: a whole float
            assert bind_input("function", "fn", [param], {"p": value}, MODEL) == {"p": number}
    for text in ("1.5", "1e-3", "0.1", "1e999"):
        value = load_document(text)
        assert not accepts(param, float(text)) or text == "1e999"
        with pytest.raises(GraphCliError) as caught:
            bind_input("function", "fn", [param], {"p": value}, MODEL)
        assert caught.value.code == "function.parameter_invalid", text


def test_a_float_is_no_value_of_any_other_type() -> None:
    for type_ in PARAM_TYPES:
        if type_ == "integer":
            continue
        param = definition(type_)
        for value in (load_document("1.5"), load_document("2.0"), load_document("1e2")):
            with pytest.raises(GraphCliError) as caught:
                bind_input("function", "fn", [param], {"p": value}, MODEL)
            assert caught.value.code == "function.parameter_invalid", (type_, value)


def test_a_decimal_is_text_and_an_integer_written_as_a_number_still_binds() -> None:
    param = definition("decimal")
    assert bind_input("function", "fn", [param], {"p": "1.50"}, MODEL) == {"p": Decimal("1.50")}
    assert bind_input("function", "fn", [param], {"p": 7}, MODEL) == {"p": Decimal(7)}
    assert not accepts(param, 7)  # the schema is stricter than the binder


# ---- every tool of the fixture catalogs ----------------------------------------------------------

CATALOGS = (tools.company(), tools.inventory(), tools.staff())


def sample(schema: dict[str, Any]) -> Any:
    """A value the property schema accepts."""
    if "anyOf" in schema:
        return sample(schema["anyOf"][0])
    if schema["type"] == "array":
        return [sample(schema["items"])]
    if "enum" in schema:
        return schema["enum"][-1]
    for type_, value in (("integer", 3), ("boolean", True)):
        if schema["type"] == type_:
            return value
    if schema.get("format") == "date":
        return "2026-11-25"
    if schema.get("format") == "date-time":
        return "2026-11-25T10:00:00.250+02:00"
    if schema.get(dt.EXTENSION, {}).get("class"):
        return "ORG-0042"
    return "12.50" if "pattern" in schema else "text"


def test_every_tool_binds_each_property_given_and_null_for_the_ones_not_required() -> None:
    bound = 0
    for catalog in CATALOGS:
        scene = catalog.scene()
        for kind, entries in (
            (dt.FUNCTION, scene.functions),
            (dt.ACTION, scene.actions),
            (dt.VIEW, scene.views),
        ):
            for entry in entries:
                params = entry.defn.params
                schemas = {p.name: dt.param_schema(param_type(p), scene.model) for p in params}
                given = {name: sample(schema) for name, schema in schemas.items()}
                whole = {"type": "object", "properties": schemas}
                assert Draft202012Validator(whole).is_valid(given), (entry.name, given)
                found = bind_input(kind, entry.name, params, given, scene.model)
                assert found == bind_json(kind, entry.name, params, given, scene.model)
                nulls = {p.name: None for p in params if not p.required}
                rest = {k: v for k, v in given.items() if k not in nulls}
                with_nulls = bind_input(kind, entry.name, params, {**given, **nulls}, scene.model)
                assert with_nulls == bind_json(kind, entry.name, params, rest, scene.model)
                bound += 1
    assert bound >= 20
