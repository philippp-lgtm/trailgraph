"""Type descriptors of parameters for tool schemas (tool manifest plan TM5): one descriptor per
parameter type, and the text grammars checked against the real parsers, so that a value a schema
accepts always binds (``bind_json``) and every real value matches."""

import re
from datetime import date

import pytest

from graph_cli.kernel.canonical import MAX_SAFE_INT
from graph_cli.kernel.clock import DATE_PATTERN, TIMESTAMP_PATTERN, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import ENTITY_REF_PATTERN, parse_entity_ref
from graph_cli.kinetic.params import PARAM_TYPES, ParamDef
from graph_cli.kinetic.tool_types import ParamType, param_type, param_types, ref_type
from graph_cli.ontology_layer.values import DECIMAL_RE


def declared(type_: str, **extra: object) -> ParamDef:
    fields: dict[str, object] = {"name": "p", "type": type_, "summary": "A parameter."} | extra
    return ParamDef(**fields)  # type: ignore[arg-type]


def test_every_parameter_type_has_a_descriptor() -> None:
    extras = {"ref": {"of_class": "ex:Thing"}, "enum": {"enum": "ex:Level"}}
    found = {t: param_type(declared(t, **extras.get(t, {}))) for t in PARAM_TYPES}
    assert {t: (d.json_type, d.pattern is not None) for t, d in found.items()} == {
        "string": ("string", False),
        "integer": ("integer", False),
        "decimal": ("string", True),
        "boolean": ("boolean", False),
        "date": ("string", True),
        "datetime": ("string", True),
        "enum": ("string", False),
        "ref": ("string", True),
    }
    assert found["ref"].of_class == "ex:Thing" and found["enum"].enum == "ex:Level"


def test_an_integer_has_the_safe_range_and_nothing_else_has_one() -> None:
    assert (param_type(declared("integer")).minimum, param_type(declared("integer")).maximum) == (
        -MAX_SAFE_INT,
        MAX_SAFE_INT,
    )
    for type_ in set(PARAM_TYPES) - {"integer"}:
        extras = (
            {"of_class": "ex:T"} if type_ == "ref" else {"enum": "ex:E"} if type_ == "enum" else {}
        )
        descriptor = param_type(declared(type_, **extras))
        assert descriptor.minimum is None and descriptor.maximum is None


def test_required_default_many_and_optional_follow_the_declaration() -> None:
    assert param_type(declared("string")).required
    with_default = param_type(declared("string", default="x"))
    assert not with_default.required and with_default.default == "x"
    optional = param_type(declared("string", optional=True))
    assert not optional.required and optional.default is None
    many = param_type(declared("string", many=True, default=["a", "b"]))
    assert many.many and many.default == ["a", "b"]


def test_a_decimal_default_is_text_whatever_the_file_wrote() -> None:
    assert param_type(declared("decimal", default=5)).default == "5"
    assert param_type(declared("decimal", default="5.50")).default == "5.50"
    assert param_type(declared("decimal", many=True, default=[1, "2.5"])).default == ["1", "2.5"]
    assert param_type(declared("integer", default=5)).default == 5
    assert param_type(declared("boolean", default=False)).default is False


def test_the_descriptors_come_in_declaration_order() -> None:
    params = (declared("string"), ParamDef("q", "integer", "Another."))
    assert [d.name for d in param_types(params)] == ["p", "q"]


def test_a_reference_that_is_no_parameter() -> None:
    entity = ref_type("_entity", "The entity.", "ex:Thing")
    assert (
        isinstance(entity, ParamType) and entity.required and entity.pattern == ENTITY_REF_PATTERN
    )
    assert entity.of_class == "ex:Thing"


# ---- the grammars against the real parsers ---------------------------------------------------


def parses_as_date(text: str) -> bool:
    try:
        parse_date(text)
    except GraphCliError:
        return False
    return True


def parses_as_timestamp(text: str) -> bool:
    try:
        parse_timestamp(text)
    except GraphCliError:
        return False
    return True


# Years around the rules of the calendar: the first ones, the century rule, today's, the last
SWEPT_YEARS = (0, 1, 2, 3, 4, 100, 399, 400, 401, 1600, 1700, 1900, 2000, 2100, 9996, 9998, 9999)


def test_the_date_pattern_matches_exactly_the_dates_of_the_calendar() -> None:
    pattern = re.compile(DATE_PATTERN)
    for year in (*SWEPT_YEARS, *range(1995, 2030)):
        for month in range(0, 14):
            for day in range(0, 33):
                text = f"{year:04d}-{month:02d}-{day:02d}"
                assert bool(pattern.fullmatch(text)) == parses_as_date(text), text


def test_the_date_pattern_refuses_other_notations() -> None:
    pattern = re.compile(DATE_PATTERN)
    for text in ("2026-1-5", "26-01-05", "2026/01/05", "2026-01-05T00:00:00Z", " 2026-01-05", ""):
        assert not pattern.fullmatch(text), text


def test_the_timestamp_pattern_matches_only_what_parses() -> None:
    pattern = re.compile(TIMESTAMP_PATTERN)
    zones = ("Z", "+00:00", "-00:00", "+02:00", "-05:30", "+23:59", "-23:59")
    stamps = []
    for day in ("2026-01-01", "2024-02-29", "2023-02-29", "0002-01-01", "9998-12-31", "2026-13-01"):
        for clock in ("00:00:00", "23:59:59", "12:30:45.123", "12:30:45.123456789", "24:00:00"):
            stamps += [f"{day}T{clock}{zone}" for zone in zones]
    # the edges of the calendar: an offset moves them out of it
    stamps += ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00Z"]
    stamps += ["2026-01-01T12:00:60Z", "2026-01-01T12:60:00Z", "2026-01-01T12:00:00+24:00"]
    for text in stamps:
        if pattern.fullmatch(text):
            assert parses_as_timestamp(text), text


def test_the_timestamp_pattern_matches_every_notation_the_format_names() -> None:
    pattern = re.compile(TIMESTAMP_PATTERN)
    for text in (
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00.5Z",
        "2026-01-01T00:00:00.123456789+02:00",
        "2026-11-25T23:59:59-05:30",
    ):
        assert pattern.fullmatch(text) and parses_as_timestamp(text), text
    for text in ("2026-01-01T00:00:00", "2026-01-01 00:00:00Z", "2026-01-01", "2026-01-01T00:00Z"):
        assert not pattern.fullmatch(text), text


def test_the_decimal_pattern_is_the_grammar_of_the_binder() -> None:
    pattern = DECIMAL_RE.pattern
    for text in ("0", "-0", "10", "-3.50", "0.001", "123456789012345678901234567890.5"):
        assert re.fullmatch(pattern, text), text
    for text in ("", "01", "1.", ".5", "+1", "1e3", "1,5", " 1", "NaN"):
        assert not re.fullmatch(pattern, text), text


UUID = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"


@pytest.mark.parametrize(
    "text",
    [
        UUID,
        UUID.upper(),
        f"urn:uuid:{UUID}",
        "ITEM-0007",
        "ORG-123456",
        "A-0001",
        "1f0e5d6c7b8a",
        "1F0E5D6C7B8A",
        "0192f0a17c3e7a108b2c1f0e5d6c7b8a",
        "1f0e5d6c",
    ],
)
def test_every_reference_the_pattern_accepts_parses(text: str) -> None:
    assert re.fullmatch(ENTITY_REF_PATTERN, text)
    assert parse_entity_ref(text, allow_abbrev=True) is not None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Org Unit",
        "engineering",
        "ITEM-007",
        "item-0007",
        "ABCDEFGHI-0001",
        "1f0e5d6",  # 7 hex digits
        "1f0e5d6c7b8a1f0e5d6c7b8a1f0e5d6c7",  # 33
        UUID[:-1],
        "0192f0a1-7c3e-7a10-cb2c-1f0e5d6c7b8a",  # a variant the parser refuses
        f"URN:UUID:{UUID}",
        f" {UUID}",
        "@local",
    ],
)
def test_what_the_pattern_refuses_is_a_name_or_an_unreadable_reference(text: str) -> None:
    assert not re.fullmatch(ENTITY_REF_PATTERN, text)


def test_a_reference_the_pattern_accepts_never_fails_the_parser_whatever_its_digits() -> None:
    for tail in ("8", "9", "a", "b", "A", "B"):
        text = f"0192f0a1-7c3e-7a10-{tail}b2c-1f0e5d6c7b8a"
        assert re.fullmatch(ENTITY_REF_PATTERN, text) and parse_entity_ref(text) is not None
    for tail in ("0", "7", "c", "f"):
        text = f"0192f0a1-7c3e-7a10-{tail}b2c-1f0e5d6c7b8a"
        assert not re.fullmatch(ENTITY_REF_PATTERN, text)


def test_the_sweep_covers_the_leap_rules() -> None:
    leap = [
        y for y in SWEPT_YEARS if y and date(y, 3, 1).toordinal() - date(y, 2, 1).toordinal() == 29
    ]
    assert {4, 400, 1600, 2000, 9996} <= set(leap) and not {100, 1700, 1900, 2100} & set(leap)
