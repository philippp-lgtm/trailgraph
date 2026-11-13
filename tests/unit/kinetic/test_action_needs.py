"""``action_needs``: what an action's ops need on concrete types, with the attributes and
relations they write (write allow lists plan W9). Pure over a compiled catalog and a model."""

from dataclasses import replace

from support.actions import company_model, valid_sources

from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.action_needs import NeedOptions, action_needs
from graph_cli.ontology_layer.model import CompiledModel

MODEL = company_model()


def entries(
    sources: dict[str, str] | None = None, model: CompiledModel = MODEL
) -> dict[str, ActionEntry]:
    document, problems = compile_actions(sources or valid_sources(), model)
    assert [p.to_json() for p in problems] == []
    return {e.name: e for e in load_action_entries(document)}


ACTIONS = entries()


def needs(name: str, model: CompiledModel = MODEL) -> tuple[NeedOptions, ...]:
    return action_needs(ACTIONS[name], model)


def source(name: str, ops: str, params: str = "") -> dict[str, str]:
    text = f"""\
schema: graph_cli.action/1
name: {name}
module: organization
summary: A test action.
use_when: Only in a test.
params:
{params or "  {}"}
ops:
{ops}
examples:
  - request: Run the test action.
"""
    return {f"{name}.yaml": text}


EMPLOYEE_ONLY = """\
  employee:
    type: ref
    class: ppl:Employee
    summary: The employee.
"""


def test_a_need_on_an_abstract_class_has_its_concrete_subtypes_as_options() -> None:
    holds = (("write.link", "rel", "org:holds"),)
    assert needs("assign_position") == (
        NeedOptions(
            "write.link",
            "ppl:Person",
            (("ppl:Employee", holds), ("ppl:External", holds)),
        ),
    )


def test_a_create_gathers_its_attributes_and_inline_edges_on_the_created_type() -> None:
    (need,) = needs("create_task")
    assert need.action == "write.create" and need.type == "ops:Task"
    assert need.options == (
        (
            "ops:Task",
            (
                ("write.create", "attr", "due_on"),  # an optional parameter's entry counts
                ("write.create", "attr", "title"),
                ("write.create", "rel", "ops:assigned_to"),  # an inline edge, decided as create
            ),
        ),
    )


def test_a_dynamic_unlink_in_needs_the_domain_of_the_relation() -> None:
    unlink, link = sorted(needs("hand_over_position"), key=lambda n: n.action, reverse=True)
    assert (unlink.action, unlink.type) == ("write.unlink", "ppl:Person")
    assert [o[0] for o in unlink.options] == ["ppl:Employee", "ppl:External"]
    assert all(o[1] == (("write.unlink", "rel", "org:holds"),) for o in unlink.options)
    assert (link.action, link.type) == ("write.link", "ppl:Person")


def test_ops_on_one_declared_class_union_their_terms_and_a_transition_finds_its_lifecycle() -> None:
    found = {(n.action, n.type): n for n in needs("offboard_employee")}
    unlink = found[("write.unlink", "ppl:Employee")]
    assert unlink.options == (
        (
            "ppl:Employee",
            (("write.unlink", "rel", "leg:employed_by"), ("write.unlink", "rel", "org:holds")),
        ),
    )
    transition = found[("write.transition", "ppl:Employee")]
    assert transition.options == (("ppl:Employee", (("write.transition", "attr", "status"),)),)


def test_a_transition_attr_given_in_the_template_is_the_term() -> None:
    sources = source(
        "finish",
        "  - {op: transition, id: $employee, to: left, attr: status}",
        EMPLOYEE_ONLY,
    )
    (need,) = action_needs(entries(sources)["finish"], MODEL)
    assert need.options == (("ppl:Employee", (("write.transition", "attr", "status"),)),)


def test_a_transition_without_a_single_lifecycle_attribute_has_no_term() -> None:
    employee = MODEL.classes["ppl:Employee"]
    twice = replace(
        employee, attributes={**employee.attributes, "phase": employee.attributes["status"]}
    )
    model = replace(MODEL, classes={**MODEL.classes, "ppl:Employee": twice})
    assert model.lifecycle_attribute("ppl:Employee") is None  # several
    assert MODEL.lifecycle_attribute("ppl:Person") is None  # none
    assert MODEL.lifecycle_attribute("ppl:Employee") == "status"
    assert MODEL.lifecycle_attribute("no:Such") is None
    transition = next(
        n for n in needs("offboard_employee", model) if n.action == "write.transition"
    )
    assert transition.options == (("ppl:Employee", ()),)


def test_a_local_subject_is_exactly_its_create_type() -> None:
    sources = source(
        "open_task",
        "  - {op: create, type: ops:Task, local: task, attrs: {title: $title}}\n"
        "  - {op: update, id: '@task', set: {due_on: $due}}\n"
        "  - {op: retract, id: $person}",
        "  title: {type: string, summary: A title.}\n"
        "  due: {type: date, summary: A day.}\n"
        "  person: {type: ref, class: ppl:Person, summary: A person.}\n",
    )
    found = {
        (n.action, n.type): n.options for n in action_needs(entries(sources)["open_task"], MODEL)
    }
    assert found[("write.update", "ops:Task")] == (
        ("ops:Task", (("write.update", "attr", "due_on"),)),
    )
    assert found[("write.create", "ops:Task")] == (
        ("ops:Task", (("write.create", "attr", "title"),)),
    )
    assert found[("write.retract", "ppl:Person")] == (("ppl:Employee", ()), ("ppl:External", ()))


def test_a_dynamic_unlink_in_on_a_relation_without_a_domain_needs_every_concrete_class() -> None:
    """The published needs have none for it (the gap); the derived need has every concrete
    class of the model as an option, any of which meets it."""
    relation = replace(MODEL.relations["org:holds"], domain=())
    model = replace(MODEL, relations={**MODEL.relations, "org:holds": relation})
    sources = source(
        "release",
        "  - {op: unlink, id: $position, rel: org:holds, direction: in, all: true}",
        "  position: {type: ref, class: org:Position, summary: A position.}\n",
    )
    entry = entries(sources, model)["release"]
    assert entry.needs == ()
    (need,) = action_needs(entry, model)
    assert (need.action, need.type) == ("write.unlink", None)
    concrete = sorted(c for c, d in model.classes.items() if not d.abstract)
    assert [o[0] for o in need.options] == concrete
    assert all(o[1] == (("write.unlink", "rel", "org:holds"),) for o in need.options)
    assert "ppl:Person" not in {o[0] for o in need.options}  # abstract classes are no option


def test_a_class_the_model_lacks_gives_no_need_and_no_error() -> None:
    classes = {k: v for k, v in MODEL.classes.items() if not k.startswith("ppl:")}
    model = replace(MODEL, classes=classes)
    assert needs("assign_position", model) == ()
    assert [n.type for n in needs("create_task", model)] == ["ops:Task"]


def test_every_declared_class_of_the_company_actions_has_concrete_options() -> None:
    for name in ACTIONS:
        for need in needs(name):
            assert need.options, name
