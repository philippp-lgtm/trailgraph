"""Base types ``lang_string`` and ``lang_text``: shapes, model rules, checks (Q18 stage 3)."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, revision
from support.writes import codes, judge

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import CompileOutcome, compile_bundle
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.evaluate import evaluate
from graph_cli.ontology_layer.exporters.jsonschema import export_jsonschema
from graph_cli.ontology_layer.values import (
    check_facets,
    coerce_text,
    lang_value_problem,
    with_language,
)

NAS = compiled_model("notes_and_sources", "1.2.0")


def de(text: str) -> dict[str, JsonValue]:
    return {"lang": "de", "value": text}


def en(text: str) -> dict[str, JsonValue]:
    return {"lang": "en", "value": text}


def entry(**attrs: Any) -> dict[str, Any]:
    return {"op": "create", "type": "nas:GlossaryEntry", "local": "g", "attrs": attrs}


# ---- values -------------------------------------------------------------


@pytest.mark.parametrize("base", ["lang_string", "lang_text"])
def test_text_form(base: str) -> None:
    assert coerce_text(base, '"Steuerung und Kontrolle"@de') == de("Steuerung und Kontrolle")
    assert coerce_text(base, "SuK@DE") == de("SuK")
    assert coerce_text(base, "SuK") == "SuK"  # untagged: the graph language decides
    with pytest.raises(ValueError, match="BCP 47"):
        coerce_text(base, '"SuK"@de_DE')


def test_facets_apply_to_the_text() -> None:
    assert check_facets(de("x" * 81), {"max_length": 80}) == "longer than 80 characters"
    assert check_facets(de("a\nb"), {"single_line": True}) == "contains a line break"
    assert check_facets(de("ok"), {"max_length": 80, "single_line": True}) is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (de("SuK"), None),
        ({"lang": "de-CH", "value": ""}, None),
        ("SuK", "needs a language tag"),
        ({"value": "SuK"}, "needs a language tag"),
        ({"lang": "de"}, "expected {"),
        ({"lang": "de", "value": "SuK", "extra": 1}, "expected {"),
        ({"lang": "de", "value": 1}, "value must be a string"),
        ({"lang": "de_CH", "value": "SuK"}, "not a well-formed"),
        ({"lang": 7, "value": "SuK"}, "not a well-formed"),
        ({"lang": "DE", "value": "SuK"}, "canonical case (de)"),
        (["SuK"], "expected {"),
    ],
)
def test_lang_value_problem(value: JsonValue, message: str | None) -> None:
    problem = lang_value_problem(value)
    assert problem is None if message is None else message in str(problem)


def test_with_language() -> None:
    assert with_language("SuK", "de") == de("SuK")
    assert with_language("SuK", None) == "SuK"
    assert with_language({"value": "SuK"}, "de") == de("SuK")
    assert with_language({"lang": "DE-ch", "value": "SuK"}, "en") == {
        "lang": "de-CH",
        "value": "SuK",
    }
    assert with_language(en("x"), "de") == en("x")  # a given tag stays
    assert with_language({"lang": "de_CH", "value": "x"}, "de") == {"lang": "de_CH", "value": "x"}
    assert with_language(7, "de") == 7


# ---- model rules --------------------------------------------------------


def _compile(attributes: str) -> CompileOutcome:
    text = f"""\
format: graph_cli.ir/1
module:
  id: example_module
  prefix: ex
  version: 1.0.0
  summary: Things named in several languages.
classes:
  Thing:
    summary: Something that is tracked.
    attributes:
{attributes}"""
    return compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )


def _model_codes(attributes: str) -> list[str]:
    return sorted(f.code for f in _compile(attributes).findings)


def test_the_fixture_compiles_with_a_tagged_default() -> None:
    usage = NAS.classes["nas:GlossaryEntry"].attributes["usage_hint"]
    assert usage.default == en("in notes")


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        (
            "      name: {summary: Names., type: lang_string, many: true, role: label}\n",
            [],
        ),
        (
            "      name: {summary: Names., type: lang_string, role: alt_label, unique: true}\n",
            [],
        ),
        (
            "      name: {summary: Names., type: string, many: true, role: label}\n",
            ["model.semantic.label_not_single"],
        ),
        (
            "      name: {summary: Names., type: lang_text, role: label}\n",
            ["model.semantic.role_not_applicable"],
        ),
        (
            "      name: {summary: A name., type: lang_string, default: SuK}\n",
            ["model.structural.invalid_default"],
        ),
        (
            "      name: {summary: A name., type: lang_string, default: '\"SuK\"@de_DE'}\n",
            ["model.structural.invalid_default"],
        ),
        (
            "      name: {summary: A name., type: lang_text, default: 'SuK@de'}\n",
            [],
        ),
    ],
)
def test_model_rules(attributes: str, expected: list[str]) -> None:
    assert _model_codes(attributes) == expected


def test_qualifiers_may_be_language_tagged_and_take_the_graph_language() -> None:
    text = """\
format: graph_cli.ir/1
module:
  id: example_module
  prefix: ex
  version: 1.0.0
  summary: Things that cite each other.
classes:
  Thing:
    summary: Something that is tracked.
relations:
  cites:
    summary: One thing cites another.
    domain: Thing
    range: Thing
    qualifiers:
      quote: {summary: The quoted words., type: lang_text}
"""
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )
    assert outcome.findings == () and outcome.model is not None
    ops = [
        {"op": "create", "type": "ex:Thing", "local": "a"},
        {
            "op": "create",
            "type": "ex:Thing",
            "local": "b",
            "edges": [{"rel": "ex:cites", "target": "@a", "qualifiers": {"quote": "Zitat"}}],
        },
    ]
    working, found = judge(outcome.model, *ops, language="de")
    assert codes(found) == []
    (edge,) = [e for r in working.revisions.values() for e in r.edges]
    assert edge.qualifiers == {"quote": de("Zitat")}
    _, refused = judge(outcome.model, *ops)
    assert codes(refused) == ["data.structural.datatype_mismatch"]


# ---- data checks --------------------------------------------------------


def test_valid_tagged_writes_have_no_findings() -> None:
    _, found = judge(
        NAS,
        entry(
            heading=[de("Steuerung und Kontrolle"), en("Control and Monitoring")],
            variant_names=[de("SuK"), de("S&K"), en("C&M")],
            explanation=[de("Ein Fach.")],
        ),
    )
    assert codes(found) == []


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"heading": ["Steuerung"]}, ["data.structural.datatype_mismatch"]),
        ({"heading": [{"lang": "DE", "value": "x"}]}, []),  # normalize puts it in canonical case
        ({"heading": [{"lang": "de_DE", "value": "x"}]}, ["data.structural.datatype_mismatch"]),
        ({"heading": [de("x" * 81)]}, ["data.structural.facet_violation"]),
        ({"heading": [de("a"), de("b")]}, ["data.structural.language_repeated"]),
        ({"heading": [de("a")], "variant_names": [de("b"), de("c")]}, []),
        ({"heading": de("a")}, ["data.structural.cardinality_many"]),
        ({"heading": [de("a")], "usage_hint": "untagged"}, ["data.structural.datatype_mismatch"]),
    ],
)
def test_data_checks(attrs: dict[str, Any], expected: list[str]) -> None:
    _, found = judge(NAS, entry(**attrs))
    assert codes(found) == expected


def test_language_repeated_names_the_tags() -> None:
    _, found = judge(NAS, entry(heading=[de("a"), en("b"), de("c"), en("d")]))
    (finding,) = found
    assert finding.target["path"] == "attrs.heading"
    assert finding.actual == ["de", "en"]


def test_names_in_any_language_count_as_duplicates() -> None:
    existing = revision("old", "nas:GlossaryEntry", {"heading": [de("Kontrolle")]})
    _, found = judge(NAS, entry(heading=[en("Kontrolle")]), base=DictSnapshot(existing))
    (finding,) = found
    assert finding.code == "data.semantic.possible_duplicate"
    assert "'Kontrolle'" in finding.message


def test_check_graph_reports_untagged_and_non_canonical_stored_values() -> None:
    untagged = revision("g", "nas:GlossaryEntry", {"heading": ["untagged"]})
    upper = revision("h", "nas:GlossaryEntry", {"heading": [{"lang": "DE", "value": "x"}]})
    found = check_graph(NAS, DictSnapshot(untagged, upper))
    assert codes(found) == ["data.structural.datatype_mismatch"] * 2


def test_rules_see_the_text() -> None:
    thing = revision("g", "nas:GlossaryEntry", {"heading": [de("a")], "usage_hint": en("x")})
    snapshot = DictSnapshot(thing)
    assert evaluate({"eq": [{"attr": "usage_hint"}, {"const": "x"}]}, thing, snapshot, NAS)
    assert evaluate({"in": [{"const": "a"}, {"attr": "heading"}]}, thing, snapshot, NAS)
    assert evaluate({"eq": [{"attr": "heading"}, {"const": ["a"]}]}, thing, snapshot, NAS)
    empty = revision("e", "nas:GlossaryEntry", {"usage_hint": en("")})
    assert evaluate({"nonempty": {"attr": "usage_hint"}}, empty, snapshot, NAS) is False


# ---- export -------------------------------------------------------------


def test_json_schema_export() -> None:
    schema = export_jsonschema(NAS)["nas.GlossaryEntry.json"]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    heading = properties["heading"]["items"]
    assert heading["x-graph_cli-base"] == "lang_string"
    assert heading["properties"]["value"] == {
        "type": "string",
        "maxLength": 80,
        "not": {"pattern": "[\\r\\n]"},
    }
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors({"heading": [de("SuK")]}))
    assert list(validator.iter_errors({"heading": ["SuK"]}))
    assert list(validator.iter_errors({"heading": [{"lang": "de", "value": "x" * 81}]}))
    assert list(validator.iter_errors({"explanation": [{"lang": "de_DE", "value": "x"}]}))
