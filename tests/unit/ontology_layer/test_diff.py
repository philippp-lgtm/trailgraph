"""Semver classifier (contracts B7.12): fixture version series goldens and rule table."""

from dataclasses import replace
from itertools import pairwise

import pytest
from support.fixtures import (
    ONTOLOGIES,
    TOY_ONTOLOGIES,
    assert_golden,
    compiled_model,
    ontology_versions,
)

from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.diff import check_bump, classify
from graph_cli.ontology_layer.model import CompiledModel

SERIES = ontology_versions("inventory")
PAIRS = list(pairwise(SERIES))


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_version_series_golden(name: str) -> None:
    reports = [
        classify(compiled_model(name, a), compiled_model(name, b)).to_json()
        for a, b in pairwise(ontology_versions(name))
    ]
    document = {"schema": "graph_cli.fixture_diffs/1", "ontology": name, "diffs": reports}
    assert_golden(ONTOLOGIES / name / "expected_diffs.json", pretty_bytes(document))


def test_importing_files_is_minor() -> None:
    report = classify(
        compiled_model("notes_and_sources", "1.0.0"), compiled_model("notes_and_sources", "1.1.0")
    )
    assert report.level == "minor"


@pytest.mark.parametrize(
    ("old", "new", "level", "count"),
    [
        ("1.0.0", "1.0.1", "patch", 3),
        ("1.0.1", "1.1.0", "minor", 5),
        ("1.1.0", "2.0.0", "major", 3),
        ("2.0.0", "2.1.0", "minor", 2),
    ],
)
def test_series_levels(old: str, new: str, level: str, count: int) -> None:
    report = classify(compiled_model("inventory", old), compiled_model("inventory", new))
    assert report.level == level
    assert len(report.changes) == count
    assert all(change.level == level for change in report.changes)
    assert check_bump(report) == "ok"


def test_declared_rename_and_breaking_changes() -> None:
    report = classify(compiled_model("inventory", "1.1.0"), compiled_model("inventory", "2.0.0"))
    assert [(c.path, c.kind) for c in report.changes] == [
        ("classes.inv:Item.attributes.name", "rename"),
        ("enums.inv:Condition.values.worn", "remove_enum_value"),
        ("relations.inv:in_category.cardinality.out.min", "tighten_cardinality"),
    ]
    assert dict(report.changes[0].detail) == {"to": "label"}


def test_bump_checks() -> None:
    report = classify(compiled_model("inventory", "1.0.1"), compiled_model("inventory", "1.1.0"))
    assert check_bump(replace(report, to_version="1.0.2")) == "insufficient"
    assert check_bump(replace(report, to_version="2.0.0")) == "excessive"
    assert check_bump(replace(report, to_version="1.0.0")) == "insufficient"


def test_identical_models_have_no_changes() -> None:
    model = compiled_model("notes_and_sources")
    report = classify(model, model)
    assert (report.level, report.changes) == ("none", ())


HEADER = "format: graph_cli.ir/1\nmodule: {{id: sample, prefix: smp, version: {v}, summary: S.}}\n"
BODY = """datatypes:
  Code: {summary: Code., base: string, max_length: 10}
classes:
  Thing:
    summary: A thing.
    attributes:
      code: {summary: Code., type: Code, mutability: immutable}
      title: {summary: Title., type: string, role: label}
      state: {summary: State., lifecycle: Flow}
  Other:
    summary: Another thing.
relations:
  link: {summary: Link., domain: Thing, range: Thing}
lifecycles:
  Flow:
    summary: Flow.
    states:
      open: {summary: Open., initial: true}
      held: {summary: Held.}
      shut: {summary: Shut., terminal: true}
    transitions:
      - {from: open, to: held}
      - {from: held, to: shut}
"""


def model(body: str, version: str) -> CompiledModel:
    text = HEADER.format(v=version) + body
    outcome = compile_bundle("sample", LayeredSource(MappingSource({"sample": text.encode()})))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


@pytest.mark.parametrize(
    ("old", "new", "kind", "level"),
    [
        ("max_length: 10", "max_length: 5", "tighten_facet", "major"),
        ("max_length: 10", "max_length: 20", "loosen_facet", "minor"),
        ("range: Thing}", "range: [Thing, Other]}", "widen_range", "minor"),
        ("domain: Thing,", "domain: Other,", "change_domain", "major"),
        ("mutability: immutable", "mutability: content", "loosen_mutability", "minor"),
        ("mutability: immutable}", "mutability: immutable, role: alt_label}", "add_role", "minor"),
        ("type: string, role: label}", "type: string}", "remove_role", "major"),
        ("role: label}", "role: hidden_label}", "change_role", "major"),
        (
            "{summary: Code., type: Code, mutability: immutable}",
            "{summary: Code., type: string}",
            "change_type",
            "major",
        ),
        (
            "      - {from: held, to: shut}\n",
            "      - {from: held, to: shut}\n      - {from: open, to: shut}\n",
            "add_transition",
            "minor",
        ),
        (
            "  Other:\n    summary: Another thing.\n",
            "  Renamed:\n    summary: Another thing.\n    renamed_from: Other\n",
            "rename",
            "major",
        ),
        (
            "relations:\n",
            "rules:\n  - {id: r1, summary: R., severity: error, when: write, "
            "scope: Thing, then: [{attr: code, value_presence: PRESENT}]}\nrelations:\n",
            "add_error_rule",
            "major",
        ),
        (
            "transitions:\n      - {from: open, to: held}",
            "transitions:\n      - from: open\n"
            "        to: held\n        guards:\n          - id: g1\n            summary: G.\n"
            "            require: [{attr: code, value_presence: PRESENT}]",
            "add_guard",
            "major",
        ),
    ],
)
def test_rule_table(old: str, new: str, kind: str, level: str) -> None:
    assert old in BODY
    report = classify(model(BODY, "1.0.0"), model(BODY.replace(old, new), "2.0.0"))
    assert (kind, level) in [(c.kind, c.level) for c in report.changes], report.to_json()
    assert report.level == max(
        (c.level for c in report.changes), key=("patch", "minor", "major").index
    )


def _rule(severity: str, when: str, then: str) -> str:
    return (
        f"rules:\n  - {{id: r1, summary: R., severity: {severity}, when: {when}, "
        f"scope: Thing, then: [{then}]}}\nrelations:\n"
    )


_CODE = "{attr: code, value_presence: PRESENT}"
_EITHER = (
    "{any_of: [{attr: code, value_presence: PRESENT}, {attr: state, value_presence: PRESENT}]}"
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # F8: loosening a warning rule must not force a major release
        (_rule("warning", "write", _CODE), _rule("warning", "write", _EITHER), "minor"),
        (_rule("warning", "write", _EITHER), _rule("warning", "write", _CODE), "minor"),
        (_rule("warning", "write", _CODE), _rule("warning", "batch", _CODE), "minor"),
        (_rule("warning", "write", _CODE), "relations:\n", "minor"),
        (_rule("error", "write", _CODE), _rule("error", "write", _EITHER), "major"),
        (_rule("error", "write", _CODE), _rule("error", "batch", _CODE), "major"),
        (_rule("error", "write", _CODE), "relations:\n", "major"),
        (_rule("error", "write", _CODE), _rule("warning", "write", _EITHER), "minor"),
        (_rule("warning", "write", _CODE), _rule("error", "write", _EITHER), "major"),
    ],
    ids=[
        "warning_loosened",
        "warning_tightened",
        "warning_timing",
        "warning_removed",
        "error_loosened",
        "error_timing",
        "error_removed",
        "error_to_warning",
        "warning_to_error",
    ],
)
def test_rule_changes_follow_the_resulting_severity(old: str, new: str, expected: str) -> None:
    before = model(BODY.replace("relations:\n", old), "1.0.0")
    after = model(BODY.replace("relations:\n", new), "2.0.0")
    report = classify(before, after)
    rule_changes = [c for c in report.changes if c.path == "rules.smp.r1"]
    assert rule_changes, report.to_json()
    assert report.level == expected, report.to_json()


_STATE = "      state: {summary: State., lifecycle: Flow}\n"
_SIZED = BODY.replace(_STATE, _STATE + "      size: {summary: Size., type: integer}\n")
_SUM = "{sum: {rel: link, direction: in, attr: size}, maximum_value: %s}"


@pytest.mark.parametrize(
    ("old", "new", "kind", "level"),
    [
        ("relations:\n", _rule("warning", "write", _SUM % "3"), "add_warning_rule", "minor"),
        (
            _rule("error", "write", _SUM % "3"),
            _rule("error", "write", _SUM % "{attr: size}"),
            "change_expression",
            "major",
        ),
        (
            _rule("warning", "write", _SUM % "3"),
            _rule("warning", "write", _SUM % "{attr: size}"),
            "change_expression",
            "minor",
        ),
    ],
    ids=["sum_warning_added", "sum_error_changed", "sum_warning_changed"],
)
def test_sum_rules_classify_like_other_rules(old: str, new: str, kind: str, level: str) -> None:
    before = model(_SIZED.replace("relations:\n", old), "1.0.0")
    after = model(_SIZED.replace("relations:\n", new), "2.0.0")
    report = classify(before, after)
    assert [(c.path, c.kind, c.level) for c in report.changes] == [("rules.smp.r1", kind, level)]
    assert report.level == level
