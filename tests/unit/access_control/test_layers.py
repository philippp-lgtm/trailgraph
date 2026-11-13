"""Deny rules, exceptions, asserted matchers, levels and layers (contracts B6.6, B6.7, B6.11).

Hand-written compiled documents; the authored form and its compiler are
tested in ``test_compiler.py``.
"""

import dataclasses

import pytest
from support.audit_events import maint_actor
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.documents import document_problems
from graph_cli.access_control.effective import (
    graph_level,
    project_warning,
    scope_of,
    type_limits,
    write_obligations,
)
from graph_cli.access_control.policy import BUILTIN_POLICY, BUILTIN_POLICY_HASH, policy_set
from graph_cli.kernel.actions import ACCESS_LEVELS, actions_above, level_actions
from graph_cli.kernel.actor import ActorContext, Assurance, Facet
from graph_cli.kernel.authz import AuthzContext, Obligations
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id

G1 = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
G2 = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"
CTX = AuthzContext(op_count=1, has_reason=False, dry_run=False)
APP = actor()


def with_facets(**facets: Facet) -> ActorContext:
    return dataclasses.replace(APP, **facets)  # type: ignore[arg-type]


def asserted(value: str, *, kind: str | None = None) -> Facet:
    return Facet(Assurance.ASSERTED, id=value, type=kind, source="test")


EXPLORE = with_facets(agent=asserted("agent-1", kind="explore"))
IN_PROJECT = with_facets(project=asserted("example_project"))
OTHER_PROJECT = with_facets(project=asserted("another_project"))


def installation(*rules: dict[str, JsonValue], **extra: JsonValue) -> dict[str, JsonValue]:
    grant: dict[str, JsonValue] = {
        "id": "grant.1",
        "effect": "allow",
        "principal": {"entrypoint": "app"},
        "action": list(level_actions("write")),
        "resource": ["graph:*"],
    }
    return {
        "schema": "graph_cli.policy/1",
        "id": "installation",
        "layer": "installation",
        "rules": [grant, *rules],
        "obligations": [{"action": ["write.supersede", "write.retract"], "require_reason": True}],
        **extra,
    }


def graph_layer(
    graph: str, *rules: dict[str, JsonValue], **extra: JsonValue
) -> dict[str, JsonValue]:
    return {
        "schema": "graph_cli.policy/1",
        "id": "graph",
        "layer": "graph",
        "graph": graph,
        "rules": list(rules),
        **extra,
    }


def deny(
    rule_id: str, actions: list[str], resource: JsonValue, **extra: JsonValue
) -> dict[str, JsonValue]:
    return {"id": rule_id, "effect": "deny", "action": actions, "resource": resource, **extra}


# Project scopes as the compiler writes them: example_project writes G1, reads G2;
# no project (or an unknown one) reads everything.
SCOPED = installation(
    deny(
        "scope.example_project.second",
        ["write.*", "ext.*"],
        [f"graph:{G2}*"],
        principal={"project": ["example_project"]},
    ),
    deny(
        "scope.example_project.*",
        ["read.*", "write.*", "ext.*"],
        ["graph:*"],
        principal={"project": ["example_project"]},
        unless=[{"resource": [f"graph:{G1}*", f"graph:{G2}*"]}],
    ),
    deny(
        "no_project.*",
        ["write.*", "ext.*"],
        ["graph:*"],
        unless=[{"principal": {"project": ["example_project"]}}],
    ),
    projects=["example_project"],
)


def decide(
    who: ActorContext, action: str, resource: str, *documents: dict[str, JsonValue]
) -> tuple[str, str, str | None]:
    decision = authorize(who, action, resource, CTX, policy_set(*documents))
    return decision.effect, decision.rule_id, decision.code


def test_builtin_set_is_unchanged() -> None:
    assert policy_set(BUILTIN_POLICY).hash == BUILTIN_POLICY_HASH
    assert policy_set(BUILTIN_POLICY).documents == (BUILTIN_POLICY,)


def test_layered_hash_names_both_documents() -> None:
    layer = graph_layer(G1)
    combined = policy_set(SCOPED, layer)
    assert combined.hash == hash_value(
        {"installation": hash_value(SCOPED), "graph": hash_value(layer)}
    )
    assert combined.documents == (SCOPED, layer)


def test_deny_wins_over_allow() -> None:
    policy = installation(deny("no_retract", ["write.retract"], "graph:*"))
    assert decide(APP, "write.retract", f"graph:{G1}/type:inv:Item", policy) == (
        "deny",
        "no_retract",
        "access.denied",
    )
    assert decide(APP, "write.update", f"graph:{G1}/type:inv:Item", policy)[0] == "allow"


def test_asserted_facets_narrow_in_deny_rules() -> None:
    policy = installation(
        deny("no_explore_writes", ["write.*"], "graph:*", principal={"agent_type": ["explore"]})
    )
    typed = f"graph:{G1}/type:inv:Item"
    assert decide(EXPLORE, "write.create", typed, policy)[1] == "no_explore_writes"
    assert decide(EXPLORE, "read.get", f"graph:{G1}", policy)[0] == "allow"
    assert decide(APP, "write.create", typed, policy)[0] == "allow"  # absent facet: no match


def test_project_scopes_intersect_with_grants() -> None:
    assert decide(IN_PROJECT, "write.create", f"graph:{G1}/type:inv:Item", SCOPED)[0] == "allow"
    assert decide(IN_PROJECT, "read.get", f"graph:{G2}", SCOPED)[0] == "allow"
    assert decide(IN_PROJECT, "write.create", f"graph:{G2}/type:inv:Item", SCOPED)[1] == (
        "scope.example_project.second"
    )
    third = "graph:0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"
    assert decide(IN_PROJECT, "read.list", third, SCOPED)[1] == "scope.example_project.*"


@pytest.mark.parametrize("who", [APP, OTHER_PROJECT], ids=["no_project", "unknown_project"])
def test_no_project_scope_covers_absent_and_unknown_projects(who: ActorContext) -> None:
    assert decide(who, "read.find", f"graph:{G2}", SCOPED)[0] == "allow"
    assert decide(who, "write.create", f"graph:{G1}/type:inv:Item", SCOPED) == (
        "deny",
        "no_project.*",
        "access.denied",
    )


def test_absent_graph_layer_is_neutral_and_present_one_narrows() -> None:
    typed = f"graph:{G1}/type:files:File"
    limits = graph_layer(
        G1, deny("type.files:File", ["write.*"], [typed]), types={"files:File": "read"}
    )
    neutral = graph_layer(G1)
    assert decide(IN_PROJECT, "write.create", typed, SCOPED)[0] == "allow"
    assert decide(IN_PROJECT, "write.create", typed, SCOPED, neutral)[0] == "allow"
    assert decide(IN_PROJECT, "write.create", typed, SCOPED, limits) == (
        "deny",
        "type.files:File",
        "access.denied",
    )
    assert (
        decide(IN_PROJECT, "write.create", f"graph:{G1}/type:inv:Item", SCOPED, limits)[0]
        == "allow"
    )
    assert decide(IN_PROJECT, "read.get", f"graph:{G1}", SCOPED, limits)[0] == "allow"


def test_obligations_of_both_layers_combine() -> None:
    layer = graph_layer(
        G1,
        obligations=[
            {"action": ["write.transition"], "require_reason": True},
            {"action": ["write.*"], "max_batch": 200},
        ],
    )
    policy = policy_set(
        installation(obligations=[{"action": ["write.*"], "max_batch": 500}]), layer
    )
    typed = f"graph:{G1}/type:inv:Item"
    assert authorize(APP, "write.transition", typed, CTX, policy).code == "access.reason_required"
    reasoned = dataclasses.replace(CTX, has_reason=True, op_count=200)
    decision = authorize(APP, "write.transition", typed, reasoned, policy)
    assert decision.obligations == Obligations(require_reason=True, max_batch=200)
    big = dataclasses.replace(CTX, op_count=201)
    assert authorize(APP, "write.create", typed, big, policy).code == "access.batch_too_large"


def test_unless_conditions_need_every_part() -> None:
    policy = installation(
        deny(
            "only_g1_for_project",
            ["read.*"],
            "graph:*",
            unless=[{"principal": {"project": ["example_project"]}, "resource": [f"graph:{G1}*"]}],
        )
    )
    assert decide(IN_PROJECT, "read.get", f"graph:{G1}", policy)[0] == "allow"
    assert decide(IN_PROJECT, "read.get", f"graph:{G2}", policy)[0] == "deny"
    assert decide(APP, "read.get", f"graph:{G1}", policy)[0] == "deny"


def test_ceiling_comes_before_deny_rules() -> None:
    policy = installation(deny("everything", ["*"], "*"))
    assert decide(APP, "maint.repair", f"graph:{G1}", policy)[2] == "access.ceiling"
    assert decide(maint_actor(), "maint.repair", f"graph:{G1}", policy)[1] == "everything"


@pytest.mark.parametrize(
    ("document", "path"),
    [
        (
            installation(
                {
                    "id": "a",
                    "effect": "allow",
                    "principal": {"agent_type": "x"},
                    "action": ["*"],
                    "resource": "*",
                }
            ),
            "rules[1].principal.agent_type",
        ),
        (
            installation(
                {
                    "id": "a",
                    "effect": "allow",
                    "principal": {"colour": "x"},
                    "action": ["*"],
                    "resource": "*",
                }
            ),
            "rules[1].principal.colour",
        ),
        (installation(deny("d", ["read.teleport"], "*")), "rules[1].action"),
        (installation(deny("d", ["read.*"], "graph:example")), "rules[1].resource"),
        (installation(deny("grant.1", ["read.*"], "*")), "rules[1].id"),
        (
            installation(
                {
                    "id": "a",
                    "effect": "allow",
                    "action": ["*"],
                    "resource": "*",
                    "unless": [{"resource": "*"}],
                }
            ),
            "rules[1].unless",
        ),
        (
            graph_layer(G1, {"id": "a", "effect": "allow", "action": ["*"], "resource": "*"}),
            "rules[0].effect",
        ),
        (graph_layer("example_graph"), "graph"),
        (graph_layer(G1, types={"files:File": "hidden"}), "types.files:File"),
        (
            installation(obligations=[{"action": ["write.*"], "max_batch": 0}]),
            "obligations[0].max_batch",
        ),
        (
            installation(obligations=[{"action": ["*"], "require_reason": True}]),
            "obligations[0].action",
        ),
        (
            graph_layer(G1, deny("d", ["read.find"], f"graph:{G1}/type:files:File")),
            "rules[0].action",
        ),
        (
            graph_layer(G1, deny("d", ["read.get"], f"graph:{G1}/type:files:File/attr:title")),
            "rules[0].action",
        ),
        (installation(projects=["not a name"]), "projects"),
        (installation(when="always"), "when"),
    ],
)
def test_documents_outside_the_grammar(document: dict[str, JsonValue], path: str) -> None:
    assert path in [p.path for p in document_problems(document)]
    with pytest.raises(GraphCliError) as info:
        authorize(APP, "read.get", f"graph:{G1}", CTX, policy_set(document))
    assert info.value.code == "internal.error"


def test_the_level_ladder() -> None:
    assert ACCESS_LEVELS == ("none", "read", "write")
    assert level_actions("none") == ()
    assert set(level_actions("read")) < set(level_actions("write"))
    assert actions_above("write") == ()
    assert set(actions_above("none")) >= {"read.*", "write.*", "ext.*"}
    with pytest.raises(GraphCliError):
        level_actions("admin")


def test_effective_levels_scope_and_warning() -> None:
    graph1, graph2 = GraphId(parse_id(G1)), GraphId(parse_id(G2))
    scoped = policy_set(SCOPED)
    assert graph_level(IN_PROJECT, graph1, scoped) == "write"
    assert graph_level(IN_PROJECT, graph2, scoped) == "read"
    third = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"))
    assert graph_level(IN_PROJECT, third, scoped) == "none"
    assert graph_level(APP, graph1, scoped) == "read"
    assert (scope_of(IN_PROJECT, scoped), scope_of(APP, scoped)) == (
        "example_project",
        "no_project",
    )
    assert scope_of(OTHER_PROJECT, scoped) == "no_project"
    assert scope_of(IN_PROJECT, policy_set(BUILTIN_POLICY)) is None
    warning = project_warning(OTHER_PROJECT, scoped)
    assert warning is not None and warning.code == "access.project_unknown"
    assert dict(warning.details) == {"project": "another_project"}
    assert project_warning(IN_PROJECT, scoped) is None
    assert project_warning(APP, scoped) is None
    assert project_warning(OTHER_PROJECT, policy_set(BUILTIN_POLICY)) is None
    assert write_obligations(IN_PROJECT, graph1, scoped) == {
        "write.retract": {"require_reason": True},
        "write.supersede": {"require_reason": True},
    }
    assert write_obligations(IN_PROJECT, graph2, scoped) == {}
    layer = graph_layer(G1, types={"files:File": "read"})
    assert type_limits(policy_set(SCOPED, layer)) == {"files:File": "read"}
