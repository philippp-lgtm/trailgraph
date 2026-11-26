"""What a policy lets an actor do, summarized (contracts B2.10, B6.11, B6.13).

Pure helpers over ``authorize()`` for ``graph whoami``, ``graph-admin
policy explain`` and the admin API: the access level per graph, whether an
actor maintains a graph, the actions a deny rule withholds below it, the
obligations of the allowed writes, what a write allow list lets a caller write
(``write_allowance``), which types a caller sees (``visibility``, ``read_allowance``), which
markings it holds (``held_markings``, ``marking_allowance``), the
project scope that applies and the warning for a project the installation policy has no scope
for. Nothing here decides on its own; every answer is a set
of ``authorize()`` calls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.access_control.authorize import allow_rule_holds, authorize, authorize_entry
from graph_cli.access_control.compiler import CATALOG_BASE_ACTIONS, WRITE_VERBS
from graph_cli.access_control.documents import (
    CURIE_RE,
    INSTALLATION_LAYER,
    fine_types,
    glob,
    is_term_write_glob,
    layer_of,
    read_limited,
    rules,
    strings,
)
from graph_cli.access_control.terms import TermModel, TypeTerms, marking_resource, term_resource
from graph_cli.kernel.actions import (
    ACCESS_LEVELS,
    CATALOG_LEVEL,
    ENTRY_KIND_ACTIONS,
    ENTRY_NAME_RE,
    ENTRY_PLURALS,
    LEVEL_WRITE_ACTIONS,
    PROJECT_MAINT_ACTIONS,
    level_actions,
)
from graph_cli.kernel.actor import ActorContext, Assurance
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet

NO_PROJECT: Final = "no_project"
# Obligations do not lower a level: a reason is given, one op at a time.
_PROBE: Final = AuthzContext(op_count=1, has_reason=True, dry_run=False)
_UNDECLARED_MARKING: Final = "probe:undeclared"  # a marking no model declares


def graph_level(actor: ActorContext, graph: GraphId, policy: PolicySet) -> str:
    """The highest level of the ladder whose every action is allowed on ``graph:<id>``, or
    ``catalog`` for a caller whose grants on the graph are catalog grants only (it holds no right
    on the graph itself beyond listing and describing it, which no rung of the ladder describes)
    and the decisions still let it list and describe the graph: a catalog caller a scope or a deny
    rule takes the graph from is at ``none``, like a reader in the same position."""
    if _catalog_level(actor, graph, policy):
        return CATALOG_LEVEL
    resource = f"graph:{graph}"
    reached = ACCESS_LEVELS[0]
    for level in ACCESS_LEVELS[1:]:
        actions = [a for a in level_actions(level) if not a.endswith("*")]
        if level == "write":
            actions += sorted(LEVEL_WRITE_ACTIONS)
        if not all(
            authorize(actor, a, resource, _PROBE, policy).effect == "allow" for a in actions
        ):
            break
        reached = level
    return reached


def _catalog_level(actor: ActorContext, graph: GraphId, policy: PolicySet) -> bool:
    """Whether ``actor`` is at the level ``catalog`` on ``graph``: its grants there are catalog
    grants only and ``read.list`` and ``read.describe`` on ``graph:<id>`` are allowed to it."""
    resource = f"graph:{graph}"
    return (
        catalog_grants(actor, graph, policy).catalog_only
        and authorize(actor, "read.list", resource, _PROBE, policy).effect == "allow"
        and authorize(actor, "read.describe", resource, _PROBE, policy).effect == "allow"
    )


def effective_level(actor: ActorContext, graph: GraphId, policy: PolicySet) -> str:
    """What the actor can do on ``graph:<id>`` in one word: ``write`` when any write action is
    allowed next to some read, ``read`` when any read action is, else ``none``.

    ``graph_level`` demands every action of a level, so one denied read action (say
    ``read.function``) drops it to ``none`` for a caller who reads the graph; this is the level to
    tell that caller, with ``withheld_actions`` naming what is missing below it. A caller whose
    grants are catalog grants only is at ``catalog`` (see ``graph_level``)."""
    reached = graph_level(actor, graph, policy)
    if (
        reached in (ACCESS_LEVELS[-1], CATALOG_LEVEL)
        or catalog_grants(actor, graph, policy).catalog_only
    ):
        return reached  # a catalog-only caller was never granted a read to count
    resource = f"graph:{graph}"

    def holds(actions: Iterable[str]) -> bool:
        return any(authorize(actor, a, resource, _PROBE, policy).effect == "allow" for a in actions)

    reads = [a for a in level_actions("read") if not a.endswith("*")]
    if not holds(reads):
        return reached
    return "write" if holds(sorted(LEVEL_WRITE_ACTIONS)) else "read"


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
    ``none`` while the graph stays readable). A catalog-only caller was never granted the
    primitives, so a deny rule took nothing of them away: only a denied ``read.list`` or
    ``read.describe`` (what its grant does allow) is withheld from it, and, as in a class of
    the ladder, only while the other one is still allowed."""
    resource = f"graph:{graph}"
    if catalog_grants(actor, graph, policy).catalog_only:
        denied = [
            a
            for a in CATALOG_BASE_ACTIONS
            if authorize(actor, a, resource, _PROBE, policy).effect != "allow"
        ]
        return sorted(denied) if len(denied) < len(CATALOG_BASE_ACTIONS) else []
    ladder = [a for a in level_actions("write") if not a.endswith("*")] + sorted(
        LEVEL_WRITE_ACTIONS
    )
    withheld: list[str] = []
    for area in ("read.", "write."):
        actions = [a for a in ladder if a.startswith(area)]
        denied = [
            a for a in actions if authorize(actor, a, resource, _PROBE, policy).effect != "allow"
        ]
        if len(denied) < len(actions):
            withheld += denied
    return sorted(withheld)


@dataclass(frozen=True, slots=True)
class CatalogGrants:
    """What the allow rules that match an actor name of the entries of one graph.

    ``functions``, ``actions`` and ``views`` are the granted names, sorted and unique;
    ``catalog_only`` tells that every grant the actor has on the graph is a catalog grant
    (``level: catalog``): the actor holds no right on the graph itself beyond listing and
    describing it, so what its grants do not name does not exist for it. Names are the
    grants', whether the entries are published or a deny rule takes a right away."""

    functions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    views: tuple[str, ...] = ()
    catalog_only: bool = False
    # the ids of the allow rules that make the actor a reader of the graph (empty for a catalog-only
    # actor): what a ``whoami`` or an ``explain`` shows to say why the catalog limits nothing
    reader_rules: tuple[str, ...] = ()
    # the ids (``grant.<n>``) of the ``level: catalog`` grants that name entries of the graph for
    # the actor: told from a read grant with ``catalog.actions`` (a reader) by their base rule,
    # which allows nothing beyond listing and describing the graph
    catalog_rules: tuple[str, ...] = ()

    def names(self, kind: str) -> tuple[str, ...]:
        """The granted names of ``kind`` (``function``, ``action`` or ``view``)."""
        if kind == "function":
            return self.functions
        if kind == "action":
            return self.actions
        if kind == "view":
            return self.views
        raise ValueError(f"unknown entry kind {kind!r}")

    def __bool__(self) -> bool:
        return bool(self.functions or self.actions or self.views)


_ENTRY_KIND_OF: Final = ENTRY_PLURALS


def catalog_grants(actor: ActorContext, graph: GraphId, policy: PolicySet) -> CatalogGrants:
    """The catalog entries of ``graph`` that the grants of ``actor`` name, and whether it is
    *catalog-only* there.

    Decided from the compiled allow rules whose principal matches the actor as ``authorize``
    matches it (``allow_rule_holds``), never from a decision: a rule that names an entry
    resource of the graph (``graph:<id>/function:<name>``, ``/action:`` or ``/view:``) grants
    that name, and a rule that covers ``graph:<id>`` itself with any action beyond listing and
    describing it is a grant of the graph proper (a ``read`` or ``write`` level, the
    maintainers' rule). The actor is catalog-only when it has entry grants and no grant of the
    graph proper. A deny rule is no grant, so a ``read.get`` deny never turns a reader into a
    catalog caller. Pure and cheap (no decision is made): cache it per policy hash and actor."""
    resource = f"graph:{graph}"
    prefixes = {kind: f"{resource}/{kind}:" for kind in _ENTRY_KIND_OF}
    found: dict[str, set[str]] = {kind: set() for kind in _ENTRY_KIND_OF}
    proper: set[str] = set()
    entry_grants: set[str] = set()  # ids of the grants whose entry rules name an entry here
    base_only: set[str] = set()  # rules that allow nothing beyond listing and describing
    for document in policy.documents:
        for rule in rules(document):
            if rule.get("effect") != "allow" or not allow_rule_holds(rule, actor):
                continue
            patterns = strings(rule, "resource")
            if all(a in CATALOG_BASE_ACTIONS for a in strings(rule, "action")):
                base_only.add(str(rule.get("id")))
            for kind, prefix in prefixes.items():
                # a rule grants the entry only through an action that covers the action of its
                # kind (a hand-built rule may name another one, which authorize never allows)
                if not any(glob(a, ENTRY_KIND_ACTIONS[kind]) for a in strings(rule, "action")):
                    continue
                for pattern in patterns:
                    name = pattern[len(prefix) :]
                    if pattern.startswith(prefix) and ENTRY_NAME_RE.match(name):
                        found[kind].add(name)
                        entry_grants.add(str(rule.get("id")).rsplit(".", 1)[0])
            if any(glob(p, resource) for p in patterns) and any(
                a not in CATALOG_BASE_ACTIONS for a in strings(rule, "action")
            ):
                proper.add(str(rule.get("id")))
    names = {_ENTRY_KIND_OF[kind]: tuple(sorted(found[kind])) for kind in found}
    return CatalogGrants(
        functions=names["functions"],
        actions=names["actions"],
        views=names["views"],
        catalog_only=any(found.values()) and not proper,
        reader_rules=tuple(sorted(proper)) if any(found.values()) else (),
        catalog_rules=tuple(sorted(entry_grants & base_only)),
    )


@dataclass(frozen=True, slots=True, order=True)
class CatalogOverlap:
    """A catalog grant (``catalog`` rule id, say ``grant.2``) on ``graph`` whose callers are
    certainly also covered by a rule that grants the graph itself (``other``)."""

    graph: str
    catalog: str
    other: str


_CATALOG_RULE_RE: Final = re.compile(r"^(grant\.[0-9]+)\.(?:functions|views|actions)$")


def catalog_overlaps(documents: Iterable[Mapping[str, JsonValue]]) -> tuple[CatalogOverlap, ...]:
    """The catalog grants of the installation layer of ``documents`` that another grant of the
    same graph certainly covers: an allow rule that covers ``graph:<id>`` with an action beyond
    listing and describing, whose principal matches every actor the catalog grant's does (it
    names no fact the catalog grant lacks and each of its values contains the grant's). Rights
    add up, so for those callers the grant limits nothing: its ``reads`` and ``markings`` count
    for every read, not only inside its entries. Only certain cases are found (a rule with an
    ``unless`` condition is skipped; whether two different roles meet in one token is a question
    of tokens, not of the policy). Pure over the documents; sorted."""
    found: set[CatalogOverlap] = set()
    for document in documents:
        if layer_of(document) != INSTALLATION_LAYER:
            continue
        allows = [r for r in rules(document) if r.get("effect") == "allow"]
        for rule in allows:
            matched = _CATALOG_RULE_RE.match(str(rule.get("id")))
            if matched is None:
                continue
            base = next((r for r in allows if r.get("id") == matched.group(1)), None)
            if base is None or any(a not in CATALOG_BASE_ACTIONS for a in strings(base, "action")):
                continue  # a read grant with catalog.actions is a reader: it limits nothing anyway
            graphs = {
                m.group(1)
                for p in strings(rule, "resource")
                if (m := _ENTRY_GRAPH_RE.match(p)) is not None
            }
            for graph in graphs:
                for other in allows:
                    other_id = str(other.get("id"))
                    if other_id == matched.group(1) or other_id.startswith(f"{matched.group(1)}."):
                        continue
                    if (
                        "unless" not in other
                        and any(glob(p, f"graph:{graph}") for p in strings(other, "resource"))
                        and any(a not in CATALOG_BASE_ACTIONS for a in strings(other, "action"))
                        and _principal_implies(rule.get("principal"), other.get("principal"))
                    ):
                        found.add(CatalogOverlap(graph, matched.group(1), other_id))
    return tuple(sorted(found))


_ENTRY_GRAPH_RE: Final = re.compile(r"^graph:([^/]+)/(?:function|action|view):")


def _principal_implies(narrow: JsonValue, wide: JsonValue) -> bool:
    """Whether every actor whose facts match the principal ``narrow`` also matches ``wide``:
    ``wide`` names no fact ``narrow`` lacks, and each value of ``narrow`` is among those of
    ``wide``."""
    near = narrow if isinstance(narrow, Mapping) else {}
    far = wide if isinstance(wide, Mapping) else {}
    for key, expected in far.items():
        if key not in near:
            return False
        found = near[key]
        have = found if isinstance(found, list) else [found]
        wanted = expected if isinstance(expected, list) else [expected]
        if not all(value in wanted for value in have):
            return False
    return True


def write_obligations(
    actor: ActorContext, graph: GraphId, policy: PolicySet
) -> dict[str, JsonValue]:
    """``{action: {require_reason?, max_batch?}}`` of the writes allowed on ``graph:<id>``.

    A write action denied on the graph itself may still be allowed on the types a write
    allow list names; the first such allowed type gives the obligations, so a limited writer
    sees its ``require_reason`` and ``max_batch`` too."""
    found: dict[str, JsonValue] = {}
    granted = write_grant_types(actor, graph, policy)
    for action in sorted(LEVEL_WRITE_ACTIONS):
        resources = [f"graph:{graph}", *(f"graph:{graph}/type:{t}" for t in granted)]
        decision = None
        for resource in resources:
            probe = authorize(actor, action, resource, _PROBE, policy)
            if probe.effect == "allow":
                decision = probe
                break
        if decision is None:
            continue
        entry = _obligation_entry(decision)
        if entry:
            found[action] = entry
    return found


def entry_write_obligations(
    actor: ActorContext, graph: GraphId, entered: Decision, policy: PolicySet
) -> dict[str, JsonValue]:
    """``{action: {require_reason?, max_batch?}}`` of the writes an action the caller's grants name
    asks on ``graph:<id>``: the write actions are decided deny-only through ``authorize_entry``
    (``entered`` is the allow of ``authorize`` on the action's entry resource, made under
    ``policy`` for ``actor``), so the obligations shown are the ones the entry path will hold the
    ops to, whatever allow rules the caller lacks by hand. A write a rule denies is left out. The
    entry decision's own obligation is listed under its action (``write.action``)."""
    found: dict[str, JsonValue] = {}
    own = _obligation_entry(entered)
    if own:
        found[str(entered.action)] = own
    for action in sorted(LEVEL_WRITE_ACTIONS):
        decision = authorize_entry(actor, entered, action, f"graph:{graph}", _PROBE, policy)
        entry = _obligation_entry(decision) if decision.effect == "allow" else {}
        if entry:
            found[action] = entry
    return found


def _obligation_entry(decision: Decision) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {}
    if decision.obligations.require_reason:
        entry["require_reason"] = True
    if decision.obligations.max_batch is not None:
        entry["max_batch"] = decision.obligations.max_batch
    return entry


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
                or not any(is_term_write_glob(a) for a in strings(rule, "action"))
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


def held_markings(
    actor: ActorContext, graph: GraphId, policy: PolicySet, markings: Iterable[str]
) -> frozenset[str]:
    """The markings of ``markings`` ``actor`` holds in ``graph`` (field visibility plan F4): the
    ones for which ``authorize`` allows ``read.get`` on ``marking:<graph>/<curie>``, one probe
    per marking and none recorded (like the type probes of ``visibility``). ``markings`` are
    the markings the graph's pinned model uses; a name outside the marking grammar is never
    held (fail closed). Nothing is held without a rule that names the marking: no grant of a
    graph, scope or deny rule covers a marking resource, and the project-admin ceiling refuses
    it, so only the maintainer rules (``*`` on ``*``) and ``markings`` grants allow it. The
    caller caches the answer per policy hash, actor facts and pin."""
    held: set[str] = set()
    for marking in markings:
        resource = marking_resource(graph, marking)
        if (
            resource is not None
            and authorize(actor, "read.get", resource, _PROBE, policy).effect == "allow"
        ):
            held.add(marking)
    return frozenset(held)


def holds_every_marking(actor: ActorContext, graph: GraphId, policy: PolicySet) -> bool:
    """Whether ``actor`` holds every marking of ``graph`` whatever its model declares, for a
    graph whose model cannot be read: only a rule that covers every resource (the maintainer's
    ``*`` on ``*``) allows ``read.get`` on a marking nobody declared, because a ``markings``
    grant names its markings one by one."""
    resource = marking_resource(graph, _UNDECLARED_MARKING)
    assert resource is not None
    return authorize(actor, "read.get", resource, _PROBE, policy).effect == "allow"


def marking_allowance(
    actor: ActorContext, graph: GraphId, policy: PolicySet, used: Iterable[str]
) -> list[str] | None:
    """What a caller may see of the marked attributes of ``graph``, for ``whoami`` and ``policy
    explain``: the sorted markings of ``used`` it holds, or ``None`` when there is nothing to say
    (no marking is used, or it holds every one: it sees every marked attribute). An empty list
    tells that something is marked and the caller holds none of it; a marking the caller lacks
    is never named."""
    markings = frozenset(used)
    if not markings:
        return None
    held = held_markings(actor, graph, policy, markings)
    return None if held == markings else sorted(held)


def visible_limits(limits: Mapping[str, JsonValue], classes: Iterable[str]) -> dict[str, JsonValue]:
    """The type limits of ``type_limits`` that name a type of ``classes`` (the classes a caller
    sees, abstract ones included), for a caller who sees only some types: a limit on a hidden
    type, or on a prefix none of whose types is seen, is left out, so no hidden type is named.
    A key is a CURIE or ``prefix:*`` (a glob by prefix, as in a rule)."""
    names = tuple(classes)
    return {
        key: value
        for key, value in limits.items()
        if any(name.startswith(key[:-1]) if key.endswith("*") else name == key for name in names)
    }


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
