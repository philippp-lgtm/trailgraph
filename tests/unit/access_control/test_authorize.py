"""authorize() and the built-in policy (contracts B6.2-B6.9, A5)."""

import dataclasses

import pytest
from support.audit_events import maint_actor
from support.fixtures import GOLDENS, assert_golden
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.policy import (
    BUILTIN_POLICY,
    BUILTIN_POLICY_HASH,
    BUILTIN_POLICY_SET,
    BuiltinPolicySource,
)
from graph_cli.kernel.actions import ACTIONS
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.authz import AuthzContext, Obligations
from graph_cli.kernel.canonical import JsonValue, canonical_bytes, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet

GRAPH = "graph:0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
TYPED = f"{GRAPH}/type:inv:Item"
CTX = AuthzContext(op_count=1, has_reason=False, dry_run=False)
APP, MAINT = actor(), maint_actor()
SERVER = dataclasses.replace(
    actor("token:ci"), client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER)
)

# (actor, action, resource) -> (effect, rule, code)
TABLE: list[tuple[ActorContext, str, str, str, str, str | None]] = [
    (APP, "read.get", GRAPH, "allow", "builtin.allow_app", None),
    (APP, "write.create", TYPED, "allow", "builtin.allow_app", None),
    (APP, "ext.notes.tag", GRAPH, "allow", "builtin.allow_app", None),
    (APP, "read.list", "installation", "allow", "builtin.allow_app_list", None),
    (APP, "read.describe", "installation", "allow", "builtin.allow_app_list", None),
    (APP, "read.get", "installation", "deny", "default.deny", "access.denied"),
    (APP, "write.create", "installation", "deny", "default.deny", "access.denied"),
    (APP, "maint.repair", GRAPH, "deny", "ceiling.app_no_maint", "access.ceiling"),
    (APP, "maint.audit_verify", "installation", "deny", "ceiling.app_no_maint", "access.ceiling"),
    (SERVER, "read.get", GRAPH, "deny", "default.deny", "access.denied"),  # B14: not granted
    (SERVER, "maint.repair", GRAPH, "deny", "ceiling.server_no_maint", "access.ceiling"),
    (MAINT, "maint.repair", GRAPH, "allow", "builtin.allow_maint", None),
    (MAINT, "read.get", "installation", "allow", "builtin.allow_maint", None),
    (
        MAINT,
        "write.supersede",
        TYPED,
        "deny",
        "obligation.require_reason",
        "access.reason_required",
    ),
]


@pytest.mark.parametrize(("who", "action", "resource", "effect", "rule", "code"), TABLE)
def test_decision_table(
    who: ActorContext, action: str, resource: str, effect: str, rule: str, code: str | None
) -> None:
    decision = authorize(who, action, resource, CTX, BUILTIN_POLICY_SET)
    assert (decision.effect, decision.rule_id, decision.code) == (effect, rule, code)
    assert (decision.action, decision.resource) == (action, resource)
    assert decision.policy_hash == BUILTIN_POLICY_HASH


def test_no_maint_action_is_reachable_from_the_app() -> None:
    """B6.5 for the whole taxonomy, even under a policy that allows everything; the server
    has the same ceiling (B14)."""
    allow_all = _policy({"id": "x", "effect": "allow", "action": ["*"], "resource": "*"})
    for action in sorted(a for a in ACTIONS if a.startswith("maint.")):
        for policy in (BUILTIN_POLICY_SET, allow_all):
            for who in (APP, SERVER):
                decision = authorize(who, action, "installation", CTX, policy)
                assert decision.code == "access.ceiling", action
    assert authorize(SERVER, "read.get", GRAPH, CTX, allow_all).effect == "allow"


def test_a_role_change_is_maintenance_only() -> None:
    """B6.5, B14.11: ``maint.token_update`` is in the taxonomy, and the ceilings of the app
    and the server hold for it under the built-in policy and under one that allows all; the
    maintainers' ``*`` covers it, so no policy has to be applied for it."""
    assert "maint.token_update" in ACTIONS
    allow_all = _policy({"id": "x", "effect": "allow", "action": ["*"], "resource": "*"})
    for policy in (BUILTIN_POLICY_SET, allow_all):
        for who in (APP, SERVER):
            decision = authorize(who, "maint.token_update", "installation", CTX, policy)
            assert (decision.effect, decision.code) == ("deny", "access.ceiling")
    allowed = authorize(MAINT, "maint.token_update", "installation", CTX, BUILTIN_POLICY_SET)
    assert (allowed.effect, allowed.rule_id) == ("allow", "builtin.allow_maint")


def test_reason_obligation() -> None:
    with_reason = dataclasses.replace(CTX, has_reason=True)
    for action in ("write.supersede", "write.retract"):
        assert authorize(APP, action, TYPED, CTX, BUILTIN_POLICY_SET).code == (
            "access.reason_required"
        )
        decision = authorize(APP, action, TYPED, with_reason, BUILTIN_POLICY_SET)
        assert decision.effect == "allow"
        assert decision.obligations == Obligations(require_reason=True)
    dry_run = dataclasses.replace(CTX, dry_run=True)
    assert authorize(APP, "write.retract", TYPED, dry_run, BUILTIN_POLICY_SET).effect == "deny"
    assert authorize(APP, "write.update", TYPED, CTX, BUILTIN_POLICY_SET).obligations == (
        Obligations()
    )


def test_batch_obligation() -> None:
    policy = _policy(
        {"id": "x", "effect": "allow", "action": ["write.*"], "resource": "graph:*"},
        obligations=[{"action": ["write.create"], "max_batch": 5}],
    )
    big = dataclasses.replace(CTX, op_count=6)
    assert authorize(APP, "write.create", GRAPH, big, policy).code == "access.batch_too_large"
    allowed = authorize(APP, "write.create", GRAPH, CTX, policy)
    assert allowed.obligations == Obligations(max_batch=5)
    assert authorize(APP, "write.update", GRAPH, big, policy).effect == "allow"


def test_principal_matchers() -> None:
    policy = _policy(
        {
            "id": "only_test_1",
            "effect": "allow",
            "principal": {"principal_id": "test:1", "principal_name": "tester"},
            "action": ["read.*"],
            "resource": "*",
        }
    )
    assert authorize(APP, "read.get", GRAPH, CTX, policy).effect == "allow"
    assert authorize(actor("test:2"), "read.get", GRAPH, CTX, policy).effect == "deny"


def test_globs() -> None:
    policy = _policy(
        {
            "id": "typed",
            "effect": "allow",
            "action": ["write.create"],
            "resource": f"{GRAPH}/type:*",
        }
    )
    assert authorize(APP, "write.create", TYPED, CTX, policy).effect == "allow"
    assert authorize(APP, "write.create", GRAPH, CTX, policy).effect == "deny"
    assert authorize(APP, "write.update", TYPED, CTX, policy).effect == "deny"


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("read.teleport", GRAPH),
        ("ext.Notes.tag", GRAPH),
        ("read.get", "graph:not-a-uuid"),
        ("read.get", f"{GRAPH}/type:Item"),
        ("read.get", "installation/x"),
    ],
)
def test_bugs_are_internal_errors(action: str, resource: str) -> None:
    with pytest.raises(GraphCliError) as info:
        authorize(APP, action, resource, CTX, BUILTIN_POLICY_SET)
    assert info.value.code == "internal.error"


@pytest.mark.parametrize(
    "rule",
    [
        {
            "id": "a",
            "effect": "allow",
            "principal": {"agent_id": "x"},
            "action": ["*"],
            "resource": "*",
        },
        {"id": "u", "effect": "allow", "action": ["*"], "resource": "*", "when": "always"},
    ],
)
def test_documents_outside_the_grammar_are_refused(rule: dict[str, JsonValue]) -> None:
    """Asserted-facet allow rules (B6.7) and unknown keys; deny rules are P2 (test_layers)."""
    with pytest.raises(GraphCliError) as info:
        authorize(MAINT, "read.get", GRAPH, CTX, _policy(rule))
    assert info.value.code == "internal.error"


def test_builtin_source_and_hash() -> None:
    source = BuiltinPolicySource()
    graph = GraphId(GRAPH.removeprefix("graph:"))  # type: ignore[arg-type]
    assert source.current(None) == source.current(graph) == BUILTIN_POLICY_SET
    assert hash_value(BUILTIN_POLICY) == BUILTIN_POLICY_HASH
    assert_golden(
        GOLDENS / "access_control" / "builtin_policy.json",
        canonical_bytes({"hash": BUILTIN_POLICY_HASH, "policy": dict(BUILTIN_POLICY)}) + b"\n",
    )


def _policy(*rules: dict[str, JsonValue], obligations: list[JsonValue] | None = None) -> PolicySet:
    document: dict[str, JsonValue] = {
        "schema": "graph_cli.policy/1",
        "id": "test",
        "rules": list(rules),
    }
    if obligations is not None:
        document["obligations"] = obligations
    return PolicySet(hash_value(document), (document,))
