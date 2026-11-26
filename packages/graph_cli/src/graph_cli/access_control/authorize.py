"""``authorize()``: one pure decision per (action, resource) pair (contracts B6.2-B6.9).

Order:

1. the action must be in the taxonomy or a well-formed extension action, and
   the resource must follow the grammar; anything else is a bug
   (``internal.error``, B6.2);
2. the **ceiling**: the app and server entrypoints never get a ``maint.*``
   action (B6.5), and a project admin (the maintenance entrypoint with a
   verified project: an admin token bound to a project) gets at most the
   actions of the ``maintain`` scope level (graph-level maintenance and
   ``read.get``), on a graph; hard-coded, evaluated before any policy (so it never holds
   a marking: ``marking:<graph>/<curie>`` is no graph resource);
3. the first matching **deny** rule of any document: an explicit deny wins
   (B6.6);
4. the first matching allow rule, else default deny;
5. obligations of every document: a missing reason or a too large batch
   turns the allow into a deny. Dry-runs get the same answer, so a dry-run
   predicts the real outcome.

``authorize_entry`` is the same decision without step 4, for what a catalog entry needs:
a caller who passed an *entry decision* (``authorize`` on the entry resource
``graph:<id>/<kind>:<name>``: any rule that allows it, a catalog grant among them) runs the
entry with the entry's rights, so what the entry reads or writes is not looked up as an allow
rule of its own. The ceiling, every deny rule and the obligations still hold, an allow is never
produced by the policy there (the decision names the rule of the entry decision), and only what
an entry needs can be asked: the graph's reads, writes on the graph and its types and terms.
What a caller sees (types, markings) stays with ``authorize``.

The documents of a ``PolicySet`` are its layers (installation, then the
graph's own policy). Only the installation layer grants; every other rule
narrows, so "the installation allows and the graph allows" is "some allow
rule matches and no deny rule does" (Cedar's permit/forbid). A rule matches
when every ``principal`` key matches the actor (an absent facet matches
nothing; the multi-valued ``role`` matches when the actor holds at least one of
the named roles), an ``action`` glob matches, a ``resource`` glob matches and no
``unless`` condition matches. Allow rules match verified facets and the
entrypoint only (B6.7). A document outside the ``graph_cli.policy/1``
grammar is ``internal.error``: installed documents are checked when they
are loaded (``access_control.documents``), so reaching here is a bug.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.access_control.documents import (
    RESOURCE_RE,
    Fact,
    document_problems,
    glob,
    principal_facts,
    rules,
    strings,
)
from graph_cli.kernel.actions import (
    ENTRY_KIND_ACTIONS,
    EXT_ACTION_RE,
    LEVEL_WRITE_ACTIONS,
    PROJECT_ADMIN_ACTIONS,
    Action,
    is_known_action,
)
from graph_cli.kernel.actor import ActorContext, Assurance, Entrypoint
from graph_cli.kernel.authz import AuthzContext, Decision, Obligations
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet

CEILING_RULE: Final = "ceiling.app_no_maint"
SERVER_CEILING_RULE: Final = "ceiling.server_no_maint"
# The entrypoints that serve the app surface and their ceiling rules (B6.5, B14).
CEILINGS: Final = {"app": CEILING_RULE, "server": SERVER_CEILING_RULE}
PROJECT_CEILING_RULE: Final = "ceiling.project_maint"
DEFAULT_DENY_RULE: Final = "default.deny"
REASON_RULE: Final = "obligation.require_reason"
BATCH_RULE: Final = "obligation.max_batch"


def authorize(
    actor: ActorContext, action: str, resource: str, ctx: AuthzContext, policy: PolicySet
) -> Decision:
    if not (is_known_action(action) or EXT_ACTION_RE.match(action)):
        raise GraphCliError("internal.error", f"unknown action {action!r}")
    if not RESOURCE_RE.match(resource):
        raise GraphCliError("internal.error", f"malformed resource {resource!r}")
    return _decide(actor, Action(action), resource, resource, ctx, policy)


# What a granted entry may ask: reads of the graph itself, and the write actions of the ladder on
# the graph, its types and their terms. Never an entry action (an entry grants no other entry),
# a read of a type or a marking (what a caller sees stays with ``authorize``), a maintenance or
# extension action, or anything outside the entry's graph.
_ENTRY_READS: Final = frozenset({"read.get", "read.find", "read.function", "read.traverse"})
_ENTRY_RESOURCE_RE: Final = re.compile(r"^(graph:[^/]+)/(function|action|view):[^/:]+$")


def authorize_entry(
    actor: ActorContext,
    entry: Decision,
    action: str,
    resource: str,
    ctx: AuthzContext,
    policy: PolicySet,
) -> Decision:
    """The decision on what a granted catalog entry needs (``action`` on ``resource``, say
    ``write.update`` on a type, ``read.get`` on the graph), made *deny-only*: the ceiling, then
    the first matching deny rule of every layer, then the obligations; no allow rule is looked
    up. ``entry`` is the allow decision of ``authorize`` on the entry resource
    (``graph:<id>/<kind>:<name>``, with the action of its kind); an allow names its rule as its
    own, so the audit shows which grant let the step through. Nothing here allows by itself, so a
    policy that denies the step (a type limit, a scope, a term or agent-type rule, the ceiling)
    still refuses it.

    A question outside what an entry needs is a bug (``internal.error``): ``entry`` must be an
    allow of the right kind made under this very policy (a decision from before a policy refresh
    grants nothing); ``resource`` must be the entry's graph or a type or term of it, and
    ``action`` a read of the graph (``read.get``, ``read.find``, ``read.function``,
    ``read.traverse``) or a write action of the ladder, never an entry action, a read of a type
    or marking, ``maint.*`` or ``ext.*``. Recorded like every decision."""
    if not (is_known_action(action) or EXT_ACTION_RE.match(action)):
        raise GraphCliError("internal.error", f"unknown action {action!r}")
    if not RESOURCE_RE.match(resource):
        raise GraphCliError("internal.error", f"malformed resource {resource!r}")
    found = _ENTRY_RESOURCE_RE.match(entry.resource) if RESOURCE_RE.match(entry.resource) else None
    if (
        entry.effect != "allow"
        or found is None
        or entry.action != ENTRY_KIND_ACTIONS[found.group(2)]
        or not entry.rule_id
    ):
        raise GraphCliError(
            "internal.error",
            "an entry decision is an allow on the entry, by the action of its kind",
        )
    if entry.policy_hash != policy.hash:
        raise GraphCliError("internal.error", "the entry decision was made under another policy")
    graph = found.group(1)
    if not _entry_needs(action, resource, graph):
        raise GraphCliError(
            "internal.error", f"{action} on {resource} is not something an entry needs"
        )
    return _decide(actor, Action(action), resource, resource, ctx, policy, entry.rule_id)


def _entry_needs(action: str, resource: str, graph: str) -> bool:
    """Whether ``action`` on ``resource`` is something an entry of ``graph`` may ask."""
    if action in _ENTRY_READS:
        return resource == graph
    if action in LEVEL_WRITE_ACTIONS:
        return resource == graph or resource.startswith(f"{graph}/type:")
    return False


# Appended to the resource of ``authorize_unlisted_type`` and ``authorize_unlisted_attribute``:
# no CURIE or name (so no exact rule resource) ends in it, while every glob that matches the
# type or attribute still matches.
_UNLISTED_MARK: Final = "~"


def authorize_unlisted_type(
    actor: ActorContext,
    action: str,
    graph: GraphId,
    type_curie: str,
    ctx: AuthzContext,
    policy: PolicySet,
) -> Decision:
    """``authorize`` for ``graph:<graph>/type:<type_curie>`` as if no rule named that type: the
    decision a type gets that the policy does not list. Rules on the exact resource (a type
    limit, a read allow list) do not match; the globs of the graph and of the type's prefix
    do, as they match a type the model does not have. A caller who must not learn that a
    type exists gets its answers from this decision (type read limits plan V6); the decision
    names the resource as asked."""
    if not (is_known_action(action) or EXT_ACTION_RE.match(action)):
        raise GraphCliError("internal.error", f"unknown action {action!r}")
    resource = f"graph:{graph}/type:{type_curie}"
    if not RESOURCE_RE.match(resource):
        raise GraphCliError("internal.error", f"malformed resource {resource!r}")
    return _decide(actor, Action(action), resource + _UNLISTED_MARK, resource, ctx, policy)


def authorize_unlisted_attribute(
    actor: ActorContext,
    action: str,
    graph: GraphId,
    type_curie: str,
    attribute: str,
    ctx: AuthzContext,
    policy: PolicySet,
) -> Decision:
    """``authorize`` for ``graph:<graph>/type:<type_curie>/attr:<attribute>`` as if no rule
    named that attribute: the decision an attribute gets that the policy does not list, which
    is what a name the type does not declare gets. A rule on the exact attribute (an entry of
    a ``writes`` list, a term deny rule) does not match; the globs of the graph, of the type's
    terms (``attr:*``) and of its prefix do. A caller who must not learn that an attribute
    exists (a marked one it does not hold) gets its answers to a write naming it from this
    decision (field visibility plan F6); the decision names the resource as asked."""
    if not (is_known_action(action) or EXT_ACTION_RE.match(action)):
        raise GraphCliError("internal.error", f"unknown action {action!r}")
    resource = f"graph:{graph}/type:{type_curie}/attr:{attribute}"
    if not RESOURCE_RE.match(resource):
        raise GraphCliError("internal.error", f"malformed resource {resource!r}")
    return _decide(actor, Action(action), resource + _UNLISTED_MARK, resource, ctx, policy)


def _decide(
    actor: ActorContext,
    checked: Action,
    resource: str,
    named: str,
    ctx: AuthzContext,
    policy: PolicySet,
    granted_by: str | None = None,
) -> Decision:
    """The decision of ``authorize``; rules are matched against ``resource``, the decision
    names ``named``. With ``granted_by`` no allow rule is looked up and an allow names that
    rule (``authorize_entry``)."""
    action = str(checked)

    def deny(rule_id: str, code: str) -> Decision:
        return Decision("deny", checked, named, rule_id, policy.hash, code=code)

    ceiling = CEILINGS.get(actor.client.entrypoint.value)
    if ceiling is not None and action.startswith("maint."):
        return deny(ceiling, "access.ceiling")
    project_admin = is_project_admin(actor)
    if project_admin and (action not in PROJECT_ADMIN_ACTIONS or not resource.startswith("graph:")):
        return deny(PROJECT_CEILING_RULE, "access.ceiling")
    documents = [_checked_document(d) for d in policy.documents]
    facts = principal_facts(actor)
    denied = next(
        (
            rule
            for document in documents
            for rule in rules(document)
            if rule.get("effect") == "deny" and _matches(rule, facts, facts, action, resource)
        ),
        None,
    )
    if denied is not None:
        return deny(_text(denied, "id"), "access.denied")
    if granted_by is None:
        verified = principal_facts(actor, verified_only=True)
        allowed = next(
            (
                rule
                for document in documents
                for rule in rules(document)
                if rule.get("effect") == "allow"
                and _matches(rule, verified, facts, action, resource)
                and (not project_admin or "project" in _principal(rule))
            ),
            None,
        )
        if allowed is None:
            return deny(DEFAULT_DENY_RULE, "access.denied")
        granted_by = _text(allowed, "id")
    obligations = _obligations(documents, action)
    if obligations.require_reason and not ctx.has_reason:
        return deny(REASON_RULE, "access.reason_required")
    if obligations.max_batch is not None and ctx.op_count > obligations.max_batch:
        return deny(BATCH_RULE, "access.batch_too_large")
    return Decision("allow", checked, named, granted_by, policy.hash, obligations)


def allow_rule_holds(rule: Mapping[str, JsonValue], actor: ActorContext) -> bool:
    """Whether the principal of an allow ``rule`` matches ``actor`` as ``authorize`` matches it:
    verified facets only, and for a project admin only a rule that names a project. The
    action and resource of the rule are not looked at; callers that ask which resources a
    caller's grants name use this to pick the rules."""
    return _principal_matches(
        rule.get("principal", {}), principal_facts(actor, verified_only=True)
    ) and (not is_project_admin(actor) or "project" in _principal(rule))


def is_project_admin(actor: ActorContext) -> bool:
    """The maintenance entrypoint acting for a verified project: an admin token bound to
    a project. Locally the project is asserted, so a local maintainer never is one."""
    return (
        actor.client.entrypoint is Entrypoint.MAINT
        and actor.project.assurance is Assurance.VERIFIED
    )


def _matches(
    rule: Mapping[str, JsonValue],
    facts: Mapping[str, Fact],
    conditions: Mapping[str, Fact],
    action: str,
    resource: str,
) -> bool:
    """``facts`` for the principal (verified ones only for an allow rule), ``conditions``
    for ``unless``."""
    return (
        _principal_matches(rule.get("principal", {}), facts)
        and any(glob(pattern, action) for pattern in strings(rule, "action"))
        and _resource_matches(rule.get("resource"), resource)
        and not any(
            _condition_matches(condition, conditions, resource) for condition in _conditions(rule)
        )
    )


def _principal_matches(principal: JsonValue, facts: Mapping[str, Fact]) -> bool:
    assert isinstance(principal, Mapping)  # guaranteed by _checked_document
    for key, expected in principal.items():
        value = facts.get(key)
        if value is None:
            return False
        wanted = expected if isinstance(expected, list) else [expected]
        # A multi-valued fact (the roles) matches when the actor holds one of the expected.
        if isinstance(value, tuple):
            if not any(v in wanted for v in value):
                return False
        elif value not in wanted:
            return False
    return True


def _resource_matches(patterns: JsonValue, resource: str) -> bool:
    if isinstance(patterns, str):
        return glob(patterns, resource)
    assert isinstance(patterns, list)
    return any(isinstance(p, str) and glob(p, resource) for p in patterns)


def _condition_matches(
    condition: Mapping[str, JsonValue], facts: Mapping[str, Fact], resource: str
) -> bool:
    """An ``unless`` condition: every part it has must match."""
    if "principal" in condition and not _principal_matches(condition["principal"], facts):
        return False
    return "resource" not in condition or _resource_matches(condition["resource"], resource)


def _principal(rule: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    value = rule.get("principal", {})
    assert isinstance(value, Mapping)  # guaranteed by _checked_document
    return value


def _conditions(rule: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    value = rule.get("unless", [])
    assert isinstance(value, list)
    return [c for c in value if isinstance(c, Mapping)]


def _obligations(documents: Sequence[Mapping[str, JsonValue]], action: str) -> Obligations:
    require_reason = False
    max_batch: int | None = None
    for document in documents:
        for entry in rules(document, "obligations"):
            if not any(glob(p, action) for p in strings(entry, "action")):
                continue
            require_reason = require_reason or entry.get("require_reason") is True
            batch = entry.get("max_batch")
            if isinstance(batch, int) and not isinstance(batch, bool):
                max_batch = batch if max_batch is None else min(max_batch, batch)
    return Obligations(require_reason=require_reason, max_batch=max_batch)


def _checked_document(document: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    problems = document_problems(document)
    if problems:
        shown = "; ".join(f"{p.path}: {p.message}" for p in problems[:5])
        raise GraphCliError("internal.error", f"policy outside graph_cli.policy/1: {shown}")
    return document


def _text(entry: Mapping[str, JsonValue], key: str) -> str:
    value = entry.get(key)
    assert isinstance(value, str)  # guaranteed by _checked_document
    return value
