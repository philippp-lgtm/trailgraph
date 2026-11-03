"""Which published actions and functions take an entity as a parameter (T5).

``actions_for`` and ``functions_for`` are pure: the caller passes the entries the actor may
see (``describe actions`` / ``describe functions``), so hidden entries stay hidden. An entry
matches through each of its ``ref`` parameters whose class covers the entity's class
(``model.subtypes``), one affordance per parameter, in catalog order, at most
``MAX_ENTRIES`` per group (the rest is counted in ``<group>_more``).

An action's ``applies`` is judged from the preconditions that are on the matching parameter
(``judge_precondition``, as ``check_preconditions``), on the entity's current revision. The
preconditions on other parameters need values that are not known; they are named in
``unchecked``. ``applies: true`` therefore means no known obstacle, not a promise. Functions
carry no state judgment. An entity that is not active has no affordances.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import Revision
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.expand import judge_precondition
from graph_cli.kinetic.params import ParamDef
from graph_cli.ontology_layer.affordances import ACTIVE, capped, with_more
from graph_cli.ontology_layer.model import CompiledModel


@dataclass(frozen=True, slots=True)
class Blocker:
    precondition: str
    summary: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"precondition": self.precondition, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class ActionAffordance:
    action: str
    parameter: str  # the parameter the entity would fill
    applies: bool
    blocked_by: tuple[Blocker, ...]
    unchecked: tuple[str, ...]  # ids of preconditions on other parameters

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "action": self.action,
            "as": self.parameter,
            "applies": self.applies,
            "blocked_by": [b.to_json() for b in self.blocked_by],
            "unchecked": list(self.unchecked),
        }


@dataclass(frozen=True, slots=True)
class ActionAffordances:
    entries: tuple[ActionAffordance, ...]
    more: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        return with_more("actions", [e.to_json() for e in self.entries], self.more)


@dataclass(frozen=True, slots=True)
class FunctionAffordance:
    function: str
    parameter: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"function": self.function, "as": self.parameter}


@dataclass(frozen=True, slots=True)
class FunctionAffordances:
    entries: tuple[FunctionAffordance, ...]
    more: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        return with_more("functions", [e.to_json() for e in self.entries], self.more)


def actions_for(
    entity: Revision, entries: Sequence[ActionEntry], snapshot: Snapshot, model: CompiledModel
) -> ActionAffordances:
    """The actions that take ``entity`` as a ``ref`` parameter, with what is known to block them."""
    if entity.record_status != ACTIVE:
        return ActionAffordances(())
    found: list[ActionAffordance] = []
    for entry in entries:
        for param in _matching(entity, entry.defn.params, model):
            blocked: list[Blocker] = []
            unchecked: list[str] = []
            for pre in entry.preconditions:
                if pre.on != param.name:
                    unchecked.append(pre.id)
                elif judge_precondition(pre, entity, snapshot, model) is not None:
                    blocked.append(Blocker(pre.id, pre.summary))
            found.append(
                ActionAffordance(
                    entry.name, param.name, not blocked, tuple(blocked), tuple(unchecked)
                )
            )
    kept, more = capped(found)
    return ActionAffordances(kept, more)


def functions_for(
    entity: Revision, entries: Sequence[FunctionEntry], model: CompiledModel
) -> FunctionAffordances:
    """The functions that take ``entity`` as a ``ref`` parameter."""
    if entity.record_status != ACTIVE:
        return FunctionAffordances(())
    found = [
        FunctionAffordance(entry.name, param.name)
        for entry in entries
        for param in _matching(entity, entry.defn.params, model)
    ]
    kept, more = capped(found)
    return FunctionAffordances(kept, more)


def _matching(entity: Revision, params: Sequence[ParamDef], model: CompiledModel) -> list[ParamDef]:
    """The ``ref`` parameters whose class is the entity's class or one of its ancestors."""
    return [
        p
        for p in params
        if p.type == "ref" and p.of_class is not None and entity.type in model.subtypes(p.of_class)
    ]
