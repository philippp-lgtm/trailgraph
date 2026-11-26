"""``authorize_entry``: the deny-only decision for what a granted catalog entry needs
(catalog callers plan CA5).

An entry decision (``authorize`` on ``graph:<id>/<kind>:<name>``) lets a caller run the entry;
what the entry reads or writes is then decided by ``authorize_entry``: the ceiling and the first
matching deny rule of every layer, then the obligations, and never an allow rule. The tests pin
that it never allows what a deny rule, a scope or a ceiling refuses, and that it allows what
only the default would refuse. The mutation checks (remove the deny loop, remove the ceiling
check: these tests fail) are recorded in the implementation notes of the change.
"""

import dataclasses

import pytest
from support.audit_events import maint_actor
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize, authorize_entry
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext, Obligations
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.policy import PolicySet

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
GRAPH = f"graph:{EXAMPLE}"
TICKET = f"{GRAPH}/type:ex:Ticket"
CUSTOMER = f"{GRAPH}/type:ex:Customer"
STATUS = f"{TICKET}/attr:status"
ACTION = f"{GRAPH}/action:close_ticket"
GRANTED = "grant.1.actions"
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
NO_REASON = AuthzContext(op_count=1, has_reason=False, dry_run=False)

INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  support: {description: "Support agents: close tickets through an action"}
  auditor: {description: "Auditors"}
grants:
  - entrypoint: server
    principal: {role: support}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [open_tickets], actions: [close_ticket]}
    reads: ["ex:Ticket", "ex:Customer"]
scopes:
  frozen: {example_graph: none, "*": write}
  readonly: {example_graph: read, "*": write}
  open: {example_graph: write, "*": write}
no_project: {"*": write}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
  - {id: auditor_no_retract, role: [auditor], action: ["write.retract"], graph: [example_graph]}
  - {id: no_device_find, device_id: dev-1, action: ["read.find"]}
obligations:
  - {action: ["write.transition"], require_reason: true}
  - {action: ["write.*"], max_batch: 3}
"""
GRAPH_LAYER = b"""\
schema: graph_cli.policy/1
id: graph
types:
  "ex:Customer": read
  "ex:Secret": none
deny:
  - {id: no_status_update, role: [support], action: ["write.update"], type: ["ex:Ticket"],
     attribute: [status]}
"""


def installed(*, layer: bool = True) -> PolicySet:
    installation = compile_policy(INSTALLATION, file="i.yaml", graphs=GRAPHS).document
    if not layer:
        return policy_set(installation)
    graph = compile_policy(
        GRAPH_LAYER, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset({"support"})
    ).document
    return policy_set(installation, graph)


def token(
    *roles: str,
    project: str | None = None,
    agent_type: str | None = None,
    device: str | None = None,
) -> ActorContext:
    base = actor()
    return dataclasses.replace(
        base,
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=Facet(Assurance.VERIFIED, id=project, source="token") if project else base.project,
        agent=(
            Facet(Assurance.ASSERTED, id="a1", type=agent_type, source="client")
            if agent_type
            else base.agent
        ),
        device=Facet(Assurance.ASSERTED, id=device, source="client") if device else base.device,
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


SUPPORT = token("support")


def entry(
    who: ActorContext,
    action: str,
    resource: str,
    *,
    ctx: AuthzContext = CTX,
    policy: PolicySet | None = None,
) -> tuple[str, str, str | None]:
    decision = authorize_entry(who, action, resource, ctx, policy or installed(), GRANTED)
    return decision.effect, decision.rule_id, decision.code


# ---- it never looks up an allow rule ---------------------------------------------------


def test_what_no_rule_allows_is_allowed_to_the_entry_and_names_the_granting_rule() -> None:
    """The caller's grants do not name a write of a type: ``authorize`` refuses it by default,
    and the entry decision (its action was granted by ``grant.1.actions``) allows it."""
    assert authorize(SUPPORT, "write.update", TICKET, CTX, installed()).rule_id == "default.deny"
    decision = authorize_entry(SUPPORT, "write.update", TICKET, CTX, installed(), GRANTED)
    assert decision.effect == "allow"
    assert (decision.action, decision.resource) == ("write.update", TICKET)
    assert decision.rule_id == GRANTED
    assert decision.policy_hash == installed().hash
    assert decision.code is None
    assert decision.obligations == Obligations(max_batch=3)
    assert decision.to_json()["rule_id"] == GRANTED
    # reads of the graph itself, of a type, with the actions the caller could not use by hand
    assert entry(SUPPORT, "read.get", GRAPH)[0] == "allow"
    assert entry(SUPPORT, "read.find", GRAPH)[0] == "allow"
    assert entry(SUPPORT, "read.get", CUSTOMER)[0] == "allow"
    assert entry(SUPPORT, "write.create", STATUS)[0] == "allow"
    assert entry(SUPPORT, "write.link", f"{TICKET}/rel:ex:about")[0] == "allow"


def test_the_decision_names_the_rule_the_caller_passes() -> None:
    for rule in ("grant.1.functions", "grant.7.actions", "grant.2.views"):
        decision = authorize_entry(SUPPORT, "read.get", GRAPH, CTX, installed(), rule)
        assert decision.rule_id == rule


def test_the_decision_is_pure_and_repeats() -> None:
    first = authorize_entry(SUPPORT, "write.update", TICKET, CTX, installed(), GRANTED)
    assert authorize_entry(SUPPORT, "write.update", TICKET, CTX, installed(), GRANTED) == first


# ---- a deny rule of any layer wins ----------------------------------------------------


DENIED = [
    # (who, action, resource, rule): the first matching deny rule, of either layer
    # graph layer: a type limit (read: no writes), a hidden type (none), a term rule
    (SUPPORT, "write.update", CUSTOMER, "type.ex:Customer"),
    (SUPPORT, "write.create", CUSTOMER, "type.ex:Customer"),
    (SUPPORT, "read.get", f"{GRAPH}/type:ex:Secret", "type.ex:Secret"),
    (SUPPORT, "write.update", STATUS, "deny.no_status_update"),
    # installation: deny rules by agent type, role and device
    (token("support", agent_type="explore"), "write.update", TICKET, "deny.no_explore_writes"),
    (token("support", agent_type="explore"), "write.create", TICKET, "deny.no_explore_writes"),
    (token("support", "auditor"), "write.retract", TICKET, "deny.auditor_no_retract"),
    (token("support", device="dev-1"), "read.find", GRAPH, "deny.no_device_find"),
    # scopes: none takes every read and write, read takes the writes
    (token("support", project="frozen"), "read.get", GRAPH, "scope.frozen.example_graph"),
    (token("support", project="frozen"), "read.get", TICKET, "scope.frozen.example_graph"),
    (token("support", project="frozen"), "write.update", TICKET, "scope.frozen.example_graph"),
    (token("support", project="readonly"), "write.update", TICKET, "scope.readonly.example_graph"),
    (token("support", project="readonly"), "write.create", STATUS, "scope.readonly.example_graph"),
    (token("support", project="readonly"), "write.action", ACTION, "scope.readonly.example_graph"),
]


@pytest.mark.parametrize(("who", "action", "resource", "rule"), DENIED)
def test_a_deny_rule_a_scope_or_a_type_limit_refuses_the_entry_step(
    who: ActorContext, action: str, resource: str, rule: str
) -> None:
    assert entry(who, action, resource) == ("deny", rule, "access.denied")
    # and the allow lookup of `authorize` agrees that something denies it
    assert authorize(who, action, resource, CTX, installed()).effect == "deny"


def test_the_first_matching_rule_names_the_denial() -> None:
    """Rules are tried layer by layer in document order: the installation layer comes first."""
    who = token("support", agent_type="explore")
    # `no_explore_writes` (installation) before `no_status_update` (graph layer)
    assert entry(who, "write.update", STATUS)[1] == "deny.no_explore_writes"
    assert entry(SUPPORT, "write.update", STATUS)[1] == "deny.no_status_update"


def test_a_scope_of_another_project_or_graph_does_not_deny() -> None:
    assert entry(token("support", project="open"), "write.update", TICKET)[0] == "allow"
    other = f"graph:{OTHER}"
    assert entry(token("support", project="frozen"), "read.get", other)[0] == "allow"
    assert (
        entry(token("support", project="readonly"), "write.update", f"{other}/type:ex:Ticket")[0]
        == "allow"
    )


def test_a_deny_with_exceptions_holds_where_no_exception_matches() -> None:
    """``unless`` conditions lift a deny exactly as for ``authorize``: the scopes of the
    ``no_project`` entry carry the project names as an exception."""
    tail = 'scopes:\n  readonly: {example_graph: read}\nno_project: {"*": read}\n'
    document = compile_policy(
        INSTALLATION.split(b"scopes:")[0] + tail.encode(), file="i.yaml", graphs=GRAPHS
    ).document
    policy = policy_set(document)
    # no project: the `no_project` scope (read) denies writes
    assert entry(SUPPORT, "write.update", TICKET, policy=policy) == (
        "deny",
        "no_project.*",
        "access.denied",
    )
    # a project the scopes know is judged by its own scope, not by `no_project`
    named = token("support", project="readonly")
    assert entry(named, "write.update", TICKET, policy=policy)[1] == "scope.readonly.example_graph"


# ---- the ceiling -----------------------------------------------------------------------


def test_no_maintenance_action_is_reachable_through_an_entry() -> None:
    """The ceilings of the app and the server hold whatever an entry granted."""
    server = token("support")
    app = actor()
    for who, rule in ((server, "ceiling.server_no_maint"), (app, "ceiling.app_no_maint")):
        for action in ("maint.audit_verify", "maint.graph_configure", "maint.repair"):
            assert entry(who, action, "installation") == ("deny", rule, "access.ceiling")
            assert entry(who, action, GRAPH) == ("deny", rule, "access.ceiling")


def test_a_project_admin_keeps_its_ceiling() -> None:
    admin = dataclasses.replace(
        maint_actor(), project=Facet(Assurance.VERIFIED, id="open", source="token")
    )
    # graph-level maintenance of a graph and read.get are the whole of its rights
    assert entry(admin, "maint.reindex", GRAPH)[0] == "allow"
    assert entry(admin, "read.get", GRAPH)[0] == "allow"
    for action, resource in (
        ("write.update", TICKET),
        ("read.function", f"{GRAPH}/function:open_tickets"),
        ("maint.token_create", "installation"),
        ("maint.reindex", "installation"),
        ("read.get", "installation"),
    ):
        assert entry(admin, action, resource) == (
            "deny",
            "ceiling.project_maint",
            "access.ceiling",
        ), (
            action,
            resource,
        )


# ---- obligations -----------------------------------------------------------------------


def test_obligations_hold_inside_an_entry() -> None:
    # `write.transition` needs a reason; `write.*` caps the batch at three ops
    assert entry(SUPPORT, "write.transition", TICKET, ctx=NO_REASON) == (
        "deny",
        "obligation.require_reason",
        "access.reason_required",
    )
    assert entry(SUPPORT, "write.transition", TICKET, ctx=CTX)[0] == "allow"
    big = dataclasses.replace(CTX, op_count=4)
    assert entry(SUPPORT, "write.update", TICKET, ctx=big) == (
        "deny",
        "obligation.max_batch",
        "access.batch_too_large",
    )
    assert (
        entry(SUPPORT, "write.update", TICKET, ctx=dataclasses.replace(CTX, op_count=3))[0]
        == "allow"
    )
    # the base obligation of the installation: a reason for supersede and retract
    assert entry(SUPPORT, "write.retract", TICKET, ctx=NO_REASON)[2] == "access.reason_required"


def test_an_obligation_on_write_star_counts_the_entry_decision_once() -> None:
    """The entry decision itself (``write.action`` on the entry) and the steps of its
    expansion are separate decisions under the same context: the batch limit is compared with
    the op count of the expansion each time, not summed."""
    ctx = dataclasses.replace(CTX, op_count=3)
    assert authorize(SUPPORT, "write.action", ACTION, ctx, installed()).effect == "allow"
    assert entry(SUPPORT, "write.action", ACTION, ctx=ctx)[0] == "allow"
    assert entry(SUPPORT, "write.update", TICKET, ctx=ctx)[0] == "allow"
    over = dataclasses.replace(CTX, op_count=4)
    assert authorize(SUPPORT, "write.action", ACTION, over, installed()).code == (
        "access.batch_too_large"
    )


def test_a_dry_run_is_decided_like_the_real_run() -> None:
    dry = dataclasses.replace(CTX, dry_run=True)
    assert entry(SUPPORT, "write.update", TICKET, ctx=dry)[0] == "allow"
    assert entry(SUPPORT, "write.update", CUSTOMER, ctx=dry)[0] == "deny"
    assert entry(
        SUPPORT, "write.transition", TICKET, ctx=dataclasses.replace(NO_REASON, dry_run=True)
    )[0] == ("deny")


# ---- what is asked must be well formed ------------------------------------------------


def test_malformed_questions_are_bugs() -> None:
    for action, resource, rule in (
        ("write.delete", TICKET, GRANTED),
        ("write.update", "graph:nope", GRANTED),
        ("write.update", f"{GRAPH}/function:Bad", GRANTED),
        ("write.update", TICKET, ""),
    ):
        with pytest.raises(GraphCliError) as info:
            authorize_entry(SUPPORT, action, resource, CTX, installed(), rule)
        assert info.value.code == "internal.error", (action, resource, rule)


def test_an_extension_action_of_a_declared_namespace_is_decided_too() -> None:
    assert entry(SUPPORT, "ext.notes.tag", GRAPH)[0] == "allow"
    assert entry(token("support", project="frozen"), "ext.notes.tag", GRAPH)[0] == "deny"


# ---- the property: never more than the deny rules leave --------------------------------

ACTORS = [
    SUPPORT,
    token(),
    token("auditor"),
    token("support", "auditor"),
    token("support", agent_type="explore"),
    token("support", project="frozen"),
    token("support", project="readonly"),
    token("support", project="open"),
    token("support", device="dev-1"),
    actor(),
    maint_actor(),
]
QUESTIONS = [
    (action, resource)
    for action in (
        "read.list",
        "read.get",
        "read.find",
        "read.function",
        "read.view",
        "write.create",
        "write.update",
        "write.retract",
        "write.action",
        "maint.repair",
        "ext.notes.tag",
    )
    for resource in (
        "installation",
        GRAPH,
        f"graph:{OTHER}",
        TICKET,
        CUSTOMER,
        STATUS,
        f"{GRAPH}/type:ex:Secret",
        ACTION,
        f"{GRAPH}/function:open_tickets",
    )
]


def _valid(action: str, resource: str) -> bool:
    return not (
        action in ("read.function", "read.view", "write.action")
        and resource
        in (
            "installation",
            TICKET,
            CUSTOMER,
            STATUS,
            f"{GRAPH}/type:ex:Secret",
        )
    )


@pytest.mark.parametrize("who", range(len(ACTORS)))
def test_an_entry_decision_never_allows_what_authorize_refuses_by_a_rule(who: int) -> None:
    """For every actor and question: when ``authorize`` refuses with a rule of the policy
    (a deny rule, a scope, a type limit, the ceiling: anything but the default or an
    obligation), so does ``authorize_entry`` with the same rule and code; when ``authorize``
    allows, it allows; the two differ only where the default alone refused."""
    caller = ACTORS[who]
    policy = installed()
    differing = 0
    for action, resource in QUESTIONS:
        if not _valid(action, resource):
            continue
        plain = authorize(caller, action, resource, CTX, policy)
        through = authorize_entry(caller, action, resource, CTX, policy, GRANTED)
        if plain.effect == "allow":
            assert through.effect == "allow", (action, resource)
            assert through.obligations == plain.obligations
        elif plain.rule_id == "default.deny":
            assert through.effect == "allow", (action, resource)
            differing += 1
        else:
            assert (through.effect, through.rule_id, through.code) == (
                "deny",
                plain.rule_id,
                plain.code,
            ), (action, resource)
    # the property is not vacuous: the entry decision lets through something for most actors
    assert differing > 0 or caller.client.entrypoint is Entrypoint.MAINT


def test_the_decision_table_has_a_denial_of_every_kind() -> None:
    kinds = {rule.split(".")[0] for _, _, _, rule in DENIED}
    assert kinds == {"type", "deny", "scope"}
