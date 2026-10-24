"""What a policy lets an actor do, summarized (contracts B2.10, B6.11, B6.13).

Pure helpers over ``authorize()`` for ``graph whoami``, ``graph-admin
policy explain`` and the admin API: the access level per graph, whether an
actor maintains a graph, the actions a deny rule withholds below it, the
obligations of the allowed writes, the project scope that applies and the
warning for a project the installation policy has no scope for. Nothing here
decides on its own; every answer is a set of ``authorize()`` calls.
"""

from __future__ import annotations

from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.documents import INSTALLATION_LAYER, layer_of, strings
from graph_cli.kernel.actions import (
    ACCESS_LEVELS,
    PROJECT_MAINT_ACTIONS,
    WRITE_ACTIONS,
    level_actions,
)
from graph_cli.kernel.actor import ActorContext, Assurance
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet

NO_PROJECT: Final = "no_project"
# Obligations do not lower a level: a reason is given, one op at a time.
_PROBE: Final = AuthzContext(op_count=1, has_reason=True, dry_run=False)


def graph_level(actor: ActorContext, graph: GraphId, policy: PolicySet) -> str:
    """The highest level of the ladder whose every action is allowed on ``graph:<id>``."""
    resource = f"graph:{graph}"
    reached = ACCESS_LEVELS[0]
    for level in ACCESS_LEVELS[1:]:
        actions = [a for a in level_actions(level) if not a.endswith("*")]
        if level == "write":
            actions += sorted(WRITE_ACTIONS)
        if not all(
            authorize(actor, a, resource, _PROBE, policy).effect == "allow" for a in actions
        ):
            break
        reached = level
    return reached


def maintains(actor: ActorContext, graph: GraphId, policy: PolicySet) -> bool:
    """Whether ``actor`` may run any graph-level maintenance action of the ``maintain``
    scope level on ``graph:<id>``: what a project admin sees of a graph."""
    resource = f"graph:{graph}"
    return any(
        authorize(actor, a, resource, _PROBE, policy).effect == "allow"
        for a in PROJECT_MAINT_ACTIONS
    )


def withheld_actions(actor: ActorContext, graph: GraphId, policy: PolicySet) -> list[str]:
    """The ladder actions denied on ``graph:<id>`` in a class (read, write) of which at
    least one action is allowed: what deny rules take away from a granted level, which
    ``graph_level`` alone cannot show (a denied ``read.history`` leaves the level at
    ``none`` while the graph stays readable)."""
    resource = f"graph:{graph}"
    ladder = [a for a in level_actions("write") if not a.endswith("*")] + sorted(WRITE_ACTIONS)
    withheld: list[str] = []
    for area in ("read.", "write."):
        actions = [a for a in ladder if a.startswith(area)]
        denied = [
            a for a in actions if authorize(actor, a, resource, _PROBE, policy).effect != "allow"
        ]
        if len(denied) < len(actions):
            withheld += denied
    return sorted(withheld)


def write_obligations(
    actor: ActorContext, graph: GraphId, policy: PolicySet
) -> dict[str, JsonValue]:
    """``{action: {require_reason?, max_batch?}}`` of the writes allowed on ``graph:<id>``."""
    found: dict[str, JsonValue] = {}
    for action in sorted(WRITE_ACTIONS):
        decision = authorize(actor, action, f"graph:{graph}", _PROBE, policy)
        if decision.effect != "allow":
            continue
        entry: dict[str, JsonValue] = {}
        if decision.obligations.require_reason:
            entry["require_reason"] = True
        if decision.obligations.max_batch is not None:
            entry["max_batch"] = decision.obligations.max_batch
        if entry:
            found[action] = entry
    return found


def type_limits(policy: PolicySet) -> dict[str, JsonValue]:
    """The type limits a graph layer declares (``{curie or prefix:*: read|write}``)."""
    limits: dict[str, JsonValue] = {}
    for document in policy.documents:
        types = document.get("types")
        if layer_of(document) != INSTALLATION_LAYER and isinstance(types, dict):
            limits.update(types)
    return dict(sorted(limits.items()))


def scope_of(actor: ActorContext, policy: PolicySet) -> str | None:
    """The project scope that applies: the pointer's project, ``no_project``, or ``None``
    when the installation layer has no scopes (the built-in policy)."""
    projects = _projects(policy)
    if projects is None:
        return None
    project = _project(actor)
    return project if project in projects else NO_PROJECT


def project_warning(actor: ActorContext, policy: PolicySet) -> WarningRecord | None:
    """``access.project_unknown`` for a project the installation policy has no scope for."""
    projects, project = _projects(policy), _project(actor)
    if projects is None or project is None or project in projects:
        return None
    return WarningRecord(
        "access.project_unknown",
        f"the policy has no scope for project {project!r}; the scope for no project applies",
        {"project": project},
    )


def _projects(policy: PolicySet) -> frozenset[str] | None:
    installation = policy.documents[0] if policy.documents else {}
    if layer_of(installation) != INSTALLATION_LAYER or "projects" not in installation:
        return None
    return frozenset(strings(installation, "projects"))


def _project(actor: ActorContext) -> str | None:
    facet = actor.project
    return None if facet.assurance is Assurance.ABSENT else facet.id
