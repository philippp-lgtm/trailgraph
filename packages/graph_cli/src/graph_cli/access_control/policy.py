"""The built-in policy and its source (contracts B6.8).

v1 reads no policy files: ``BuiltinPolicySource`` serves one constant
document, and its hash (C.2 over the document) is recorded in every
decision. ``<data_root>/policy/`` is reserved for authored policies (P2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet, PolicySource

POLICY_SCHEMA: Final = "graph_cli.policy/1"

BUILTIN_POLICY: Final[Mapping[str, JsonValue]] = {
    "schema": POLICY_SCHEMA,
    "id": "builtin_default",
    "rules": [
        {
            "id": "builtin.allow_app",
            "effect": "allow",
            "principal": {"entrypoint": "app"},
            "action": ["read.*", "write.*", "ext.*"],
            "resource": "graph:*",
        },
        {
            "id": "builtin.allow_app_list",
            "effect": "allow",
            "principal": {"entrypoint": "app"},
            "action": ["read.list", "read.describe"],
            "resource": "installation",
        },
        {
            "id": "builtin.allow_maint",
            "effect": "allow",
            "principal": {"entrypoint": "maint"},
            "action": ["*"],
            "resource": "*",
        },
    ],
    "obligations": [{"action": ["write.supersede", "write.retract"], "require_reason": True}],
}

BUILTIN_POLICY_HASH: Final = hash_value(BUILTIN_POLICY)
BUILTIN_POLICY_SET: Final = PolicySet(BUILTIN_POLICY_HASH, (BUILTIN_POLICY,))


class BuiltinPolicySource:
    """The same built-in policy for the installation and every graph (v1)."""

    def current(self, graph: GraphId | None) -> PolicySet:
        return BUILTIN_POLICY_SET


if TYPE_CHECKING:

    def _conforms(source: BuiltinPolicySource) -> PolicySource:
        return source
