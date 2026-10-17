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


PARTS = """classes:
  Thing:
    summary: A thing.
    attributes:
      label: {summary: Label., type: string}
      rank: {summary: Rank., type: integer}
      limit: {summary: Limit., type: Amount}
      tags: {summary: Tags., type: integer, many: true}
      size: {summary: Size., type: SizeClass}
  Part:
    summary: A part.
    is_a: Thing
  Note:
    summary: A note.
    attributes:
      label: {summary: Label., type: string}
datatypes:
  Amount: {summary: An amount., base: decimal}
enums:
  SizeClass:
    summary: A size class.
    values: {small: {summary: Small.}, large: {summary: Large.}}
relations:
  part_of: {summary: Parent thing., domain: Thing, range: Thing}
  about: {summary: What a note is about., domain: Note, range: Thing}
"""


def rule_text(*conds: str, scope: str = "Thing") -> str:
    lines = "".join(f"      - {c}\n" for c in conds)
    return (
        HEADER
        + PARTS
        + f"""rules:
  - id: sample_rule
    summary: A sum rule.
    severity: warning
    when: write
    scope: {scope}
    then:
{lines}"""
    )


def lowered(*conds: str, scope: str = "Thing") -> object:
    outcome = compile_text(rule_text(*conds, scope=scope))
    assert [f.to_json() for f in outcome.findings] == []
    assert outcome.model is not None
    return outcome.model.rules[0].expr


def codes(*conds: str, scope: str = "Thing") -> list[tuple[str, str]]:
    outcome = compile_text(rule_text(*conds, scope=scope))
    return [(f.code, str(f.target["path"])) for f in outcome.findings]


SUM_IN = {"rel": "smp:part_of", "direction": "in", "attr": "rank"}


def test_sum_and_attr_right_sides_lower_to_the_ast() -> None:
    assert lowered("{sum: {rel: part_of, direction: in, attr: rank}, equals_number: 3}") == {
        "eq": [{"sum": SUM_IN}, {"const": 3}]
    }
    assert lowered(
        "{sum: {rel: part_of, direction: in, attr: limit, where: {record_status: [active],"
        " type: [Part]}}, value_presence: PRESENT, maximum_value: {attr: limit}}"
    ) == {
        "and": [
            {
                "exists": {
                    "sum": {
                        "rel": "smp:part_of",
                        "direction": "in",
                        "attr": "limit",
                        "where": {"record_status": ["active"], "type": ["smp:Part"]},
                    }
                }
            },
            {
                "le": [
                    {
                        "sum": {
                            "rel": "smp:part_of",
                            "direction": "in",
                            "attr": "limit",
                            "where": {"record_status": ["active"], "type": ["smp:Part"]},
                        }
                    },
                    {"attr": "limit"},
                ]
            },
        ]
    }
    assert lowered("{sum: {rel: part_of, attr: rank}, value_presence: ABSENT}") == {
        "not": {"exists": {"sum": {"rel": "smp:part_of", "direction": "out", "attr": "rank"}}}
    }
    assert lowered("{count: {rel: part_of, direction: in}, minimum_value: {attr: rank}}") == {
        "ge": [{"count": {"rel": "smp:part_of", "direction": "in"}}, {"attr": "rank"}]
    }
    assert lowered("{attr: rank, maximum_value: {attr: limit}, minimum_value: 0}") == {
        "and": [
            {"ge": [{"attr": "rank"}, {"const": 0}]},
            {"le": [{"attr": "rank"}, {"attr": "limit"}]},
        ]
    }


def test_sum_and_attr_right_sides_parse_errors() -> None:
    invalid, unknown_key = "model.structural.invalid_value", "model.structural.unknown_key"
    item = "rules.sample_rule.then[0]"
    assert codes("{sum: {rel: part_of}, equals_number: 1}") == [(invalid, f"{item}.sum.attr")]
    assert codes("{sum: {rel: part_of, attr: rank}}") == [(invalid, item)]
    assert codes("{count: {rel: part_of, attr: rank}, equals_number: 1}") == [
        (unknown_key, f"{item}.count.attr")
    ]
    assert codes("{count: {rel: part_of}, value_presence: PRESENT}") == [
        (invalid, item),
        (unknown_key, f"{item}.value_presence"),
    ]
    for rhs in ("1.5", "[1]", "x", "{}"):
        assert codes(f"{{sum: {{rel: part_of, attr: rank}}, maximum_value: {rhs}}}") == [
            (invalid, f"{item}.maximum_value")
        ]
    assert codes("{attr: rank, maximum_value: {attr: limit, scale: 2}}") == [
        (unknown_key, f"{item}.maximum_value.scale")
    ]
    assert codes("{attr: rank, maximum_value: {attr: Limit}}") == [
        (invalid, f"{item}.maximum_value.attr")
    ]


def test_sum_and_attr_right_sides_are_validated() -> None:
    item = "rules.sample_rule.then[0]"
    not_numeric = "model.rule.not_numeric"
    unknown_attr = "model.rule.unknown_attribute"
    # inherited attribute on Part, decimal datatype, integer
    ok = "{sum: {rel: part_of, direction: in, attr: limit}, maximum_value: {attr: limit}}"
    assert codes(ok) == []
    assert codes("{sum: {rel: part_of, direction: in, attr: label}, minimum_value: 0}") == [
        (not_numeric, f"{item}.sum.attr")
    ]
    assert codes("{sum: {rel: part_of, direction: in, attr: tags}, minimum_value: 0}") == [
        (not_numeric, f"{item}.sum.attr")
    ]
    assert codes("{sum: {rel: part_of, direction: in, attr: size}, minimum_value: 0}") == [
        (not_numeric, f"{item}.sum.attr")
    ]
    assert codes("{count: {rel: part_of, direction: in}, maximum_value: {attr: label}}") == [
        (not_numeric, f"{item}.maximum_value.attr")
    ]
    assert codes("{count: {rel: part_of, direction: in}, maximum_value: {attr: nope}}") == [
        (unknown_attr, f"{item}.maximum_value.attr")
    ]
    assert codes("{attr: label, maximum_value: {attr: rank}}") == [(not_numeric, f"{item}.attr")]
    assert codes("{attr: label, maximum_value: 3}") == []  # integer constants stay unchecked
    assert codes("{sum: {rel: nope, attr: rank}, minimum_value: 0}") == [
        ("model.referential.unknown_relation", f"{item}.sum.rel")
    ]
    assert codes("{sum: {rel: part_of, attr: rank, where: {type: [Nope]}}, minimum_value: 0}") == [
        ("model.referential.unknown_term", f"{item}.sum.where.type")
    ]


def test_summed_attribute_must_exist_at_every_other_end() -> None:
    item = "rules.sample_rule.then[0]"
    missing = "model.rule.aggregate_unknown_attribute"
    # notes point at things: a Thing sums over its notes, which have no rank
    assert codes("{sum: {rel: about, direction: in, attr: rank}, minimum_value: 0}") == [
        (missing, f"{item}.sum.attr")
    ]
    out = "{sum: {rel: about, direction: out, attr: rank}, minimum_value: 0}"
    assert codes(out, scope="Note") == []
    # where.type narrows the other end (with subclasses)
    narrowed = "{sum: {rel: part_of, direction: in, attr: rank, where: {type: [Part]}}"
    assert codes(narrowed + ", minimum_value: 0}") == []


def test_guards_check_sums_and_attr_right_sides() -> None:
    text = (
        HEADER
        + PARTS.replace(
            "      size: {summary: Size., type: SizeClass}\n",
            "      size: {summary: Size., type: SizeClass}\n"
            "      status: {summary: Status., lifecycle: Flow, mutability: lifecycle}\n",
        )
        + """lifecycles:
  Flow:
    summary: A flow.
    states:
      open: {summary: Open., initial: true}
      done: {summary: Done., terminal: true}
    transitions:
      - from: open
        to: done
        guards:
          - id: parts_fit
            summary: The parts fit.
            require:
              - {sum: {rel: about, direction: in, attr: rank}, maximum_value: {attr: missing}}
"""
    )
    outcome = compile_text(text)
    path = "lifecycles.Flow.transitions[0].guards[0].require[0]"
    assert [(f.code, f.target["path"], f.message) for f in outcome.findings] == [
        (
            "model.lifecycle.guard_unknown_attribute",
            f"{path}.maximum_value.attr",
            f"smp:{scope} has no attribute 'missing'",
        )
        for scope in ("Part", "Thing")  # every class with the lifecycle
    ] + [
        (
            "model.rule.aggregate_unknown_attribute",
            f"{path}.sum.attr",
            "smp:Note at the other end of smp:about has no attribute 'rank'; "
            "declare it on every class there or narrow with where.type",
        )
    ]
