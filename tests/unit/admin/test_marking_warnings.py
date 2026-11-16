"""What an installed policy and a release say about markings (field visibility plan F2, step 2):
``marking_warnings`` (unknown, ungranted, required marked attributes, marked attributes in
expressions) and the deny-rule guard, which marking resources never trigger.

The models are small inline ontologies; the policies are compiled from their authored form.
"""

import json
from collections.abc import Mapping

import pytest
from support.fixtures import compiled_model

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.access_control.terms import RuleEntry, rule_entries
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, sources_hash
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import OntologyPin
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import error_codes, marking_warnings, renamed_terms
from graph_cli_admin.services_admin.release import policies_affected

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
PIN = OntologyPin("sample", "1.0.0", "sha256:" + "0" * 64)
ENTRY = GraphEntry(EXAMPLE, "example_graph", "active", PIN, {}, ())

SAMPLE = """\
format: graph_cli.ir/1
module: {id: sample, prefix: ex, version: 1.0.0, summary: Sample.}
markings:
  personal: {summary: Personal data.}
  financial: {summary: Money matters.}
  spare: {summary: Declared and never used.}
lifecycles:
  Flow:
    summary: The life of a ticket.
    states:
      open: {summary: Open., initial: true}
      closed: {summary: Closed., terminal: true}
    transitions:
      - from: open
        to: closed
        summary: Close the ticket.
        guards:
          - id: has_cost
            summary: A closed ticket has a cost.
            require:
              - {attr: cost, value_presence: PRESENT}
          - id: has_plain
            summary: A closed ticket has a remark.
            require:
              - {attr: plain, value_presence: PRESENT}
classes:
  Ticket:
    summary: A ticket.
    attributes:
      title: {summary: Title., type: string, required: true, role: label}
      reference: {summary: Reference., type: string, markings: [personal]}
      cost: {summary: Cost., type: integer, markings: [financial]}
      contact: {summary: Contact., type: string, required: true, markings: [personal]}
      plain: {summary: Remark., type: string}
      status: {summary: State., lifecycle: Flow, required: true}
  Part:
    summary: A part of a ticket.
    attributes:
      weight: {summary: Weight., type: integer, markings: [financial]}
      size: {summary: Size., type: integer}
relations:
  uses:
    summary: The parts a ticket uses.
    domain: Ticket
    range: Part
rules:
  - id: reference_needs_remark
    summary: A referenced ticket has a remark.
    severity: error
    when: write
    scope: Ticket
    if:
      - {attr: reference, value_presence: PRESENT}
    then:
      - {attr: plain, value_presence: PRESENT}
  - id: parts_weigh_something
    summary: The parts of a ticket weigh something.
    severity: warning
    when: write
    scope: Ticket
    if:
      - {count: {rel: uses}, minimum_value: 1}
    then:
      - sum: {rel: uses, attr: weight}
        minimum_value: 1
  - id: size_known
    summary: A ticket with parts has a remark.
    severity: warning
    when: write
    scope: Ticket
    if:
      - sum: {rel: uses, attr: size}
        value_presence: PRESENT
    then:
      - {attr: plain, value_presence: PRESENT}
  - id: remark_when_titled
    summary: A titled ticket has a remark.
    severity: warning
    when: write
    scope: Ticket
    if:
      - {attr: title, value_presence: PRESENT}
    then:
      - {attr: plain, value_presence: PRESENT}
"""

ACTION = """\
schema: graph_cli.action/1
name: close_ticket
summary: Close a ticket.
use_when: A ticket is done.
params:
  ticket:
    type: ref
    class: ex:Ticket
    summary: The ticket to close.
preconditions:
  - id: costed
    on: ticket
    summary: The ticket has a cost.
    require:
      - {attr: cost, value_presence: PRESENT}
  - id: remarked
    on: ticket
    summary: The ticket has a remark.
    require:
      - {attr: plain, value_presence: PRESENT}
ops:
  - {op: transition, id: $ticket, to: closed}
reason: required
examples:
  - request: The ticket is done.
"""


def compile_sample(text: str = SAMPLE) -> CompiledModel:
    outcome = compile_bundle("sample", LayeredSource(MappingSource({"sample": text.encode()})))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


MODEL = compile_sample()


def action_slot(model: CompiledModel = MODEL, source: str = ACTION) -> KineticSlot:
    files = {"close_ticket.yaml": source.encode()}
    compiled, problems = compile_actions(files, model)
    assert [p.to_json() for p in problems] == []
    return KineticSlot(sources_hash(files), hash_value(compiled), PIN, compiled, {}, "actions")


def installation(*grants: str) -> PolicySet:
    source = (
        "schema: graph_cli.policy/1\nid: installation\nroles:\n  hr: {description: HR}\n"
        "grants:\n" + "".join(f"  - {g}\n" for g in grants) + 'no_project: {"*": read}\n'
    )
    return policy_set(compile_policy(source.encode(), file="i.yaml", graphs=GRAPHS).document)


HR = (
    "{entrypoint: server, principal: {role: hr}, graphs: [example_graph], level: read,"
    " markings: [%s]}"
)
EVERYTHING = installation(HR % "ex:personal, ex:financial")
NOTHING = installation(
    "{entrypoint: server, principal: {role: hr}, graphs: [example_graph], level: read}"
)


def by_code(found: tuple[WarningRecord, ...]) -> dict[str, list[dict[str, JsonValue]]]:
    grouped: dict[str, list[dict[str, JsonValue]]] = {}
    for warning in found:
        grouped.setdefault(warning.code, []).append(dict(warning.details))
    return grouped


def of(
    code: str,
    policy: PolicySet = EVERYTHING,
    model: CompiledModel = MODEL,
    actions: KineticSlot | None = None,
) -> list[dict[str, JsonValue]]:
    return by_code(marking_warnings.warnings(policy, ENTRY, model, actions)).get(code, [])


# ---- the codes ---------------------------------------------------------------------------

CODES = (
    "admin.policy_unknown_marking",
    "admin.marking_ungranted",
    "admin.marking_in_expression",
    "admin.marking_blocks_create",
)


@pytest.mark.parametrize("code", CODES)
def test_the_marking_codes_are_registered_warnings(code: str) -> None:
    spec = next(s for s in error_codes.ADMIN_CODES if s.code == code)
    assert spec.category.value == "warning"


# ---- admin.policy_unknown_marking ---------------------------------------------------------


def test_a_grant_naming_an_undeclared_marking_warns() -> None:
    policy = installation(HR % "ex:personal, ex:financial, ex:ghost, ex:spare")
    assert of("admin.policy_unknown_marking", policy) == [
        {"graph": "example_graph", "marking": "ex:ghost"}
    ]
    message = next(
        w.message
        for w in marking_warnings.warnings(policy, ENTRY, MODEL)
        if w.code == "admin.policy_unknown_marking"
    )
    assert "ex:ghost" in message and "release 1.0.0" in message


def test_nothing_warns_when_every_granted_marking_is_declared() -> None:
    assert of("admin.policy_unknown_marking") == []
    assert of("admin.policy_unknown_marking", NOTHING) == []


def test_the_markings_of_other_graphs_are_not_this_graphs() -> None:
    elsewhere = installation(
        "{entrypoint: server, principal: {role: hr}, graphs: [other_graph], level: read,"
        " markings: [ex:ghost]}"
    )
    assert of("admin.policy_unknown_marking", elsewhere) == []


# ---- admin.marking_ungranted --------------------------------------------------------------


def test_a_used_marking_no_grant_names_warns() -> None:
    policy = installation(HR % "ex:personal")
    [found] = of("admin.marking_ungranted", policy)
    assert found["graph"] == "example_graph" and found["marking"] == "ex:financial"
    assert found["attributes"] == ["ex:Part.weight", "ex:Ticket.cost"]
    assert found["count"] == 2
    [warning] = [
        w for w in marking_warnings.warnings(policy, ENTRY, MODEL) if w.code.endswith("ungranted")
    ]
    assert "only maintainer surfaces show them" in warning.message


def test_a_declared_but_unused_marking_is_not_ungranted() -> None:
    markings = [d["marking"] for d in of("admin.marking_ungranted", NOTHING)]
    assert markings == ["ex:financial", "ex:personal"]  # ex:spare is used by no attribute


def test_a_granted_marking_is_not_ungranted() -> None:
    assert of("admin.marking_ungranted") == []


def test_without_an_installation_policy_no_app_caller_reads_or_writes_them() -> None:
    found = by_code(marking_warnings.warnings(BUILTIN_POLICY_SET, ENTRY, MODEL))
    assert [d["marking"] for d in found["admin.marking_ungranted"]] == [
        "ex:financial",
        "ex:personal",
    ]
    message = next(
        w.message
        for w in marking_warnings.warnings(BUILTIN_POLICY_SET, ENTRY, MODEL)
        if w.code == "admin.marking_ungranted"
    )
    assert "no installation policy is installed" in message
    assert "no app caller can read or write them" in message


def test_the_grants_of_another_graph_do_not_grant_this_one() -> None:
    elsewhere = installation(
        "{entrypoint: server, principal: {role: hr}, graphs: [other_graph], level: read,"
        " markings: [ex:personal, ex:financial]}"
    )
    assert len(of("admin.marking_ungranted", elsewhere)) == 2


def test_a_model_without_markings_gets_no_marking_warning() -> None:
    model = compiled_model("inventory", "1.1.0")
    assert marking_warnings.warnings(NOTHING, ENTRY, model) == ()
    assert marking_warnings.warnings(BUILTIN_POLICY_SET, ENTRY, model) == ()
    assert policies_affected(BUILTIN_POLICY_SET, ENTRY, model) == ()


# ---- admin.marking_blocks_create ----------------------------------------------------------


def test_a_required_marked_attribute_without_default_blocks_the_create() -> None:
    assert of("admin.marking_blocks_create") == [
        {
            "graph": "example_graph",
            "type": "ex:Ticket",
            "attribute": "contact",
            "markings": ["ex:personal"],
        }
    ]
    [warning] = [
        w for w in marking_warnings.warnings(EVERYTHING, ENTRY, MODEL) if "blocks" in w.code
    ]
    assert "cannot create ex:Ticket" in warning.message


def test_a_default_or_an_unmarked_required_attribute_does_not_block() -> None:
    text = SAMPLE.replace(
        "contact: {summary: Contact., type: string, required: true, markings: [personal]}",
        "contact: {summary: Contact., type: string, required: true, default: none,"
        " markings: [personal]}",
    )
    assert of("admin.marking_blocks_create", model=compile_sample(text)) == []
    # the status lifecycle is required and unmarked; title is required and unmarked
    assert [d["attribute"] for d in of("admin.marking_blocks_create")] == ["contact"]


# ---- admin.marking_in_expression ----------------------------------------------------------


def sites(found: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {f"{d['kind']}:{d['name']}": d["attributes"] for d in found}


def test_rules_and_guards_naming_marked_attributes_warn() -> None:
    found = of("admin.marking_in_expression")
    assert sites(found) == {
        "rule:ex.reference_needs_remark": ["ex:Ticket.reference"],
        # a sum names the attribute of the far end of its relation
        "rule:ex.parts_weigh_something": ["ex:Part.weight"],
        "guard:ex:Flow.ex.has_cost": ["ex:Ticket.cost"],
    }
    assert {d["name"]: d["markings"] for d in found} == {
        "ex.reference_needs_remark": ["ex:personal"],
        "ex.parts_weigh_something": ["ex:financial"],
        "ex:Flow.ex.has_cost": ["ex:financial"],
    }
    assert all(d["graph"] == "example_graph" for d in found)


def test_unmarked_attributes_in_expressions_do_not_warn() -> None:
    names = {d["name"] for d in of("admin.marking_in_expression")}
    # size_known sums an unmarked attribute, remark_when_titled reads unmarked ones only
    assert names.isdisjoint({"ex.size_known", "ex.remark_when_titled", "ex:Flow.ex.has_plain"})


def test_a_precondition_of_a_published_action_naming_a_marked_attribute_warns() -> None:
    found = of("admin.marking_in_expression", actions=action_slot())
    assert sites(found)["precondition:close_ticket.costed"] == ["ex:Ticket.cost"]
    assert "precondition:close_ticket.remarked" not in sites(found)
    # without the catalog the preconditions are not looked at
    assert not [d for d in of("admin.marking_in_expression") if d["kind"] == "precondition"]


def test_a_damaged_action_catalog_is_not_this_checks_to_report() -> None:
    slot = action_slot()
    broken = KineticSlot(slot.hash, slot.compiled_hash, PIN, {"actions": "nonsense"}, {}, "actions")
    assert [d["kind"] for d in of("admin.marking_in_expression", actions=broken)] == [
        "rule",
        "rule",
        "guard",
    ]


def test_an_inherited_marked_attribute_is_named_at_its_declaring_class() -> None:
    text = SAMPLE.replace(
        "  Part:\n",
        "  Bug:\n    summary: A defect.\n    is_a: Ticket\n    attributes: {}\n  Part:\n",
    ).replace(
        "rules:\n",
        """rules:
  - id: bug_reference
    summary: A referenced bug has a remark.
    severity: error
    when: write
    scope: Bug
    if:
      - {attr: reference, value_presence: PRESENT}
    then:
      - {attr: plain, value_presence: PRESENT}
""",
    )
    found = sites(of("admin.marking_in_expression", model=compile_sample(text)))
    assert found["rule:ex.bug_reference"] == ["ex:Ticket.reference"]


# ---- policies_affected --------------------------------------------------------------------


def test_policies_affected_carries_the_marking_warnings_after_the_type_ones() -> None:
    codes = [w.code for w in policies_affected(NOTHING, ENTRY, MODEL, action_slot())]
    assert codes == [
        "admin.marking_ungranted",
        "admin.marking_ungranted",
        "admin.marking_blocks_create",
        "admin.marking_in_expression",
        "admin.marking_in_expression",
        "admin.marking_in_expression",
        "admin.marking_in_expression",
    ]
    unknown = installation(HR % "ex:ghost, ex:personal, ex:financial")
    first = next(iter(policies_affected(unknown, ENTRY, MODEL)))
    assert first.code == "admin.policy_unknown_marking"


# ---- the deny-rule guard ------------------------------------------------------------------

OLD_PEOPLE = """\
format: graph_cli.ir/1
module: {id: sample, prefix: ex, version: 1.0.0, summary: Sample.}
markings:
  personal: {summary: Personal data.}
classes:
  Person:
    summary: A person.
    attributes:
      name: {summary: Name., type: string, required: true, role: label}
      email: {summary: E-mail., type: string, markings: [personal]}
"""
NEW_PEOPLE = """\
format: graph_cli.ir/1
module: {id: sample, prefix: ex, version: 2.0.0, summary: Sample.}
markings:
  personal: {summary: Personal data.}
classes:
  Human:
    summary: A person.
    renamed_from: ex:Person
    attributes:
      name: {summary: Name., type: string, required: true, role: label}
      mail: {summary: E-mail., type: string, markings: [personal], renamed_from: email}
"""


def marking_entries() -> tuple[RuleEntry, ...]:
    source = (
        "schema: graph_cli.policy/1\nid: installation\nroles:\n  hr: {description: HR}\n"
        "grants:\n  - {entrypoint: server, principal: {role: hr}, graphs: [example_graph],"
        " level: write, markings: [ex:personal]}\n"
        'no_project: {"*": read}\n'
    )
    compiled = compile_policy(source.encode(), file="i.yaml", graphs=GRAPHS)
    return rule_entries([compiled.document])


def test_marking_resources_are_no_hazard_for_the_deny_rule_guard() -> None:
    """A release that renames the attribute or the class a marking guards leaves the marking
    resource alone: the guard looks at ``graph:<id>/type:`` resources only, and a marking is no
    deny rule (an allow rule that names a renamed term fails closed anyway)."""
    old, new = compile_sample(OLD_PEOPLE), compile_sample(NEW_PEOPLE)
    renames = derive_renames(old, new)
    entries = marking_entries()
    assert any(e.resource.startswith("marking:") for e in entries)
    assert renamed_terms.probes(entries, EXAMPLE, renames, (), new) == ()
    assert renamed_terms.hazards(entries, EXAMPLE, renames, (), new) == ()
    # also as a deny entry, which the compiler never writes: only graph: resources are judged
    forged = RuleEntry("graph", "r", "deny", f"marking:{EXAMPLE}/ex:personal", ("read.get",))
    assert renamed_terms.hazards([forged], EXAMPLE, renames, (), new) == ()
    assert (
        renamed_terms.references(
            [forged, *entries], EXAMPLE, renames, new, functions=None, actions=None
        )
        == []
    )


def test_marking_entries_are_listed_with_their_resource() -> None:
    [entry] = [e for e in marking_entries() if e.rule.endswith(".markings")]
    assert entry.layer == "installation" and entry.effect == "allow"
    assert entry.resource == f"marking:{EXAMPLE}/ex:personal"
    assert entry.actions == ("read.get",)


def test_the_json_of_a_warning_stays_plain() -> None:
    """Details are JSON values: the live view and the envelope print them as they are."""
    for warning in policies_affected(NOTHING, ENTRY, MODEL, action_slot()):
        assert json.loads(json.dumps(dict(warning.details))) == dict(warning.details)


def test_details_are_mappings() -> None:
    warnings = marking_warnings.warnings(NOTHING, ENTRY, MODEL)
    assert warnings and all(isinstance(w.details, Mapping) for w in warnings)
