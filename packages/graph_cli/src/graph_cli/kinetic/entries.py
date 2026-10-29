"""The entries of a compiled function catalog as the app shows them (plan K8, K9).

``load_entries`` reads the catalog document ``graph_cli.functions/1`` that
``compile_catalog`` wrote into typed entries; ``summary_json`` and
``detail_json`` are the shapes of ``graph describe functions`` and
``graph describe function``. Pure: no storage, no model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str
from graph_cli.kinetic.format import FunctionDef, ParamDef

DEPRECATED: Final = "deprecated"


@dataclass(frozen=True, slots=True)
class Touched:
    """A type or relation a function's query names, with the module that owns it."""

    curie: str
    module: str | None


@dataclass(frozen=True, slots=True)
class FunctionEntry:
    """One published function: its declaration, the normalized query text and its hash."""

    defn: FunctionDef  # ``defn.query`` is the authored text
    module: str | None  # the module id
    query: str  # normalized: CURIEs, backticked
    types: tuple[Touched, ...]
    relations: tuple[Touched, ...]
    hash: str

    @property
    def name(self) -> str:
        return self.defn.name

    @property
    def deprecated(self) -> bool:
        return self.defn.status == DEPRECATED

    @property
    def signature(self) -> str:
        """``unit: ref org:OrgUnit, include_vacant?: boolean``."""
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
        """Everything of the function format plus the normalized query text and the hash."""
        d = self.defn
        return {
            "name": self.name,
            "module": self.module,
            "summary": d.summary,
            "use_when": d.use_when,
            "status": d.status,
            "replaced_by": d.replaced_by,
            "featured": d.featured,
            "signature": self.signature,
            "params": [p.to_json() for p in d.params],
            "returns": [{"column": r.column, "summary": r.summary} for r in d.returns],
            "examples": [{"question": e.question, "params": dict(e.params)} for e in d.examples],
            "touches": {
                "types": [{"curie": t.curie, "module": t.module} for t in self.types],
                "relations": [{"curie": t.curie, "module": t.module} for t in self.relations],
            },
            "query": self.query,
            "hash": self.hash,
        }


def signature(params: Sequence[ParamDef]) -> str:
    return ", ".join(p.signature for p in params)


def load_entries(document: JsonObject) -> tuple[FunctionEntry, ...]:
    """The entries of a catalog document (schema checked by the slot reader), in its order.
    A document of another shape raises ``JsonShapeError``."""
    found: list[FunctionEntry] = []
    for value in req_list(document, "functions"):
        entry = as_object(value, "functions")
        touches = req_obj(entry, "touches", "functions.")
        module = entry.get("module")
        found.append(
            FunctionEntry(
                defn=FunctionDef.from_json(entry, req_str(entry, "source_query")),
                module=module if isinstance(module, str) else None,
                query=req_str(entry, "query"),
                types=_touched(touches, "types"),
                relations=_touched(touches, "relations"),
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
