"""``authorize()``: one pure decision per (action, resource) pair (contracts B6.2-B6.9).

Order:

1. the action must be in the taxonomy or a well-formed extension action, and
   the resource must follow the grammar; anything else is a bug
   (``internal.error``, B6.2);
2. the **ceiling**: the app entrypoint never gets a ``maint.*`` action
   (B6.5); hard-coded, evaluated before any policy;
3. the policy: the first matching allow rule, else default deny (B6.6);
4. obligations of the matching policy: a missing reason or a too large batch
   turns the allow into a deny. Dry-runs get the same answer, so a dry-run
   predicts the real outcome.

v1 evaluates exactly the ``graph_cli.policy/1`` subset the built-in document
uses: allow rules matching ``entrypoint``, ``principal_id`` or
``principal_name`` (B6.7), action and resource globs, and the
``require_reason``/``max_batch`` obligations. A document with anything else
is ``internal.error``; authored policies with deny rules and graph layers are
P2.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.kernel.actions import EXT_ACTION_RE, Action, is_known_action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision, Obligations
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet

CEILING_RULE: Final = "ceiling.app_no_maint"
DEFAULT_DENY_RULE: Final = "default.deny"
REASON_RULE: Final = "obligation.require_reason"
BATCH_RULE: Final = "obligation.max_batch"

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
RESOURCE_RE: Final = re.compile(
    rf"^installation$|^graph:{_UUID}(/type:[a-z][a-z0-9_]*:[A-Za-z][A-Za-z0-9_]*)?$"
)
_DOCUMENT_KEYS: Final = frozenset({"schema", "id", "rules", "obligations"})
_RULE_KEYS: Final = frozenset({"id", "effect", "principal", "action", "resource"})
_PRINCIPAL_KEYS: Final = frozenset({"entrypoint", "principal_id", "principal_name"})
_OBLIGATION_KEYS: Final = frozenset({"action", "require_reason", "max_batch"})


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

    if actor.client.entrypoint.value == "app" and action.startswith("maint."):
        return deny(CEILING_RULE, "access.ceiling")
    documents = [_checked_document(d) for d in policy.documents]
    rule_id = next(
        (
            _text(rule, "id")
            for document in documents
            for rule in _list(document, "rules")
            if _matches(rule, actor, action, resource)
        ),
        None,
    )
    if rule_id is None:
        return deny(DEFAULT_DENY_RULE, "access.denied")
    obligations = _obligations(documents, action)
    if obligations.require_reason and not ctx.has_reason:
        return deny(REASON_RULE, "access.reason_required")
    if obligations.max_batch is not None and ctx.op_count > obligations.max_batch:
        return deny(BATCH_RULE, "access.batch_too_large")
    return Decision("allow", checked, resource, rule_id, policy.hash, obligations)


def _matches(
    rule: Mapping[str, JsonValue], actor: ActorContext, action: str, resource: str
) -> bool:
    principal = rule.get("principal", {})
    assert isinstance(principal, Mapping)
    facts = {
        "entrypoint": actor.client.entrypoint.value,
        "principal_id": actor.principal.id,
        "principal_name": actor.principal.name,
    }
    return (
        all(facts[key] == value for key, value in principal.items())
        and any(_glob(pattern, action) for pattern in _strings(rule, "action"))
        and _glob(_text(rule, "resource"), resource)
    )


def _glob(pattern: str, value: str) -> bool:
    """``*`` matches everything, ``prefix*`` a prefix, anything else itself."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _obligations(documents: Sequence[Mapping[str, JsonValue]], action: str) -> Obligations:
    require_reason = False
    max_batch: int | None = None
    for document in documents:
        for entry in _list(document, "obligations"):
            if not any(_glob(p, action) for p in _strings(entry, "action")):
                continue
            require_reason = require_reason or entry.get("require_reason") is True
            batch = entry.get("max_batch")
            if isinstance(batch, int) and not isinstance(batch, bool):
                max_batch = batch if max_batch is None else min(max_batch, batch)
    return Obligations(require_reason=require_reason, max_batch=max_batch)


def _checked_document(document: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """Refuse anything beyond the v1 subset (authored policies are P2)."""
    problems = []
    if document.get("schema") != "graph_cli.policy/1" or not set(document) <= _DOCUMENT_KEYS:
        problems.append("document keys")
    for rule in _list(document, "rules"):
        principal = rule.get("principal", {})
        if (
            not set(rule) <= _RULE_KEYS
            or rule.get("effect") != "allow"
            or not isinstance(rule.get("id"), str)
            or not isinstance(rule.get("resource"), str)
            or not _strings(rule, "action")
            or not isinstance(principal, Mapping)
            or not set(principal) <= _PRINCIPAL_KEYS
        ):
            problems.append(f"rule {rule.get('id')!r}")
    for entry in _list(document, "obligations"):
        batch = entry.get("max_batch")
        if (
            not set(entry) <= _OBLIGATION_KEYS
            or not _strings(entry, "action")
            or not isinstance(entry.get("require_reason", False), bool)
            or (batch is not None and (not isinstance(batch, int) or isinstance(batch, bool)))
        ):
            problems.append("obligation")
    if problems:
        raise GraphCliError(
            "internal.error", f"policy outside the v1 subset: {', '.join(problems)}"
        )
    return document


def _list(document: Mapping[str, JsonValue], key: str) -> list[Mapping[str, JsonValue]]:
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise GraphCliError("internal.error", f"policy {key} is not a list of objects")
    return [v for v in value if isinstance(v, Mapping)]


def _strings(entry: Mapping[str, JsonValue], key: str) -> list[str]:
    value = entry.get(key)
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _text(entry: Mapping[str, JsonValue], key: str) -> str:
    value = entry.get(key)
    assert isinstance(value, str)  # guaranteed by _checked_document
    return value
