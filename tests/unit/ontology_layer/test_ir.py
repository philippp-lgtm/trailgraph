"""IR parsing details not covered by the one-defect broken fixtures."""

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle

HEADER = """format: graph_cli.ir/1
module: {id: sample, prefix: smp, version: 1.0.0, summary: Sample module.}
"""

THING = """classes:
  Thing:
    summary: A thing.
    attributes:
      label: {summary: Label., type: string}
      rank: {summary: Rank., type: integer}
      colour: {summary: Colour., type: string}
relations:
  next: {summary: Next thing., domain: Thing, range: Thing}
"""


def compile_text(text: str):  # type: ignore[no-untyped-def]
    return compile_bundle("sample", LayeredSource(MappingSource({"sample": text.encode()})))


def test_one_run_reports_every_structural_problem() -> None:
    outcome = compile_text(
        HEADER
        + """classes:
  Thing:
    summary: A thing.
    colour: red
    attributes:
      label: {type: string, required: maybe}
"""
    )
    codes = sorted(f.code for f in outcome.findings)
    assert codes == [
        "model.structural.invalid_value",
        "model.structural.missing_summary",
        "model.structural.unknown_key",
    ]
    assert outcome.model is None


def test_condition_operators_lower_to_the_ast() -> None:
    outcome = compile_text(
        HEADER
        + THING
        + """rules:
  - id: sample_rule
    summary: Exercises every operator.
    severity: warning
    when: batch
    scope: Thing
    if:
      - {attr: label, value_presence: ABSENT}
    then:
      - all_of:
          - {attr: rank, minimum_value: 1, maximum_value: 9}
          - {attr: colour, equals_string_in: [red, blue]}
      - {count: {rel: next, direction: out, where: {type: Thing}}, maximum_value: 1}
"""
    )
    assert outcome.findings == ()
    assert outcome.model is not None
    (rule,) = outcome.model.rules
    assert rule.id == "smp.sample_rule"
    assert rule.expr == {
        "implies": [
            {"not": {"exists": {"attr": "label"}}},
            {
                "and": [
                    {
                        "and": [
                            {
                                "and": [
                                    {"ge": [{"attr": "rank"}, {"const": 1}]},
                                    {"le": [{"attr": "rank"}, {"const": 9}]},
                                ]
                            },
                            {"in": [{"attr": "colour"}, {"const": ["red", "blue"]}]},
                        ]
                    },
                    {
                        "le": [
                            {
                                "count": {
                                    "rel": "smp:next",
                                    "direction": "out",
                                    "where": {"type": ["smp:Thing"]},
                                }
                            },
                            {"const": 1},
                        ]
                    },
                ]
            },
        ]
    }


def test_conditions_without_operator_or_subject_are_invalid() -> None:
    outcome = compile_text(
        HEADER
        + THING
        + """rules:
  - id: no_operator
    summary: Missing operator.
    severity: error
    when: write
    scope: Thing
    then:
      - {attr: label}
"""
    )
    assert [f.code for f in outcome.findings] == ["model.structural.invalid_value"]


def test_namespace_default_and_annotations() -> None:
    outcome = compile_text(
        HEADER
        + """extensions:
  probe: {enabled: true, level: 3}
classes:
  Thing:
    summary: A thing.
    annotations:
      probe: {colour: red}
"""
    )
    assert outcome.model is not None
    (module,) = [m for m in outcome.model.modules if m.id == "sample"]
    assert module.namespace == "urn:graph_cli:onto:sample#"
    assert module.extensions == {"probe": {"enabled": True, "level": 3}}
    assert outcome.model.classes["smp:Thing"].doc.annotations == {"probe": {"colour": "red"}}
