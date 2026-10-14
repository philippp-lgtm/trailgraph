"""Closed permission action taxonomy ``graph_cli.actions/1`` (contracts B6.1, B6.2, B6.11).

Lives in the kernel because audit, access_control and storage are mutually
independent and all need the vocabulary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, NewType

from graph_cli.kernel.errors import GraphCliError

Action = NewType("Action", str)

TAXONOMY_SCHEMA: Final = "graph_cli.actions/1"

READ_ACTIONS: Final = frozenset(
    Action(f"read.{verb}")
    for verb in ("list", "get", "find", "traverse", "history", "describe", "audit_own", "native")
)
WRITE_ACTIONS: Final = frozenset(
    Action(f"write.{verb}")
    for verb in (
        "create",
        "update",
        "link",
        "unlink",
        "transition",
        "supersede",
        "retract",
        "alias",
    )
)
MAINT_ACTIONS: Final = frozenset(
    Action(f"maint.{verb}")
    for verb in (
        "graph_create",
        "graph_configure",
        "graph_archive",
        "graph_restore",
        "ontology_release",
        "ontology_pin",
        "migrate",
        "bundle",
        "policy_change",
        "audit_read",
        "audit_verify",
        "audit_anchor",
        "repair",
        "reindex",
        "purge",
    )
)
# Names are part of the closed taxonomy now; their behaviour comes later.
RESERVED_ACTIONS: Final = frozenset(
    Action(a)
    for a in ("read.native", "maint.migrate", "maint.bundle", "maint.policy_change", "maint.purge")
)
ACTIONS: Final[frozenset[Action]] = READ_ACTIONS | WRITE_ACTIONS | MAINT_ACTIONS

EXT_ACTION_RE: Final = re.compile(r"^ext\.[a-z][a-z0-9_]{1,31}\.[a-z][a-z0-9_]*$")
_SCHEMA_ADMIN: Final = frozenset({"maint.migrate", "maint.bundle"})


def is_known_action(action: str, ext_actions: Iterable[str] = ()) -> bool:
    """True for taxonomy actions and for declared extension actions."""
    if action in ACTIONS:
        return True
    return EXT_ACTION_RE.match(action) is not None and action in frozenset(ext_actions)


def require_known_action(action: str, ext_actions: Iterable[str] = ()) -> Action:
    """An unknown action in code is a bug (B6.2)."""
    if not is_known_action(action, ext_actions):
        raise GraphCliError("internal.error", f"unknown action {action!r}")
    return Action(action)


def extension_state_action(namespace: str) -> Action:
    """The implicit ``ext.<ns>.state`` action of every extension (B6.1)."""
    action = f"ext.{namespace}.state"
    if not EXT_ACTION_RE.match(action):
        raise GraphCliError("internal.error", f"invalid extension namespace {namespace!r}")
    return Action(action)


def capability_class(action: str) -> str:
    """Display/policy grouping (B6.11): read, write, schema_admin, graph_admin, extension:<ns>."""
    area, _, rest = action.partition(".")
    if area in ("read", "write"):
        return area
    if area == "maint":
        if rest.startswith("ontology_") or action in _SCHEMA_ADMIN:
            return "schema_admin"
        return "graph_admin"
    if area == "ext":
        return f"extension:{rest.partition('.')[0]}"
    raise GraphCliError("internal.error", f"unknown action {action!r}")
