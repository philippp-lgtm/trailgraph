"""What an entity can do next in the model: lifecycle transitions and relation room (T5).

Two pure projections over one current revision, its ``Snapshot`` and the compiled model:
``transitions`` lists, per lifecycle attribute of the entity's class, the transitions out
of the current state with the result of their guards; ``relations`` lists, per relation
that can start at the entity (``out``) or end at it (``in``), how many active edges it has
and how many the cardinality still takes. Nothing is stored or filtered by rights.

Both return a result with ``to_json``: the keys to merge into the ``affordances`` object of
``graph get``. Each group holds at most ``MAX_ENTRIES`` entries; the rest is counted in a
``<group>_more`` key (present only when something was cut). An entity that is not active
(superseded, retracted) has nothing that applies: empty groups.

Edge counts: ``out`` counts the edges in the entity's revision; ``in`` counts the edges of
``snapshot.incoming`` whose source is active, as ``expand`` does for ``unlink all``. The
relations of the ``core`` module (provenance) are not listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.evaluate import evaluate
from graph_cli.ontology_layer.model import CompiledModel, RelationDef

MAX_ENTRIES: Final = 40
ACTIVE: Final = "active"
CORE_PREFIX: Final = "core:"

Direction = Literal["out", "in"]


def capped[T](items: list[T]) -> tuple[tuple[T, ...], int]:
    """The first ``MAX_ENTRIES`` items and the number cut off."""
    return tuple(items[:MAX_ENTRIES]), max(0, len(items) - MAX_ENTRIES)


def with_more(key: str, items: list[JsonValue], more: int) -> dict[str, JsonValue]:
    """``{key: items}``, plus ``{key + "_more": more}`` when entries were cut."""
    found: dict[str, JsonValue] = {key: items}
    if more:
        found[f"{key}_more"] = more
    return found


@dataclass(frozen=True, slots=True)
class FailedGuard:
    guard: str
    summary: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"guard": self.guard, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class TransitionTarget:
    state: str
    allowed: bool
    blocked_by: tuple[FailedGuard, ...] = ()

    def to_json(self) -> dict[str, JsonValue]:
        found: dict[str, JsonValue] = {"state": self.state, "allowed": self.allowed}
        if self.blocked_by:
            found["blocked_by"] = [g.to_json() for g in self.blocked_by]
        return found


@dataclass(frozen=True, slots=True)
class LifecycleTransitions:
    attr: str
    lifecycle: str
    from_state: str | None
    to: tuple[TransitionTarget, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "attr": self.attr,
            "lifecycle": self.lifecycle,
            "from": self.from_state,
            "to": [t.to_json() for t in self.to],
        }


@dataclass(frozen=True, slots=True)
class TransitionAffordances:
    entries: tuple[LifecycleTransitions, ...]
    more: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        return with_more("transitions", [e.to_json() for e in self.entries], self.more)


@dataclass(frozen=True, slots=True)
class RelationRoom:
    relation: str
    direction: Direction
    count: int
    min: int
    max: int | None
    room: int | None  # None: unbounded

    @property
    def missing(self) -> int:
        return max(0, self.min - self.count)

    def to_json(self) -> dict[str, JsonValue]:
        found: dict[str, JsonValue] = {
            "relation": self.relation,
            "direction": self.direction,
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "room": self.room,
        }
        if self.missing:
            found["missing"] = self.missing
        return found


@dataclass(frozen=True, slots=True)
class RelationAffordances:
    entries: tuple[RelationRoom, ...]  # room left, or unbounded
    more: int = 0
    full: tuple[str, ...] = ()  # relations at their maximum, names only
    full_more: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        found = with_more("relations", [e.to_json() for e in self.entries], self.more)
        found.update(with_more("full", list(self.full), self.full_more))
        return found


def transitions(
    model: CompiledModel, revision: Revision, snapshot: Snapshot
) -> TransitionAffordances:
    """The transitions out of the current state of every lifecycle attribute of the entity."""
    cls = model.classes.get(revision.type)
    if revision.record_status != ACTIVE or cls is None:
        return TransitionAffordances(())
    found: list[LifecycleTransitions] = []
    for name in sorted(cls.attributes):
        attribute = cls.attributes[name]
        if attribute.kind != "lifecycle":
            continue
        lifecycle = model.lifecycles[attribute.type]
        value = revision.attrs.get(name)
        state = value if isinstance(value, str) else None
        targets: list[TransitionTarget] = []
        for transition in lifecycle.transitions:
            if state is None or transition.from_state != state:
                continue
            failed = tuple(
                FailedGuard(g.id, g.summary)
                for g in transition.guards
                if not evaluate(g.expr, revision, snapshot, model)
            )
            targets.append(TransitionTarget(transition.to_state, not failed, failed))
        found.append(LifecycleTransitions(name, attribute.type, state, tuple(targets)))
    entries, more = capped(found)
    return TransitionAffordances(entries, more)


def relations(model: CompiledModel, revision: Revision, snapshot: Snapshot) -> RelationAffordances:
    """Per relation the entity's class (or an ancestor) can take: edge count, bounds, room."""
    if revision.record_status != ACTIVE:
        return RelationAffordances(())
    rooms: list[RelationRoom] = []
    for name in sorted(model.relations):
        if name.startswith(CORE_PREFIX):
            continue
        relation = model.relations[name]
        if model.is_a_any(revision.type, relation.domain):
            count = sum(1 for e in revision.edges if e.rel == name)
            rooms.append(_room(name, "out", count, relation.out.min, relation.out.max))
        if model.is_a_any(revision.type, relation.range):
            rooms.append(_room(name, "in", _incoming(snapshot, revision, name), *_into(relation)))
    open_rooms = [r for r in rooms if r.room != 0]
    full = sorted({r.relation for r in rooms if r.room == 0})
    entries, more = capped(open_rooms)
    names, full_more = capped(full)
    return RelationAffordances(entries, more, names, full_more)


def _into(relation: RelationDef) -> tuple[int, int | None]:
    return relation.into.min, relation.into.max


def _room(name: str, direction: Direction, count: int, low: int, high: int | None) -> RelationRoom:
    room = None if high is None else max(0, high - count)
    return RelationRoom(name, direction, count, low, high, room)


def _incoming(snapshot: Snapshot, revision: Revision, name: str) -> int:
    count = 0
    for source, _ in snapshot.incoming(revision.id, TermRef(name)):
        found = snapshot.get(source)
        if found is not None and found.record_status == ACTIVE:
            count += 1
    return count
