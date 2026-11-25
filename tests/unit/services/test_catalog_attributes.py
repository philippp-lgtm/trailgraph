"""Functions, actions, describe and the generated skill of a reader with an attribute view
(field visibility plan F5-F7): which entries the model view hides and why, what ``describe``
shows of markings, the generated skill (goldens) and its digest.

The ``staff`` toy ontology marks the personal data of employees with ``stf:personal`` (the
salary also with ``stf:pay``). The readers: a holder of every marking (the unlimited one), a
holder of ``stf:personal`` only and a holder of none. The oracle for the last is the twin of
``support.markings``, as in ``test_model_view_attributes``.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.fixtures import FIXTURES, GOLDENS, assert_golden, compiled_model
from support.markings import (
    ACTION_SOURCES,
    FUNCTION,
    FUNCTION_SOURCES,
    NAME_ONLY,
    twin_model,
)
from support.skills import GRAPH_SKILL_NAME, graph_entry, render_inputs

from graph_cli.kernel.read_view import ReadView
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import load_entries
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.describe import (
    describe_actions,
    describe_fields,
    describe_functions,
    describe_type,
)
from graph_cli.services.visibility import attribute_mask, filter_model
from graph_cli.services.visible_catalog import (
    action_hidden,
    function_hidden,
    hidden_actions,
    hidden_functions,
)
from graph_cli.skills.graph_skill import L0_BASE, measure, render_checked
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import SKILL_FILE, read_tree

MODEL = compiled_model("staff")
ENTRY = graph_entry(MODEL)
BLOCKS = FIXTURES / "skills" / "blocks" / GRAPH_SKILL_NAME
EVERYTHING = frozenset({"stf:personal", "stf:pay"})
PERSONAL = frozenset({"stf:personal"})
NOTHING: frozenset[str] = frozenset()


def compiled_actions() -> dict[str, Any]:
    document, problems = compile_actions(ACTION_SOURCES, MODEL)
    assert [p.to_json() for p in problems] == []
    return document


def compiled_functions() -> dict[str, Any]:
    document, problems = compile_catalog(FUNCTION_SOURCES, MODEL)
    assert [p.to_json() for p in problems] == []
    return document


ACTIONS_DOC = compiled_actions()
FUNCTIONS_DOC = compiled_functions()
ACTIONS = load_action_entries(ACTIONS_DOC)
FUNCTIONS = load_entries(FUNCTIONS_DOC)


def names(entries: Any) -> list[str]:
    return [e.name for e in entries]


class Reader:
    """What a reader who holds ``held`` is given: its model and the entries that fit it."""

    def __init__(self, held: frozenset[str]) -> None:
        self.held = held
        self.mask = attribute_mask(MODEL, held)
        self.view = ReadView(None, self.mask)
        self.model = filter_model(MODEL, None, self.mask)
        self.hidden_functions = hidden_functions(FUNCTIONS, FUNCTIONS_DOC, MODEL, self.model)
        self.hidden_actions = hidden_actions(ACTIONS, ACTIONS_DOC, MODEL, self.model)
        self.functions = tuple(f for f in FUNCTIONS if f.name not in self.hidden_functions)
        self.actions = tuple(a for a in ACTIONS if a.name not in self.hidden_actions)

    def catalog(self, key: str, document: dict[str, Any], kept: Any) -> dict[str, Any]:
        wanted = set(names(kept))
        return {**document, key: [e for e in document[key] if e["name"] in wanted]}

    def documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        functions = self.catalog("functions", FUNCTIONS_DOC, self.functions)
        actions = self.catalog("actions", ACTIONS_DOC, self.actions)
        return render_inputs(self.model, functions=functions or None, actions=actions or None)


# ---- what an attribute view hides --------------------------------------------------------


def test_the_toy_catalogs_hold_what_the_checks_below_need() -> None:
    assert names(ACTIONS) == [
        "set_email",
        "set_grade",
        "set_phone",
        "set_phone_badged",
        "set_salary",
    ]
    assert names(FUNCTIONS) == ["by_grade", "colleague_names", "employee_mail"]


def test_a_reader_who_holds_every_marking_loses_nothing() -> None:
    reader = Reader(EVERYTHING)
    assert reader.mask is None and not reader.view.limited
    assert reader.hidden_functions == reader.hidden_actions == frozenset()


def test_an_action_that_sets_or_reads_a_hidden_attribute_is_hidden() -> None:
    nothing = Reader(NOTHING)
    # the e-mail, the salary and the grade are unknown to it; the badge code is unknown to the
    # precondition, so the action that checks it does not compile over the view either
    assert nothing.hidden_actions == {
        "set_email",
        "set_grade",
        "set_phone_badged",
        "set_salary",
    }
    assert names(nothing.actions) == ["set_phone"]
    # holding stf:personal shows all of them but the salary, which stf:pay also marks
    assert Reader(PERSONAL).hidden_actions == {"set_salary"}


def test_a_function_that_reads_a_hidden_attribute_is_hidden() -> None:
    nothing = Reader(NOTHING)
    assert nothing.hidden_functions == {"by_grade", "employee_mail"}
    assert names(nothing.functions) == ["colleague_names"]
    assert Reader(PERSONAL).hidden_functions == frozenset()


def test_a_parameter_typed_by_an_enum_only_hidden_attributes_use_hides_the_entry() -> None:
    view = Reader(NOTHING).model
    assert "stf:PayGrade" not in view.enums  # only the marked pay grade uses it
    grade = next(f for f in FUNCTIONS if f.name == "by_grade")
    # the query names no hidden attribute: the parameter alone makes the entry hidden
    assert "pay_grade" not in json.dumps(grade.defn.to_json() | {"query": grade.defn.query})
    assert function_hidden(grade, view)
    assert not function_hidden(grade, MODEL)
    assert action_hidden(next(a for a in ACTIONS if a.name == "set_grade"), view)
    phone = next(a for a in ACTIONS if a.name == "set_phone")
    assert not action_hidden(phone, view)


def test_a_parameter_typed_by_an_enum_no_attribute_uses_keeps_the_entry() -> None:
    unused = MODEL.enums["stf:PayGrade"]  # the same values under a name no attribute uses
    model = replace(MODEL, enums={**MODEL.enums, "stf:Unused": unused})
    source = FUNCTION.format(
        name="by_unused",
        summary="Names, echoing a value of an unused enum.",
        params="\n  value:\n    type: enum\n    enum: stf:Unused\n    summary: A value.",
        query="MATCH (n) RETURN n.name AS name, $value AS value",
        returns=f"{NAME_ONLY}\n  - column: value\n    summary: The value.",
    )
    document, problems = compile_catalog({"by_unused.yaml": source}, model)
    assert [p.to_json() for p in problems] == []
    (entry,) = load_entries(document)
    view = filter_model(model, None, attribute_mask(model, NOTHING))
    assert "stf:Unused" in view.enums and "stf:PayGrade" not in view.enums
    assert not function_hidden(entry, view)


# ---- preconditions: the compile check leaves no gap ---------------------------------------

FAMILY = """\
format: graph_cli.ir/1
module:
  id: family
  prefix: fm
  version: 1.0.0
  summary: Parties that share a marked note.
markings:
  personal:
    summary: Personal data of a natural person.
classes:
  Party:
    summary: A party.
    abstract: true
    attributes:
      name: {summary: Name., type: string, role: label}
      note: {summary: Note., type: string, markings: [personal]}
  Person:
    summary: A natural person.
    is_a: Party
  Company:
    summary: A company.
    is_a: Party
"""
FAMILY_ACTION = """\
schema: graph_cli.action/1
name: tidy_party
summary: Rename a party that has a note.
use_when: A party with a note is renamed.
params:
  who: {type: ref, class: fm:Party, summary: The party.}
  name: {type: string, summary: The new name.}
preconditions:
  - id: has_note
    on: who
    summary: The party has a note.
    require:
      - {attr: note, value_presence: PRESENT}
ops:
  - {op: update, id: $who, set: {name: $name}}
examples:
  - request: Rename the party.
"""


def family_model(tmp_path: Path, text: str) -> CompiledModel:
    (tmp_path / "family.yaml").write_text(text)
    outcome = compile_bundle("family", LayeredSource(DirectorySource(tmp_path)))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


@pytest.mark.doc_claim("security_model.marked_rules_are_a_channel")
def test_a_precondition_on_a_class_reads_what_every_subclass_shows(tmp_path: Path) -> None:
    """The compile check over the model view is enough for preconditions (no read of
    ``expr_attributes.reads`` is added): a subclass cannot declare an inherited attribute
    again, so the whole family of a parameter's class shows an attribute of that class or hides
    it, and a ``sum`` is checked on every class at the end of its relation."""
    model = family_model(tmp_path, FAMILY)
    document, problems = compile_actions({"tidy_party.yaml": FAMILY_ACTION}, model)
    assert [p.to_json() for p in problems] == []
    entries = load_action_entries(document)
    view = filter_model(model, None, attribute_mask(model, frozenset()))
    assert "note" not in view.classes["fm:Party"].attributes  # no class of the family shows it
    assert hidden_actions(entries, document, model, view) == {"tidy_party"}
    held = filter_model(model, None, attribute_mask(model, {"fm:personal"}))
    assert hidden_actions(entries, document, model, held) == frozenset()
    redeclared = FAMILY.replace(
        "    is_a: Party\n  Company:",
        "    is_a: Party\n    attributes:\n      note: {summary: Own note., type: string}\n"
        "  Company:",
    )
    (tmp_path / "family.yaml").write_text(redeclared)
    outcome = compile_bundle("family", LayeredSource(DirectorySource(tmp_path)))
    assert [f.code for f in outcome.findings] == ["model.semantic.attribute_redefined"]


def test_a_sum_in_a_precondition_over_classes_that_differ_in_visibility_is_hidden() -> None:
    """The weekly hours of the employees at the end of a relation are marked: the action over
    them does not compile over the view of a reader who lacks the marking."""
    sources = {
        "crew_budget.yaml": """\
schema: graph_cli.action/1
name: crew_budget
summary: Rename a crew that works few hours.
use_when: A crew with few hours is renamed.
params:
  crew: {type: ref, class: stf:Crew, summary: The crew.}
  name: {type: string, summary: The new name.}
preconditions:
  - id: few_hours
    on: crew
    summary: The employees of the crew work at most 400 hours.
    require:
      - sum: {rel: stf:in_crew, direction: in, attr: weekly_hours, where: {type: [stf:Employee]}}
        maximum_value: 400
ops:
  - {op: update, id: $crew, set: {name: $name}}
examples:
  - request: Rename the crew.
"""
    }
    document, problems = compile_actions(sources, MODEL)
    assert [p.to_json() for p in problems] == []
    entries = load_action_entries(document)
    view = Reader(NOTHING).model
    assert hidden_actions(entries, document, MODEL, view) == {"crew_budget"}
    assert hidden_actions(entries, document, MODEL, Reader(PERSONAL).model) == frozenset()


# ---- describe -----------------------------------------------------------------------------


def field(document: dict[str, Any], name: str) -> dict[str, Any]:
    body = document.get("type", document)
    found = next(f for f in body["fields"] if f["name"] == name)
    assert isinstance(found, dict)
    return found


def test_describe_shows_the_markings_of_the_attributes_a_reader_sees() -> None:
    everything = describe_type(ENTRY, MODEL, "stf:Employee")
    assert field(everything, "email")["markings"] == ["stf:personal"]
    assert field(everything, "salary")["markings"] == ["stf:pay", "stf:personal"]
    assert "markings" not in field(everything, "phone")
    personal = describe_type(ENTRY, Reader(PERSONAL).model, "stf:Employee")
    assert field(personal, "email")["markings"] == ["stf:personal"]
    assert "salary" not in {f["name"] for f in personal["type"]["fields"]}  # type: ignore[index]
    nothing = describe_type(ENTRY, Reader(NOTHING).model, "stf:Employee")
    assert all("markings" not in f for f in nothing["type"]["fields"])  # type: ignore[index]
    text = json.dumps(describe_fields(ENTRY, Reader(NOTHING).model, "stf:Employee"))
    assert "stf:personal" not in text and "stf:pay" not in text and "email" not in text


def test_the_view_of_a_reader_without_markings_describes_as_the_twin() -> None:
    twin, nothing = twin_model(), Reader(NOTHING).model
    for curie in ("stf:Employee", "stf:Badge", "stf:Contractor"):
        assert describe_type(ENTRY, nothing, curie) == describe_type(ENTRY, twin, curie)


def golden(name: str, document: dict[str, Any]) -> None:
    data = json.loads(json.dumps(document))
    del data["graph"]["id"]
    assert_golden(
        GOLDENS / "describe" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode()
    )


@pytest.mark.parametrize(
    ("label", "held"), [("holder", EVERYTHING), ("personal", PERSONAL), ("nothing", NOTHING)]
)
def test_describe_goldens_with_a_marked_attribute(label: str, held: frozenset[str]) -> None:
    reader = Reader(held)
    golden(
        f"markings_type_staff_employee_{label}", describe_type(ENTRY, reader.model, "stf:Employee")
    )
    golden(f"markings_actions_staff_{label}", describe_actions(ENTRY, reader.model, reader.actions))
    golden(
        f"markings_functions_staff_{label}",
        describe_functions(ENTRY, reader.model, reader.functions),
    )


# ---- the generated skill -------------------------------------------------------------------


def template_files() -> dict[str, bytes]:
    files, problems = read_tree(BLOCKS)
    assert problems == []
    return files


def render(reader: Reader) -> dict[str, bytes]:
    documents, graph = reader.documents()
    rendered, problems = render_checked(template_files(), documents, graph, name=GRAPH_SKILL_NAME)
    assert [p.to_json() for p in problems] == [] and rendered is not None
    return rendered


def test_the_skill_of_a_holder_marks_the_fields_and_that_of_a_reader_without_does_not() -> None:
    holder = render(Reader(EVERYTHING))[SKILL_FILE].decode()
    assert "`email` (marked stf:personal)" in holder
    assert "`salary` (decimal, marked stf:pay+stf:personal)" in holder
    assert "`holder_name` (main name, marked stf:personal)" in holder
    assert "`phone`" in holder and "`phone` (" not in holder  # unmarked: as before
    for path, data in render(Reader(EVERYTHING)).items():
        if path != SKILL_FILE:
            assert "marked stf:personal" in data.decode() or "markings" not in data.decode()
    nothing = render(Reader(NOTHING))
    for path, data in nothing.items():
        text = data.decode()
        for token in ("marked", "stf:personal", "stf:pay", "badge_code", "salary", "PayGrade"):
            assert token not in text, (path, token)
    assert "`phone`" in nothing[SKILL_FILE].decode()


@pytest.mark.parametrize(
    ("label", "held"), [("holder", EVERYTHING), ("personal", PERSONAL), ("nothing", NOTHING)]
)
def test_skill_goldens_with_a_marked_attribute(label: str, held: frozenset[str]) -> None:
    for path, data in render(Reader(held)).items():
        assert_golden(GOLDENS / "skills" / f"staff_{label}" / path, data)


def test_a_reader_without_a_marked_field_gets_the_skill_of_a_graph_without_markings() -> None:
    """The twin has no marking: the skill of a reader who holds none is its skill, but for the
    entries of the catalogs (the twin has no catalogs here)."""
    twin = twin_model()
    nothing = Reader(NOTHING)
    documents, graph = render_inputs(nothing.model)
    twin_documents, _ = render_inputs(twin)
    assert documents == twin_documents
    plain, _ = render_checked(template_files(), documents, graph, name=GRAPH_SKILL_NAME)
    again, _ = render_checked(template_files(), twin_documents, graph, name=GRAPH_SKILL_NAME)
    assert plain == again


# ---- the budget ----------------------------------------------------------------------------

MARK_BYTES = 40  # one mark in ``SKILL.md``: ``, marked stf:pay+stf:personal``, a little over


def unmarked(model: CompiledModel) -> CompiledModel:
    """``model`` with every marking taken off (the same fields and prose, no mark)."""
    classes = {
        curie: replace(
            cls,
            attributes={n: replace(a, markings=()) for n, a in cls.attributes.items()},
        )
        for curie, cls in model.classes.items()
    }
    return replace(model, classes=classes, markings={})


def test_a_marked_attribute_costs_a_few_bytes_of_level_zero() -> None:
    template, problems = parse_template(template_files())
    assert problems == [] and template is not None
    documents, graph = render_inputs(MODEL)
    marked = measure(template, documents, graph)
    plain_documents, plain_graph = render_inputs(unmarked(MODEL))
    plain = measure(template, plain_documents, plain_graph)
    count = sum(
        1
        for curie, cls in MODEL.classes.items()
        for a in cls.attributes.values()
        if a.markings and a.declared_in == curie
    )
    assert count == 8
    extra = marked.skill - plain.skill
    assert 0 < extra <= count * MARK_BYTES, (extra, count)
    assert marked.curated == plain.curated and not marked.over and marked.skill <= L0_BASE


def test_a_graph_without_markings_renders_the_same_bytes() -> None:
    twin = twin_model()
    assert not twin.features() and twin.markings == {}
    documents, graph = render_inputs(twin)
    assert b"marked" not in json.dumps(documents).encode()
    assert all(
        b"marked " not in data
        for data in render_checked(template_files(), documents, graph, name=GRAPH_SKILL_NAME)[
            0
        ].values()
    )  # type: ignore[union-attr]


# ---- the claim digest ----------------------------------------------------------------------


def digest(held: frozenset[str], model: CompiledModel = MODEL, types: frozenset[str] | None = None):  # type: ignore[no-untyped-def]
    return ReadView(types, attribute_mask(model, held)).digest


def test_the_digest_follows_what_the_reader_sees_of_the_attributes() -> None:
    assert digest(EVERYTHING) is None  # a holder of every marking is no limited reader
    assert digest(NOTHING) != digest(PERSONAL) and None not in (digest(NOTHING), digest(PERSONAL))
    assert digest(NOTHING) == digest(NOTHING)  # stable
    # a release that marks an attribute changes the claim of a reader who lacks the marking
    assert digest(NOTHING) != digest(NOTHING, unmarked(MODEL))
    # a holder is not limited before the release either: its claim is unchanged
    assert digest(EVERYTHING, unmarked(MODEL)) is None
    # a marking only a type outside the reader's types uses does not change its claim
    crew = frozenset({"stf:Crew"})
    assert digest(NOTHING, types=crew) == digest(PERSONAL, types=crew)


def test_the_claim_of_a_reader_limited_by_types_alone_is_the_stage_3_digest() -> None:
    types = frozenset({"stf:Employee", "stf:Crew"})
    assert ReadView(types, attribute_mask(MODEL, EVERYTHING)).digest == ReadView.of(types).digest
