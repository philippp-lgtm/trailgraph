"""The three-way split of functions, actions and views for a caller: ``allowed``,
``not_allowed`` with the rights it ``missing``, or ``hidden`` (graph start plan S4, step 1).

Over the company example (memory backend) with its published actions, functions and views, and
the ``staff`` toy for markings. Policies are generated from YAML: deny rules on ``read.function``
and ``read.find``, read-only and hidden types of the graph layer, write allow lists and
obligations.

The property tests are exhaustive over the policy matrix (no ``hypothesis`` dependency): a
caller's read limits decide what is hidden and nothing else does, so the names present in a
classification never depend on the caller's rights; what is hidden never occurs in any output
(``support.catalog_leaks``); classifying records no decision; obligations never make an entry
not allowed.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support import markings as marked
from support import views as view_fixtures
from support.actions import publish_actions, valid_sources
from support.catalog_leaks import assert_hidden_absent
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.ports.catalog import (
    FunctionsSlot,
    KineticSlot,
    functions_hash,
    sources_hash,
)
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.services.action_rights import NeedResult
from graph_cli.services.catalog_access import Classified, Missing, allowed_entries
from graph_cli.services.read import ReadService

GRAPH = "example_graph"
KINDS = ("functions", "actions", "views")

# Fixed rows of the company example: what the toy catalogs hold.
FUNCTIONS = {
    "employer_of",
    "members_of_unit",
    "missing_required_trainings",
    "policies_for_unit",
    "reporting_chain",
    "training_completions",
    "vacant_positions",
}
ACTIONS = {"assign_position", "create_task", "hand_over_position", "offboard_employee"}
VIEWS = {
    "legacy_unit_overview",
    "members_report",
    "org_catalog",
    "position_overview",
    "tasks_by_state",
    "unit_overview",
}
EVERYTHING = {"functions": FUNCTIONS, "actions": ACTIONS, "views": VIEWS}
FUNCTION_VIEWS = {"legacy_unit_overview", "members_report", "org_catalog", "unit_overview"}
SET_VIEWS = {"members_report", "org_catalog", "tasks_by_state"}


class Fixed:
    """A policy source with one fixed policy set."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


@dataclass(frozen=True)
class Policy:
    """A caller's policy, as the matrix varies it."""

    grant: str = "level: write"
    deny: tuple[str, ...] = ()  # read.function, read.find
    types: tuple[tuple[str, str], ...] = ()  # graph layer: type -> read | none
    obligations: bool = False

    def compiled(self, env: ServiceEnv) -> PolicySet:
        graphs = {GRAPH: env.entry.id}
        deny = "".join(f"  - {{id: no_{a.replace('.', '_')}, action: [{a}]}}\n" for a in self.deny)
        installation = (
            "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
            f"  - {{entrypoint: app, graphs: [{GRAPH}], {self.grant}}}\n"
            'no_project: {"*": write}\n' + (f"deny:\n{deny}" if deny else "")
        )
        layers = [compile_policy(installation.encode(), file="i.yaml", graphs=graphs).document]
        if self.types or self.obligations:
            graph = "schema: graph_cli.policy/1\nid: graph\n"
            if self.types:
                graph += "types:\n" + "".join(f'  "{t}": {m}\n' for t, m in self.types)
            if self.obligations:
                graph += (
                    "obligations:\n  - {action: [write.*], require_reason: true, max_batch: 1}\n"
                )
            layers.append(
                compile_policy(
                    graph.encode(),
                    file="g.yaml",
                    graphs=graphs,
                    graph=env.entry.id,
                    roles=frozenset(),
                ).document
            )
        return policy_set(*layers)  # type: ignore[arg-type]


def publish_functions(env: ServiceEnv, sources: dict[str, str]) -> None:
    compiled, problems = compile_catalog(sources, env.model)
    assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    env.catalog.functions[env.entry.id] = FunctionsSlot(  # type: ignore[union-attr]
        functions_hash(files), hash_value(compiled), env.entry.pin, compiled, {}
    )


def publish_views(env: ServiceEnv) -> None:
    function_entries = view_fixtures.function_entries(env.model)
    sources = view_fixtures.valid_sources()
    compiled, problems = compile_views(sources, env.model, function_entries)
    assert [p.to_json() for p in problems] == []
    files = {name: text.encode() for name, text in sources.items()}
    env.catalog.views[env.entry.id] = KineticSlot(  # type: ignore[union-attr]
        sources_hash(files), hash_value(compiled), env.entry.pin, compiled, {}, kind="views"
    )


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    found = ServiceEnv("memory", "company", tmp_path / "root", example_version="1.1.0")
    publish_functions(found, view_fixtures.function_sources())
    publish_actions(found, valid_sources())
    publish_views(found)
    return found


def reader(env: ServiceEnv, policy: Policy | None = None) -> ReadService:
    return env.make_reader(None if policy is None else Fixed(policy.compiled(env)))


def classified(env: ServiceEnv, read: ReadService) -> dict[str, tuple[Classified[Any], ...]]:
    actions = read.published_actions(env.entry)
    assert actions is not None
    return {
        "functions": read.classified_functions(env.actor, env.entry),
        "actions": read.classified_actions(env.actor, env.entry, actions),
        "views": read.classified_views(env.actor, env.entry),
    }


def verdicts(found: Sequence[Classified[Any]]) -> dict[str, tuple[str, ...]]:
    """name -> the unmet rights as text (empty: allowed)."""
    return {c.entry.name: tuple(str(m) for m in c.missing) for c in found}


def not_allowed(found: Sequence[Classified[Any]]) -> dict[str, tuple[str, ...]]:
    return {n: m for n, m in verdicts(found).items() if m}


# ---- a caller with every right -----------------------------------------------------------


def test_a_caller_with_every_right_may_use_everything(env: ServiceEnv) -> None:
    found = classified(env, reader(env))
    for kind, names in EVERYTHING.items():
        assert set(verdicts(found[kind])) == names
        assert not_allowed(found[kind]) == {}
        assert all(c.allowed for c in found[kind])


def test_the_allowed_half_is_the_listing(env: ServiceEnv) -> None:
    """``functions``, ``actions`` and ``views`` list exactly what is allowed, for every
    policy of the matrix: one implementation of what a caller may use."""
    for policy in MATRIX:
        read = reader(env, policy)
        found = classified(env, read)
        assert [e.name for e in read.functions(env.actor, env.entry) or ()] == [
            e.name for e in allowed_entries(found["functions"])
        ]
        assert [e.name for e in read.actions(env.actor, env.entry) or ()] == [
            e.name for e in allowed_entries(found["actions"])
        ]
        assert [e.name for e in read.views(env.actor, env.entry) or ()] == [
            e.name for e in allowed_entries(found["views"])
        ]


# ---- functions and views: read.function and read.find ------------------------------------

RF = "read.function on graph"
RFIND = "read.find on graph"


def test_without_read_function_every_function_is_not_allowed(env: ServiceEnv) -> None:
    found = classified(env, reader(env, Policy(deny=("read.function",))))
    assert not_allowed(found["functions"]) == {name: (RF,) for name in FUNCTIONS}
    # a view that runs a function is not allowed with it; the others are untouched
    assert not_allowed(found["views"]) == {name: (RF,) for name in FUNCTION_VIEWS}
    assert not_allowed(found["actions"]) == {}


def test_without_read_find_the_set_views_are_not_allowed(env: ServiceEnv) -> None:
    found = classified(env, reader(env, Policy(deny=("read.find",))))
    assert not_allowed(found["views"]) == {name: (RFIND,) for name in SET_VIEWS}
    assert not_allowed(found["functions"]) == not_allowed(found["actions"]) == {}


def test_a_set_view_that_runs_a_function_names_both_rights_in_a_fixed_order(
    env: ServiceEnv,
) -> None:
    found = classified(env, reader(env, Policy(deny=("read.function", "read.find"))))
    assert not_allowed(found["views"]) == {
        "legacy_unit_overview": (RF,),
        "members_report": (RFIND, RF),
        "org_catalog": (RFIND, RF),
        "tasks_by_state": (RFIND,),
        "unit_overview": (RF,),
    }
    assert verdicts(found["views"])["position_overview"] == ()  # an entity view, no function


# ---- actions: write rights ---------------------------------------------------------------


def test_a_read_only_type_makes_the_action_that_writes_it_not_allowed(env: ServiceEnv) -> None:
    found = classified(env, reader(env, Policy(types=(("ops:Task", "read"),))))
    assert not_allowed(found["actions"]) == {"create_task": ("write.create on type:ops:Task",)}
    assert set(verdicts(found["actions"])) == ACTIONS
    # the type is still readable: the set view over it stays allowed
    assert verdicts(found["views"])["tasks_by_state"] == ()


EMPLOYEE_LINKS = "level: read, writes: {ppl:Employee: {link: [leg:employed_by]}}"


def test_a_write_allow_list_names_the_first_denied_right_of_each_unmet_need(
    env: ServiceEnv,
) -> None:
    found = classified(env, reader(env, Policy(grant=EMPLOYEE_LINKS)))
    assert not_allowed(found["actions"]) == {
        # the type is granted `link`, but not the relation the action writes
        "assign_position": ("write.link on type:ppl:Employee/rel:org:holds",),
        # link and unlink are two needs; both unmet, one entry each
        "hand_over_position": (
            "write.link on type:ppl:Employee/rel:org:holds",
            "write.unlink on type:ppl:Employee",
        ),
        "create_task": ("write.create on type:ops:Task",),
        "offboard_employee": (
            "write.transition on type:ppl:Employee",
            "write.unlink on type:ppl:Employee",
        ),
    }


def test_a_write_allow_list_that_covers_the_terms_allows_the_action(env: ServiceEnv) -> None:
    grant = "level: read, writes: {ppl:Employee: {link: [org:holds]}}"
    found = classified(env, reader(env, Policy(grant=grant)))
    assert verdicts(found["actions"])["assign_position"] == ()
    assert verdicts(found["actions"])["hand_over_position"] != ()  # also unlinks


def test_a_reader_without_write_rights_sees_every_action_not_allowed(env: ServiceEnv) -> None:
    found = classified(env, reader(env, Policy(grant="level: read")))
    assert set(not_allowed(found["actions"])) == ACTIONS
    for rights in not_allowed(found["actions"]).values():
        assert rights and all(r.startswith("write.") for r in rights)


def test_obligations_never_make_an_action_not_allowed(env: ServiceEnv) -> None:
    """``require_reason`` and ``max_batch`` are met at the call; an action that needs ``--reason``
    or writes more than one op in a batch of one stays allowed."""
    found = classified(env, reader(env, Policy(obligations=True)))
    assert not_allowed(found["actions"]) == {}
    assert set(verdicts(found["actions"])) == ACTIONS
    plain = classified(env, reader(env, Policy()))
    assert verdicts(found["actions"]) == verdicts(plain["actions"])


# ---- hidden is not not_allowed -----------------------------------------------------------


def test_what_the_read_view_hides_is_absent_not_not_allowed(env: ServiceEnv) -> None:
    # Task is hidden: its action and view are not in any classification, whatever the rights
    for deny in ((), ("read.function", "read.find")):
        found = classified(
            env, reader(env, Policy(deny=deny, grant="level: read", types=(("ops:Task", "none"),)))
        )
        assert "create_task" not in verdicts(found["actions"])
        assert "tasks_by_state" not in verdicts(found["views"])
        assert set(verdicts(found["actions"])) == ACTIONS - {"create_task"}
        assert set(verdicts(found["views"])) == VIEWS - {"tasks_by_state"}


def test_a_view_over_a_function_the_reader_does_not_see_is_hidden_not_not_allowed(
    env: ServiceEnv,
) -> None:
    # `members_of_unit` touches org:Position, which this reader does not see; its views follow
    hide = (("org:Position", "none"),)
    full = classified(env, reader(env, Policy(types=hide)))
    assert "members_of_unit" not in verdicts(full["functions"])
    no_right = classified(env, reader(env, Policy(types=hide, deny=("read.function",))))
    assert set(verdicts(no_right["views"])) == set(verdicts(full["views"]))
    assert not set(verdicts(no_right["views"])) & {"members_report", "unit_overview"}
    assert set(verdicts(no_right["functions"])) == set(verdicts(full["functions"]))


# ---- the property: hidden depends on the read view alone ---------------------------------

LIMITS: tuple[tuple[tuple[str, str], ...], ...] = (
    (),
    (("ops:Task", "none"),),
    (("org:Position", "none"),),
    (("org:Department", "none"),),
    (("ppl:Employee", "none"), ("ppl:External", "none")),
    (("ops:Training", "none"), ("leg:LegalEntity", "none")),
)
RIGHTS: tuple[dict[str, Any], ...] = (
    {},
    {"deny": ("read.function",)},
    {"deny": ("read.find",)},
    {"deny": ("read.function", "read.find")},
    {"grant": "level: read"},
    {"grant": EMPLOYEE_LINKS},
    {"obligations": True},
    {"grant": "level: read", "deny": ("read.function", "read.find"), "obligations": True},
)
MATRIX = tuple(Policy(types=limit, **rights) for limit, rights in itertools.product(LIMITS, RIGHTS))


def test_the_names_present_depend_on_the_read_view_alone(env: ServiceEnv) -> None:
    for limit in LIMITS:
        oracle = {
            k: set(verdicts(v))
            for k, v in classified(env, reader(env, Policy(types=limit))).items()
        }
        for rights in RIGHTS:
            found = classified(env, reader(env, Policy(types=limit, **rights)))
            assert {k: set(verdicts(v)) for k, v in found.items()} == oracle, (limit, rights)


def test_a_hidden_name_occurs_in_no_output(env: ServiceEnv) -> None:
    hidden_somewhere = 0
    for policy in MATRIX:
        found = classified(env, reader(env, policy))
        for kind, names in EVERYTHING.items():
            hidden = names - set(verdicts(found[kind]))
            hidden_somewhere += len(hidden)
            output = [
                (c.entry.detail_json(), c.entry.summary_json(), c.missing_json())
                for c in found[kind]
            ]
            output += [str(m) for c in found[kind] for m in c.missing]
            # the entries a reader sees may name each other (a view runs a function it sees);
            # a hidden entry is named by none of them
            assert_hidden_absent(output, hidden)
    assert hidden_somewhere > 0  # the matrix hides something, or the check proves nothing


def test_an_unmet_need_without_a_decision_is_hidden(
    env: ServiceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A need no option of which the reader sees has no decision, so it has no right to name:
    the action is hidden, never not allowed with an empty ``missing``."""
    monkeypatch.setattr(
        "graph_cli.services.read.catalogs.need_decisions",
        lambda *args, **kwargs: NeedResult((), False, None),
    )
    found = classified(env, reader(env))
    assert verdicts(found["actions"]) == {}


def test_classifying_records_no_decision(env: ServiceEnv) -> None:
    before = len(env.installation_events()), len(env.graph_events())
    for policy in MATRIX:
        classified(env, reader(env, policy))
    assert (len(env.installation_events()), len(env.graph_events())) == before


def test_a_missing_right_is_a_public_right_on_a_resource_the_reader_sees(env: ServiceEnv) -> None:
    for policy in MATRIX:
        found = classified(env, reader(env, policy))
        hidden_types = {t for t, mode in policy.types if mode == "none"}
        for kind in KINDS:
            for c in found[kind]:
                for m in c.missing:
                    assert isinstance(m, Missing)
                    assert m.action.startswith(("read.", "write."))
                    assert not m.resource.startswith("graph:")  # graph-relative
                    for t in hidden_types:
                        assert t not in m.resource, (policy, m)


# ---- markings ---------------------------------------------------------------------------


@pytest.fixture
def staff(tmp_path: Path) -> ServiceEnv:
    found = ServiceEnv("memory", "staff", tmp_path / "root")
    publish_functions(found, marked.FUNCTION_SOURCES)
    publish_actions(found, marked.ACTION_SOURCES)
    return found


def staff_reader(env: ServiceEnv, grant: str) -> ReadService:
    installation = (
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        f"  - {{entrypoint: app, graphs: [{GRAPH}], {grant}}}\n"
        'no_project: {"*": write}\n'
    )
    document = compile_policy(
        installation.encode(), file="i.yaml", graphs={GRAPH: env.entry.id}
    ).document
    return env.make_reader(Fixed(policy_set(document)))  # type: ignore[arg-type]


def test_an_action_on_a_marked_attribute_is_hidden_from_a_reader_without_the_marking(
    staff: ServiceEnv,
) -> None:
    every = {"set_email", "set_grade", "set_phone", "set_phone_badged", "set_salary"}
    holder = classified(
        staff, staff_reader(staff, "level: write, markings: [stf:personal, stf:pay]")
    )
    assert set(verdicts(holder["actions"])) == every
    assert not_allowed(holder["actions"]) == {}
    # no marking: the writes it cannot see are not there; the rest follows its rights
    plain = classified(staff, staff_reader(staff, "level: write"))
    assert set(verdicts(plain["actions"])) == {"set_phone"}
    assert not_allowed(plain["actions"]) == {}


def test_hidden_by_a_marking_stays_hidden_when_the_rights_are_missing_too(
    staff: ServiceEnv,
) -> None:
    holder = classified(
        staff, staff_reader(staff, "level: read, markings: [stf:personal, stf:pay]")
    )
    assert set(not_allowed(holder["actions"])) == {
        "set_email",
        "set_grade",
        "set_phone",
        "set_phone_badged",
        "set_salary",
    }
    plain = classified(staff, staff_reader(staff, "level: read"))
    # set_phone is the only action a reader without markings sees, and it lacks the right
    assert set(verdicts(plain["actions"])) == {"set_phone"}
    assert set(not_allowed(plain["actions"])) == {"set_phone"}
    assert_hidden_absent(
        [(c.entry.detail_json(), c.missing_json()) for c in plain["actions"]],
        {"set_email", "set_salary", "set_grade", "set_phone_badged"},
    )
