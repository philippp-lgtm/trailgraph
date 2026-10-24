"""``authorize()``: one pure decision per (action, resource) pair (contracts B6.2-B6.9).

Order:

1. the action must be in the taxonomy or a well-formed extension action, and
   the resource must follow the grammar; anything else is a bug
   (``internal.error``, B6.2);
2. the **ceiling**: the app and server entrypoints never get a ``maint.*``
   action (B6.5), and a project admin (the maintenance entrypoint with a
   verified project: an admin token bound to a project) gets at most the
   actions of the ``maintain`` scope level (graph-level maintenance and
   ``read.get``), on a graph; hard-coded, evaluated before any policy;
3. the first matching **deny** rule of any document: an explicit deny wins
   (B6.6);
4. the first matching allow rule, else default deny;
5. obligations of every document: a missing reason or a too large batch
   turns the allow into a deny. Dry-runs get the same answer, so a dry-run
   predicts the real outcome.

The documents of a ``PolicySet`` are its layers (installation, then the
graph's own policy). Only the installation layer grants; every other rule
narrows, so "the installation allows and the graph allows" is "some allow
rule matches and no deny rule does" (Cedar's permit/forbid). A rule matches
when every ``principal`` key matches the actor (an absent facet matches
nothing), an ``action`` glob matches, a ``resource`` glob matches and no
``unless`` condition matches. Allow rules match verified facets and the
entrypoint only (B6.7). A document outside the ``graph_cli.policy/1``
grammar is ``internal.error``: installed documents are checked when they
are loaded (``access_control.documents``), so reaching here is a bug.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.access_control.documents import (
    RESOURCE_RE,
    document_problems,
    glob,
    principal_facts,
    rules,
    strings,
)
from graph_cli.kernel.actions import (
    EXT_ACTION_RE,
    PROJECT_ADMIN_ACTIONS,
    Action,
    is_known_action,
)
from graph_cli.kernel.actor import ActorContext, Assurance, Entrypoint
from graph_cli.kernel.authz import AuthzContext, Decision, Obligations
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
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
    checked = Action(action)

    def deny(rule_id: str, code: str) -> Decision:
        return Decision("deny", checked, resource, rule_id, policy.hash, code=code)

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
    obligations = _obligations(documents, action)
    if obligations.require_reason and not ctx.has_reason:
        return deny(REASON_RULE, "access.reason_required")
    if obligations.max_batch is not None and ctx.op_count > obligations.max_batch:
        return deny(BATCH_RULE, "access.batch_too_large")
    return Decision("allow", checked, resource, _text(allowed, "id"), policy.hash, obligations)


def is_project_admin(actor: ActorContext) -> bool:
    """The maintenance entrypoint acting for a verified project: an admin token bound to
    a project. Locally the project is asserted, so a local maintainer never is one."""
    return (
        actor.client.entrypoint is Entrypoint.MAINT
        and actor.project.assurance is Assurance.VERIFIED
    )


def _matches(
    rule: Mapping[str, JsonValue],
    facts: Mapping[str, str | None],
    conditions: Mapping[str, str | None],
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


def _principal_matches(principal: JsonValue, facts: Mapping[str, str | None]) -> bool:
    assert isinstance(principal, Mapping)  # guaranteed by _checked_document
    for key, expected in principal.items():
        value = facts.get(key)
        if value is None:
            return False
        if value not in (expected if isinstance(expected, list) else [expected]):
            return False
    return True


def _resource_matches(patterns: JsonValue, resource: str) -> bool:
    if isinstance(patterns, str):
        return glob(patterns, resource)
    assert isinstance(patterns, list)
    return any(isinstance(p, str) and glob(p, resource) for p in patterns)


def _condition_matches(
    condition: Mapping[str, JsonValue], facts: Mapping[str, str | None], resource: str
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
