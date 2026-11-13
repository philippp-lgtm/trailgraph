"""What a policy lets an actor do, summarized (contracts B2.10, B6.11, B6.13).

Pure helpers over ``authorize()`` for ``graph whoami``, ``graph-admin
policy explain`` and the admin API: the access level per graph, whether an
actor maintains a graph, the actions a deny rule withholds below it, the
obligations of the allowed writes, what a write allow list lets a caller write
(``write_allowance``), which types a caller sees (``visibility``, ``read_allowance``), the
project scope that applies and the warning for a project the installation policy has no scope
for. Nothing here decides on its own; every answer is a set
of ``authorize()`` calls.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from graph_cli.access_control.authorize import allow_rule_holds, authorize
from graph_cli.access_control.compiler import WRITE_VERBS
from graph_cli.access_control.documents import (
    CURIE_RE,
    INSTALLATION_LAYER,
    fine_types,
    is_write_glob,
    layer_of,
    read_limited,
    rules,
    strings,
)
from graph_cli.access_control.terms import TermModel, TypeTerms, term_resource
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
    """``{action: {require_reason?, max_batch?}}`` of the writes allowed on ``graph:<id>``.

    A write action denied on the graph itself may still be allowed on the types a write
    allow list names; the first such allowed type gives the obligations, so a limited writer
    sees its ``require_reason`` and ``max_batch`` too."""
    found: dict[str, JsonValue] = {}
    granted = write_grant_types(actor, graph, policy)
    for action in sorted(WRITE_ACTIONS):
        resources = [f"graph:{graph}", *(f"graph:{graph}/type:{t}" for t in granted)]
        decision = None
        for resource in resources:
            probe = authorize(actor, action, resource, _PROBE, policy)
            if probe.effect == "allow":
                decision = probe
                break
        if decision is None:
            continue
        entry: dict[str, JsonValue] = {}
        if decision.obligations.require_reason:
            entry["require_reason"] = True
        if decision.obligations.max_batch is not None:
            entry["max_batch"] = decision.obligations.max_batch
        if entry:
            found[action] = entry
    return found


def write_grant_types(actor: ActorContext, graph: GraphId, policy: PolicySet) -> tuple[str, ...]:
    """The types a write allow list names for ``actor`` in ``graph``, sorted: the concrete
    type resources (``graph:<id>/type:<curie>``) of the allow rules whose principal matches
    the actor as ``authorize`` matches it. Only ``writes`` grants produce such rules: a rule
    counts when it names a write action, so the ``grant.<n>.reads`` rules (``read.get``) are
    no write grants. Pure and cheap (no decision is made), so a caller can ask before loading
    a model."""
    prefix = f"graph:{graph}/type:"
    found: set[str] = set()
    for document in policy.documents:
        for rule in rules(document):
            if (
                rule.get("effect") != "allow"
                or not any(is_write_glob(a) for a in strings(rule, "action"))
                or not allow_rule_holds(rule, actor)
            ):
                continue
            for resource in strings(rule, "resource"):
                if resource.startswith(prefix) and CURIE_RE.match(resource[len(prefix) :]):
                    found.add(resource[len(prefix) :])
    return tuple(sorted(found))


def write_allowance(
    actor: ActorContext,
    graph: GraphId,
    policy: PolicySet,
    terms: TermModel | None,
    *,
    level: str | None = None,
) -> dict[str, JsonValue] | None:
    """What a write allow list lets ``actor`` write in ``graph``: ``{type: {verb: true | [term]}}``,
    or ``None`` when there is nothing to say: the level is ``write`` already, no allow rule
    that matches the actor names a type of the graph, or none of those types allows any verb
    (a type without a verb is left out).

    Per type the rules name, and per verb in the order of ``WRITE_VERBS``, ``authorize`` is
    probed on ``graph:<id>/type:<T>``; a verb the type decision denies is absent. The verbs
    without terms (``supersede``, ``retract``, ``alias``) are ``true``. The others are
    ``true`` when every term of the type of that kind is allowed (``create``, ``update``: the
    attributes, ``update`` without the lifecycle ones, which no update may touch;
    ``transition``: the lifecycle attributes; ``link``, ``unlink``: the relations whose
    domain covers the type), else the sorted list of the allowed terms, possibly empty. A term
    is allowed only when its own decision and the decision on its type are (an exact type
    rule does not match the terms below it). A type no rule names terms of (``fine_types``)
    is decided on the type alone, as a write is, so its term verbs are ``true`` when the
    type is allowed. Without a model (``terms`` is ``None``) only the types are probed and
    the term verbs are ``true`` when the type is allowed; with one, a type it lacks is left out.
    A create lists attributes only: an inline edge follows the link right.

    ``level`` is ``graph_level`` when the caller has it already."""
    if (level if level is not None else graph_level(actor, graph, policy)) == "write":
        return None
    granted = write_grant_types(actor, graph, policy)
    if not granted:
        return None
    fine = fine_types(policy.documents, graph)
    found: dict[str, JsonValue] = {}
    for type_curie in granted:
        known = None if terms is None else terms.types.get(type_curie)
        if terms is not None and known is None:
            continue
        verbs: dict[str, JsonValue] = {}
        for verb in WRITE_VERBS:
            action = f"write.{verb}"
            type_resource = f"graph:{graph}/type:{type_curie}"
            if authorize(actor, action, type_resource, _PROBE, policy).effect != "allow":
                continue
            names = _verb_terms(verb, known) if type_curie in fine else None
            if names is None:
                verbs[verb] = True
                continue
            kind, candidates = names
            allowed = [
                name
                for name in candidates
                if (resource := term_resource(graph, type_curie, kind, name)) is not None
                and authorize(actor, action, resource, _PROBE, policy).effect == "allow"
            ]
            verbs[verb] = True if len(allowed) == len(candidates) else allowed
        if verbs:
            found[type_curie] = verbs
    return found or None


def visibility(
    actor: ActorContext, graph: GraphId, policy: PolicySet, concrete_types: Iterable[str]
) -> frozenset[str] | None:
    """The concrete types ``actor`` sees in ``graph`` (type limits plan V4), or ``None`` for no
    limit: the graph is not ``read_limited`` or every type in ``concrete_types`` is visible.

    A type is visible when ``authorize`` allows ``read.get`` on ``graph:<id>/type:<T>``, one
    probe per type and none recorded (like the action listing). ``concrete_types`` are the
    types of the graph's pinned model entities can have; a type outside it is not in the
    answer, so a caller that gets a set does not see it (fail closed). The caller caches the
    answer per policy hash, actor facts and pin."""
    if not read_limited(policy.documents, graph):
        return None
    types = frozenset(concrete_types)
    visible = frozenset(
        t
        for t in types
        if authorize(actor, "read.get", f"graph:{graph}/type:{t}", _PROBE, policy).effect == "allow"
    )
    return None if visible == types else visible


def read_allowance(
    actor: ActorContext, graph: GraphId, policy: PolicySet, concrete_types: Iterable[str]
) -> list[str] | None:
    """What a read limit lets ``actor`` see in ``graph``, for ``whoami`` and ``policy explain``:
    the sorted visible concrete types, or ``None`` when there is nothing to say (the actor sees
    every type; see ``visibility``)."""
    visible = visibility(actor, graph, policy, concrete_types)
    return None if visible is None else sorted(visible)


def _verb_terms(verb: str, known: TypeTerms | None) -> tuple[str, list[str]] | None:
    """The kind (``attr`` or ``rel``) and the sorted terms a verb is decided on below a type;
    ``None`` for a verb without terms or when there is no model to name them."""
    if verb not in ("create", "update", "link", "unlink", "transition") or known is None:
        return None
    if verb in ("link", "unlink"):
        return "rel", sorted(known.relations)
    attributes = known.attributes
    if verb == "transition":
        return "attr", sorted(a for a, t in attributes.items() if t.lifecycle)
    return "attr", sorted(a for a, t in attributes.items() if verb == "create" or not t.lifecycle)


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
