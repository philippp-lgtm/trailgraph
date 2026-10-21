"""The built-in policy, policy sets and the built-in source (contracts B6.8, B6.14).

The built-in document applies wherever no installation policy is installed;
its hash (C.2 over the document) is recorded in every decision.
``policy_set`` layers an installation document and, optionally, a graph's
own document into the ``PolicySet`` that ``authorize()`` evaluates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from graph_cli.access_control.documents import POLICY_SCHEMA
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet, PolicySource

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


def policy_set(
    installation: Mapping[str, JsonValue], graph: Mapping[str, JsonValue] | None = None
) -> PolicySet:
    """The layers for one decision. The hash is the installation document's, or C.2
    over both hashes when the graph has its own policy (B6.14)."""
    if graph is None:
        return PolicySet(hash_value(installation), (installation,))
    digest = hash_value({"installation": hash_value(installation), "graph": hash_value(graph)})
    return PolicySet(digest, (installation, graph))


class BuiltinPolicySource:
    """The same built-in policy for the installation and every graph (v1)."""

    def current(self, graph: GraphId | None) -> PolicySet:
        return BUILTIN_POLICY_SET


if TYPE_CHECKING:

    def _conforms(source: BuiltinPolicySource) -> PolicySource:
        return source
