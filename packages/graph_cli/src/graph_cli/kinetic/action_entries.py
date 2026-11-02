"""The entries of a compiled action catalog as the app shows them.

``load_action_entries`` reads the catalog document ``graph_cli.action_catalog/1`` that
``compile_actions`` wrote into typed entries; ``summary_json`` and
``detail_json`` are the shapes of the listing and of one action. Pure: no
storage, no model.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str
from graph_cli.kinetic.action_format import ActionDef
from graph_cli.kinetic.entries import Touched, signature

DEPRECATED: Final = "deprecated"


@dataclass(frozen=True, slots=True)
class PreconditionEntry:
    """A compiled precondition: the lowered condition ``expr`` evaluated on parameter ``on``."""

    id: str
    on: str
    summary: str
    expr: JsonValue

    def to_json(self) -> dict[str, JsonValue]:
        return {"id": self.id, "on": self.on, "summary": self.summary, "expr": self.expr}


@dataclass(frozen=True, slots=True)
class Need:
    """One ``write.*`` action on a type that the action's ops are authorized as."""

    action: str
    type: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"action": self.action, "type": self.type}


@dataclass(frozen=True, slots=True)
class ActionEntry:
    """One published action: its declaration, the compiled ops and preconditions, its hash."""

    defn: ActionDef
    module: str | None  # the module id
    ops: tuple[JsonObject, ...]  # normalized templates: CURIEs
    preconditions: tuple[PreconditionEntry, ...]
    types: tuple[Touched, ...]
    relations: tuple[Touched, ...]
    needs: tuple[Need, ...]
    hash: str

    @property
    def name(self) -> str:
        return self.defn.name

    @property
    def deprecated(self) -> bool:
        return self.defn.status == DEPRECATED

    @property
    def signature(self) -> str:
        """``person: ref ppl:Person, since: date``."""
        return signature(self.defn.params)

    def summary_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "module": self.module,
            "summary": self.defn.summary,
            "signature": self.signature,
            "status": self.defn.status,
        }

    def detail_json(self) -> dict[str, JsonValue]:
        """Everything of the action format plus the compiled ops, touches, needs and the hash."""
        d = self.defn
        return {
            "name": self.name,
            "module": self.module,
            "summary": d.summary,
            "use_when": d.use_when,
            "status": d.status,
            "replaced_by": d.replaced_by,
            "featured": d.featured,
            "reason": d.reason,
            "signature": self.signature,
            "params": [p.to_json() for p in d.params],
            "preconditions": [p.to_json() for p in self.preconditions],
            "examples": [e.to_json() for e in d.examples],
            "ops": [dict(o) for o in self.ops],
            "touches": {
                "types": [{"curie": t.curie, "module": t.module} for t in self.types],
                "relations": [{"curie": t.curie, "module": t.module} for t in self.relations],
            },
            "needs": [n.to_json() for n in self.needs],
            "hash": self.hash,
        }


def find_action(actions: Sequence[ActionEntry] | None, name: str) -> ActionEntry:
    """The action called ``name``; ``action.not_found`` with ``did_you_mean`` otherwise."""
    for entry in actions or ():
        if entry.name == name:
            return entry
    names = sorted(a.name for a in actions or ())
    raise GraphCliError(
        "action.not_found",
        f"no action {name!r}",
        details={"name": name, "did_you_mean": difflib.get_close_matches(name, names, n=5)},
    )


def load_action_entries(document: JsonObject) -> tuple[ActionEntry, ...]:
    """The entries of a catalog document (schema checked by the slot reader), in its order.
    A document of another shape raises ``JsonShapeError``."""
    found: list[ActionEntry] = []
    for value in req_list(document, "actions"):
        entry = as_object(value, "actions")
        touches = req_obj(entry, "touches", "actions.")
        module = entry.get("module")
        found.append(
            ActionEntry(
                defn=ActionDef.from_json(entry, req_obj(entry, "source", "actions.")),
                module=module if isinstance(module, str) else None,
                ops=tuple(as_object(o, "actions.ops") for o in req_list(entry, "ops", "actions.")),
                preconditions=tuple(
                    PreconditionEntry(
                        req_str(p, "id", "actions.preconditions."),
                        req_str(p, "on", "actions.preconditions."),
                        req_str(p, "summary", "actions.preconditions."),
                        p.get("expr"),
                    )
                    for p in (
                        as_object(v, "actions.preconditions")
                        for v in req_list(entry, "preconditions", "actions.")
                    )
                ),
                types=_touched(touches, "types"),
                relations=_touched(touches, "relations"),
                needs=_needs(entry),
                hash=req_str(entry, "hash"),
            )
        )
    return tuple(found)


def _touched(touches: JsonObject, key: str) -> tuple[Touched, ...]:
    out: list[Touched] = []
    for value in req_list(touches, key, "touches."):
        item = as_object(value, key)
        module = item.get("module")
        out.append(Touched(req_str(item, "curie"), module if isinstance(module, str) else None))
    return tuple(out)


def _needs(entry: JsonObject) -> tuple[Need, ...]:
    items = (as_object(v, "actions.needs") for v in req_list(entry, "needs", "actions."))
    return tuple(
        Need(req_str(n, "action", "actions.needs."), req_str(n, "type", "actions.needs."))
        for n in items
    )
