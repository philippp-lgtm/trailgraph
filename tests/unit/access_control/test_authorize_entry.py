"""``authorize_entry``: the deny-only decision for what a granted catalog entry needs
(catalog callers plan CA5).

An entry decision (``authorize`` on ``graph:<id>/<kind>:<name>``) lets a caller run the entry;
what the entry reads or writes is then decided by ``authorize_entry``: the ceiling and the first
matching deny rule of every layer, then the obligations, and never an allow rule. The tests pin
that it never allows what a deny rule, a scope or a ceiling refuses, that it allows what only the
default would refuse, and that it answers only questions an entry may ask (an allow entry
decision of the right kind under the same policy, the graph's reads, writes on the graph, its
types and terms). The mutation checks (remove the deny loop, a guard, the ceiling check: these
tests fail) are recorded in the implementation notes of the change.
"""

import dataclasses

import pytest
from support.audit_events import maint_actor
from support.txbuilder import actor

from graph_cli.access_control.authorize import _subject, authorize, authorize_entry
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.actions import ENTRY_KIND_ACTIONS, Action
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext, Decision, Obligations
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
FUNCTION = f"{GRAPH}/function:open_tickets"
VIEW = f"{GRAPH}/view:ticket_overview"
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


def passed(
    *,
    kind: str = "action",
    resource: str | None = None,
    rule: str = GRANTED,
    policy: PolicySet | None = None,
    who: ActorContext | None = None,
) -> Decision:
    """An entry decision as ``authorize`` makes it for ``who`` (``SUPPORT``), built by hand so
    that any actor can be asked about (the property tests need entries a given actor was never
    granted). ``test_a_real_entry_decision_is_accepted`` feeds a decision of ``authorize``."""
    named = {"action": ACTION, "function": FUNCTION, "view": VIEW}[kind]
    return Decision(
        "allow",
        Action(ENTRY_KIND_ACTIONS[kind]),
        resource or named,
        rule,
        (policy or installed()).hash,
        subject=_subject(who or SUPPORT),
    )


def entry(
    who: ActorContext,
    action: str,
    resource: str,
    *,
    ctx: AuthzContext = CTX,
    policy: PolicySet | None = None,
) -> tuple[str, str, str | None]:
    decision = authorize_entry(
        who, passed(policy=policy, who=who), action, resource, ctx, policy or installed()
    )
    return decision.effect, decision.rule_id, decision.code


# ---- it never looks up an allow rule ---------------------------------------------------


def test_what_no_rule_allows_is_allowed_to_the_entry_and_names_the_granting_rule() -> None:
    """The caller's grants do not name a write of a type: ``authorize`` refuses it by default,
    and the entry decision (its action was granted by ``grant.1.actions``) allows it."""
    assert authorize(SUPPORT, "write.update", TICKET, CTX, installed()).rule_id == "default.deny"
    decision = authorize_entry(SUPPORT, passed(), "write.update", TICKET, CTX, installed())
    assert decision.effect == "allow"
    assert (decision.action, decision.resource) == ("write.update", TICKET)
    assert decision.rule_id == GRANTED
    assert decision.policy_hash == installed().hash
    assert decision.code is None
    assert decision.obligations == Obligations(max_batch=3)
    assert decision.to_json()["rule_id"] == GRANTED
    # reads of the graph itself, with the actions the caller could not use by hand
    assert entry(SUPPORT, "read.get", GRAPH)[0] == "allow"
    assert entry(SUPPORT, "read.find", GRAPH)[0] == "allow"
    assert entry(SUPPORT, "write.create", STATUS)[0] == "allow"
    assert entry(SUPPORT, "write.link", f"{TICKET}/rel:ex:about")[0] == "allow"


def test_the_decision_names_the_rule_of_the_entry_decision() -> None:
    for kind, rule in (
        ("function", "grant.1.functions"),
        ("action", "grant.7.actions"),
        ("view", "grant.2.views"),
        ("action", "grant.3"),  # a level write grant allows write.action through its glob
    ):
        decision = authorize_entry(
            SUPPORT, passed(kind=kind, rule=rule), "read.get", GRAPH, CTX, installed()
        )
        assert decision.rule_id == rule


def test_the_decision_is_pure_and_repeats() -> None:
    first = authorize_entry(SUPPORT, passed(), "write.update", TICKET, CTX, installed())
    assert authorize_entry(SUPPORT, passed(), "write.update", TICKET, CTX, installed()) == first


# ---- a deny rule of any layer wins ----------------------------------------------------


DENIED = [
    # (who, action, resource, rule): the first matching deny rule, of either layer
    # graph layer: a type limit (read: no writes), a hidden type (none: no writes), a term rule
    (SUPPORT, "write.update", f"{GRAPH}/type:ex:Secret", "type.ex:Secret"),
    (SUPPORT, "write.update", CUSTOMER, "type.ex:Customer"),
    (SUPPORT, "write.create", CUSTOMER, "type.ex:Customer"),
    (SUPPORT, "write.update", STATUS, "deny.no_status_update"),
    # installation: deny rules by agent type, role and device
    (token("support", agent_type="explore"), "write.update", TICKET, "deny.no_explore_writes"),
    (token("support", agent_type="explore"), "write.create", TICKET, "deny.no_explore_writes"),
    (token("support", "auditor"), "write.retract", TICKET, "deny.auditor_no_retract"),
    (token("support", device="dev-1"), "read.find", GRAPH, "deny.no_device_find"),
    # scopes: none takes every read and write, read takes the writes
    (token("support", project="frozen"), "read.get", GRAPH, "scope.frozen.example_graph"),
    (token("support", project="frozen"), "write.update", TICKET, "scope.frozen.example_graph"),
    (token("support", project="readonly"), "write.update", TICKET, "scope.readonly.example_graph"),
    (token("support", project="readonly"), "write.create", STATUS, "scope.readonly.example_graph"),
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
    # another graph, with an entry decision of its own: the scope of this graph does not apply
    other = f"graph:{OTHER}"

    def decide(who: ActorContext, action: str, resource: str) -> str:
        elsewhere = passed(resource=f"{other}/action:close_ticket", who=who)
        return authorize_entry(who, elsewhere, action, resource, CTX, installed()).effect

    assert decide(token("support", project="frozen"), "read.get", other) == "allow"
    assert (
        decide(token("support", project="readonly"), "write.update", f"{other}/type:ex:Ticket")
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
    """An entry needs no ``maint.*`` action: asking for one is a bug, never an allow (the
    ceilings of the app and the server would deny it anyway, but it is not even asked)."""
    for who in (token("support"), actor(), maint_actor()):
        for action in ("maint.audit_verify", "maint.graph_configure", "maint.repair"):
            for resource in ("installation", GRAPH):
                with pytest.raises(GraphCliError) as info:
                    authorize_entry(who, passed(who=who), action, resource, CTX, installed())
                assert info.value.code == "internal.error", (action, resource)


def test_a_project_admin_keeps_its_ceiling() -> None:
    admin = dataclasses.replace(
        maint_actor(), project=Facet(Assurance.VERIFIED, id="open", source="token")
    )
    # read.get of a graph is within what a project admin gets
    assert entry(admin, "read.get", GRAPH)[0] == "allow"
    for action, resource in (
        ("write.update", TICKET),
        ("write.create", GRAPH),
        ("read.function", GRAPH),
        ("read.find", GRAPH),
    ):
        assert entry(admin, action, resource) == (
            "deny",
            "ceiling.project_maint",
            "access.ceiling",
        ), (action, resource)


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


def _refused(entry_decision: Decision, action: str, resource: str, policy: PolicySet) -> None:
    with pytest.raises(GraphCliError) as info:
        authorize_entry(SUPPORT, entry_decision, action, resource, CTX, policy)
    assert info.value.code == "internal.error", (entry_decision, action, resource)


def test_malformed_questions_are_bugs() -> None:
    for action, resource in (
        ("write.delete", TICKET),
        ("write.update", "graph:nope"),
        ("write.update", f"{GRAPH}/function:Bad"),
    ):
        _refused(passed(), action, resource, installed())


def test_the_entry_decision_must_be_an_allow() -> None:
    denial = Decision(
        "deny",
        Action("write.action"),
        ACTION,
        "default.deny",
        installed().hash,
        code="access.denied",
    )
    _refused(denial, "read.get", GRAPH, installed())


def test_the_entry_decision_must_be_made_on_an_entry_resource() -> None:
    for resource in (GRAPH, TICKET, STATUS, "installation", f"{GRAPH}/type:ex:Ticket"):
        _refused(passed(resource=resource), "read.get", GRAPH, installed())


def test_the_entry_decision_must_carry_the_action_of_its_kind() -> None:
    for kind, wrong in (
        ("action", "read.function"),
        ("function", "write.action"),
        ("view", "read.function"),
        ("function", "read.get"),
    ):
        named = {"action": ACTION, "function": FUNCTION, "view": VIEW}[kind]
        mismatched = Decision("allow", Action(wrong), named, GRANTED, installed().hash)
        _refused(mismatched, "read.get", GRAPH, installed())
    # the right action on the wrong kind of entry
    swapped = Decision("allow", Action("read.function"), ACTION, GRANTED, installed().hash)
    _refused(swapped, "read.get", GRAPH, installed())


def test_the_entry_decision_must_be_made_under_the_same_policy() -> None:
    """After a policy refresh a decision from before grants nothing (the lock-time re-decision
    must ask ``authorize`` on the entry again)."""
    before = installed(layer=False)
    assert before.hash != installed().hash
    _refused(passed(policy=before), "read.get", GRAPH, installed())
    # the same policy: accepted
    assert (
        authorize_entry(SUPPORT, passed(policy=before), "read.get", GRAPH, CTX, before).effect
        == "allow"
    )


def test_the_entry_decision_must_name_its_rule() -> None:
    _refused(passed(rule=""), "read.get", GRAPH, installed())


def test_the_question_must_be_about_the_graph_of_the_entry() -> None:
    other = f"graph:{OTHER}"
    for action, resource in (
        ("read.get", other),
        ("write.update", f"{other}/type:ex:Ticket"),
        ("write.create", other),
    ):
        _refused(passed(), action, resource, installed())


def test_an_entry_never_asks_another_entry_a_marking_or_the_installation() -> None:
    marking = f"marking:{EXAMPLE}/ex:secret"
    for action, resource in (
        ("read.function", FUNCTION),
        ("read.view", VIEW),
        ("write.action", ACTION),
        ("read.get", marking),
        ("write.update", marking),
        ("read.get", "installation"),
        ("write.update", "installation"),
    ):
        _refused(passed(), action, resource, installed())


def test_what_a_caller_sees_stays_with_authorize() -> None:
    """``read.get`` is asked on the graph itself only: the visibility of a type is decided by
    ``authorize`` (``visibility()``), never deny-only."""
    for resource in (TICKET, CUSTOMER, STATUS, f"{GRAPH}/type:ex:Secret"):
        _refused(passed(), "read.get", resource, installed())


def test_only_the_reads_and_writes_an_entry_needs_are_asked() -> None:
    for action in ("read.list", "read.describe", "read.history", "read.query", "read.native"):
        if action == "read.native":
            continue  # not in the taxonomy: covered by the unknown-action case below
        _refused(passed(), action, GRAPH, installed())
    for action in ("read.find", "read.function", "read.traverse"):
        _refused(passed(), action, TICKET, installed())  # reads only on the graph itself
    for action in ("ext.notes.tag", "maint.repair"):
        _refused(passed(), action, GRAPH, installed())
    _refused(passed(), "read.native", GRAPH, installed())  # unknown action


def test_the_questions_of_an_entry_are_decided_deny_only() -> None:
    for action in ("read.get", "read.find", "read.function", "read.traverse"):
        assert entry(SUPPORT, action, GRAPH)[0] == "allow", action
    # what no rule allows is allowed to the entry; what a rule denies stays denied
    for action in ("write.create", "write.update", "write.link", "write.supersede"):
        for resource in (GRAPH, TICKET):
            assert entry(SUPPORT, action, resource)[0] == "allow", (action, resource)
    assert entry(SUPPORT, "write.create", STATUS)[0] == "allow"
    assert entry(SUPPORT, "write.update", CUSTOMER)[0] == "deny"  # a type limit
    assert entry(SUPPORT, "write.update", STATUS)[1] == "deny.no_status_update"  # a term rule


@pytest.mark.parametrize("kind", ["function", "view"])
def test_only_an_action_entry_decision_authorizes_writes(kind: str) -> None:
    """A function or a view entry decision (which a plain reader's ``level: read`` also passes
    through its glob) never lets a write through, whatever the policy leaves of it."""
    for action in ("write.create", "write.update", "write.retract", "write.link"):
        for resource in (GRAPH, TICKET, STATUS):
            _refused(passed(kind=kind), action, resource, installed())
    # reads of the graph stay what an entry of any kind may ask
    for action in ("read.get", "read.find", "read.function", "read.traverse"):
        decision = authorize_entry(SUPPORT, passed(kind=kind), action, GRAPH, CTX, installed())
        assert decision.effect == "allow", (kind, action)
    # the action entry decision may write
    assert entry(SUPPORT, "write.update", TICKET)[0] == "allow"


def test_a_plain_reader_cannot_write_through_a_function_entry_decision() -> None:
    """The case of the review: ``authorize`` allows ``read.function`` on a function entry
    through ``level: read`` and refuses ``write.update`` by default; the entry decision must not
    turn that into a write."""
    reader = token("reader")
    document = compile_policy(
        INSTALLATION.replace(
            b"grants:\n",
            b"grants:\n  - {entrypoint: server, principal: {role: reader},"
            b" graphs: [example_graph], level: read}\n",
        ).replace(
            b"  auditor: {description", b"  reader: {description: x}\n  auditor: {description"
        ),
        file="i.yaml",
        graphs=GRAPHS,
    ).document
    policy = policy_set(document)
    decision = authorize(reader, "read.function", FUNCTION, CTX, policy)
    assert decision.effect == "allow"
    assert authorize(reader, "write.update", TICKET, CTX, policy).effect == "deny"
    with pytest.raises(GraphCliError) as info:
        authorize_entry(reader, decision, "write.update", TICKET, CTX, policy)
    assert info.value.code == "internal.error"


def test_the_entry_decision_must_be_made_for_the_same_actor() -> None:
    """A decision made for one actor is no pass for another: the roles and facts it was made
    for are compared (never carry a decision across requests)."""
    for other in (token("auditor"), token("support", project="open"), actor(), maint_actor()):
        with pytest.raises(GraphCliError) as info:
            authorize_entry(other, passed(who=SUPPORT), "read.get", GRAPH, CTX, installed())
        assert info.value.code == "internal.error"
    # the same facts, another object: accepted
    twin = token("support")
    assert authorize_entry(
        twin, passed(who=SUPPORT), "read.get", GRAPH, CTX, installed()
    ).effect == ("allow")
    # a decision with no subject (not made by authorize) is never accepted
    bare = dataclasses.replace(passed(), subject=None)
    _refused(bare, "read.get", GRAPH, installed())


def test_a_real_entry_decision_is_accepted() -> None:
    """The decisions ``authorize`` returns for the catalog token on its entries feed
    ``authorize_entry`` as they are (no drift between what ``authorize`` returns and what the
    guards accept), each with the action of its kind."""
    policy = installed()
    for kind, resource, rule in (
        ("action", ACTION, "grant.1.actions"),
        ("function", FUNCTION, "grant.1.functions"),
    ):
        real = authorize(SUPPORT, str(ENTRY_KIND_ACTIONS[kind]), resource, CTX, policy)
        assert (real.effect, real.rule_id) == ("allow", rule)
        through = authorize_entry(SUPPORT, real, "read.get", GRAPH, CTX, policy)
        assert (through.effect, through.rule_id) == ("allow", rule)
    real = authorize(SUPPORT, "write.action", ACTION, CTX, policy)
    assert authorize_entry(SUPPORT, real, "write.update", TICKET, CTX, policy).effect == "allow"
    # a denial of ``authorize`` is no entry decision
    denied = authorize(SUPPORT, "write.action", f"{GRAPH}/action:other", CTX, policy)
    assert denied.effect == "deny"
    _refused(denied, "read.get", GRAPH, policy)


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


def _questions(graph: str) -> list[tuple[str, str]]:
    types = (f"{graph}/type:ex:Ticket", f"{graph}/type:ex:Customer", f"{graph}/type:ex:Secret")
    return [
        *(
            (action, graph)
            for action in ("read.get", "read.find", "read.function", "read.traverse")
        ),
        *(
            (action, resource)
            for action in ("write.create", "write.update", "write.retract", "write.link")
            for resource in (graph, *types, f"{types[0]}/attr:status")
        ),
    ]


# the entry decision of each graph, and what it may ask on it
QUESTIONS = [
    (f"graph:{g}", question) for g in (EXAMPLE, OTHER) for question in _questions(f"graph:{g}")
]


@pytest.mark.parametrize("who", range(len(ACTORS)))
def test_an_entry_decision_never_allows_what_authorize_refuses_by_a_rule(who: int) -> None:
    """For every actor and question: when ``authorize`` refuses with a rule of the policy
    (a deny rule, a scope, a type limit, the ceiling: anything but the default or an
    obligation), so does ``authorize_entry`` with the same rule and code; when ``authorize``
    allows, it allows; the two differ only where the default alone refused."""
    caller = ACTORS[who]
    policy = installed()
    differing = 0
    for graph, (action, resource) in QUESTIONS:
        plain = authorize(caller, action, resource, CTX, policy)
        decision = passed(resource=f"{graph}/action:close_ticket", who=caller)
        through = authorize_entry(caller, decision, action, resource, CTX, policy)
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
