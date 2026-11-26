"""What a caller may do with a published function, action or view: the three-way split
``allowed | not_allowed(missing) | hidden`` (graph start plan S4; pure, no I/O, no audit).

- **hidden**: the item names something the caller cannot see (a type, relation, attribute,
  enum, module or function outside its read view). It is not part of any classified result,
  so nothing built on the result can name, count or suggest it. Hidden depends on the read
  view only, never on the caller's rights.
- **outside the catalog**: for a caller whose grants on the graph are catalog grants only
  (``access_control.effective.catalog_grants``), an entry its grants do not name is hidden too.
  That is a second layer, applied after the read-view layer and kept apart from its cache:
  ``CatalogFilter`` is cached per (policy hash, graph, actor key), so a catalog caller and a
  full reader with the same read view never share a filtered result.
- **allowed**: the caller sees everything the item names and holds every right it needs.
- **not allowed**: the caller sees everything the item names but lacks a right. ``missing``
  names the unmet rights; each is a right name that is public (``whoami`` lists the actions a
  caller is denied) on a resource the caller sees.

Rights are probed, never decided on the record: classifying records no denial. Obligations
(``require_reason``, ``max_batch``) never make an item not allowed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph_cli.access_control.effective import CatalogGrants, catalog_grants
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry, Named
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.services.visibility import actor_key, remember

GRAPH_RESOURCE = "graph"


@dataclass(frozen=True, slots=True)
class Missing:
    """One unmet right: the action and the resource it was needed on, graph-relative
    (``graph``, ``type:<curie>``, ``type:<curie>/attr:<name>``, ``type:<curie>/rel:<curie>``)."""

    action: str
    resource: str = GRAPH_RESOURCE

    def to_json(self) -> dict[str, JsonValue]:
        return {"action": self.action, "resource": self.resource}

    def __str__(self) -> str:
        return f"{self.action} on {self.resource}"


def relative_resource(resource: str) -> str:
    """``graph:<id>/type:ex:T`` as ``type:ex:T``, ``graph:<id>`` as ``graph``; a resource of
    another shape is returned unchanged."""
    if not resource.startswith("graph:"):
        return resource
    _, slash, rest = resource.partition("/")
    return rest if slash else GRAPH_RESOURCE


def missing_of(decision: Decision) -> Missing:
    """The unmet right a denying decision stands for."""
    return Missing(str(decision.action), relative_resource(decision.resource))


def first_denial(decisions: Iterable[Decision]) -> Decision | None:
    """The first denying decision among ``decisions`` (``None`` when all of them allow)."""
    for decision in decisions:
        if decision.effect != "allow":
            return decision
    return None


@dataclass(frozen=True, slots=True)
class Classified[Entry]:
    """A catalog entry the caller sees, with the decisions that deny it (empty: allowed).

    The decisions are the probes that failed, kept so a call of the entry can record the
    denial it stands for; ``missing`` is what a caller is told."""

    entry: Entry
    denials: tuple[Decision, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        return not self.denials

    @property
    def missing(self) -> tuple[Missing, ...]:
        return tuple(dict.fromkeys(missing_of(d) for d in self.denials))

    def missing_json(self) -> list[JsonValue]:
        return [m.to_json() for m in self.missing]

    def access_json(self) -> dict[str, JsonValue]:
        """The marks of an entry that is not allowed (none for an allowed one, which stays as
        it was): ``allowed: false`` and ``missing``."""
        if self.allowed:
            return {}
        return {"allowed": False, "missing": self.missing_json()}


def allowed_entries[Entry](classified: Sequence[Classified[Entry]]) -> tuple[Entry, ...]:
    """The entries the caller may use."""
    return tuple(c.entry for c in classified if c.allowed)


def not_allowed[Entry](classified: Sequence[Classified[Entry]]) -> tuple[Classified[Entry], ...]:
    """The entries the caller sees but may not use, still classified."""
    return tuple(c for c in classified if not c.allowed)


@dataclass(frozen=True, slots=True)
class Denied:
    """The not-allowed entries of a graph for one caller, per kind (what the listings count and
    the search shows next to the allowed ones). Never holds a hidden entry."""

    functions: tuple[Classified[FunctionEntry], ...] = ()
    actions: tuple[Classified[ActionEntry], ...] = ()
    views: tuple[Classified[ViewEntry], ...] = ()

    @classmethod
    def of(
        cls,
        functions: Sequence[Classified[FunctionEntry]],
        actions: Sequence[Classified[ActionEntry]],
        views: Sequence[Classified[ViewEntry]],
    ) -> Denied:
        """The not-allowed entries of the three classifications (allowed ones are left out)."""
        return cls(not_allowed(functions), not_allowed(actions), not_allowed(views))

    def count(self) -> int:
        return len(self.functions) + len(self.actions) + len(self.views)


_FILTER_LIMIT: Final = 256


class CatalogFilter:
    """Which entries of a graph a caller whose grants are catalog grants only may see.

    The names come from ``catalog_grants`` over the compiled policy (no decision is made) and are
    cached per (policy hash, graph, actor key): the key holds every fact an allow rule matches, so
    two actors no rule can tell apart share an answer and none shares one it must not. The cache
    is bounded (the oldest entries go first), as a server sees an unbounded number of asserted
    session and agent ids. It is not part of ``HiddenCatalog``, whose answer depends on the read
    view alone."""

    def __init__(self) -> None:
        self._found: dict[tuple[object, ...], CatalogGrants] = {}

    def grants(self, actor: ActorContext, graph: GraphId, policy: PolicySet) -> CatalogGrants:
        """What ``actor``'s grants name of ``graph``'s catalog, and whether it is catalog-only."""
        key = (policy.hash, graph, *actor_key(actor))
        found = self._found.get(key)
        if found is None:
            found = catalog_grants(actor, graph, policy)
            remember(self._found, key, found, _FILTER_LIMIT)
        return found

    def scope(self, actor: ActorContext, graph: GraphId, policy: PolicySet) -> CatalogGrants | None:
        """The grants that bound what ``actor`` sees of the catalog, ``None`` for a caller not
        limited to its catalog (nothing is filtered then)."""
        found = self.grants(actor, graph, policy)
        return found if found.catalog_only else None


def narrow[Entry: Named](
    entries: Sequence[Entry], scope: CatalogGrants | None, kind: str
) -> tuple[Entry, ...]:
    """``entries`` of ``kind`` (``function``, ``action`` or ``view``) that fit ``scope``: all of
    them without one, else those its grants name."""
    if scope is None:
        return tuple(entries)
    named = frozenset(scope.names(kind))
    return tuple(e for e in entries if e.name in named)
