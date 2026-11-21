"""What a caller may do with a published function, action or view: the three-way split
``allowed | not_allowed(missing) | hidden`` (graph start plan S4; pure, no I/O, no audit).

- **hidden**: the item names something the caller cannot see (a type, relation, attribute,
  enum, module or function outside its read view). It is not part of any classified result,
  so nothing built on the result can name, count or suggest it. Hidden depends on the read
  view only, never on the caller's rights.
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

from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue

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


def first_denied(decisions: Iterable[Decision]) -> Missing | None:
    """The first unmet right among ``decisions`` (``None`` when all of them allow)."""
    for decision in decisions:
        if decision.effect != "allow":
            return missing_of(decision)
    return None


@dataclass(frozen=True, slots=True)
class Classified[Entry]:
    """A catalog entry the caller sees, with the rights it lacks (empty: allowed)."""

    entry: Entry
    missing: tuple[Missing, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        return not self.missing

    def missing_json(self) -> list[JsonValue]:
        return [m.to_json() for m in self.missing]


def allowed_entries[Entry](classified: Sequence[Classified[Entry]]) -> tuple[Entry, ...]:
    """The entries the caller may use."""
    return tuple(c.entry for c in classified if c.allowed)


def not_allowed_entries[Entry](classified: Sequence[Classified[Entry]]) -> tuple[Entry, ...]:
    """The entries the caller sees but may not use."""
    return tuple(c.entry for c in classified if not c.allowed)
