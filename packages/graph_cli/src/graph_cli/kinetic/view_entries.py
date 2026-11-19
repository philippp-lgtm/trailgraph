"""The entries of a compiled view catalog as the app shows them.

``load_view_entries`` reads the catalog document ``graph_cli.view_catalog/1`` that
``compile_views`` wrote into typed entries; ``summary_json`` and ``detail_json`` are the
shapes of the listing and of one view. Pure: no storage, no model.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str, str_list
from graph_cli.kinetic.entries import Touched, signature, touched
from graph_cli.kinetic.view_format import ViewDef

DEPRECATED: Final = "deprecated"


@dataclass(frozen=True, slots=True)
class ViewEntry:
    """One published view: its normalized declaration, the authored source it compiles from,
    what it touches and its hash."""

    defn: ViewDef  # normalized: CURIEs
    source: ViewDef  # as authored
    module: str | None  # the module id
    types: tuple[Touched, ...]
    relations: tuple[Touched, ...]
    attributes: tuple[str, ...]
    functions: tuple[str, ...]
    hash: str

    @property
    def name(self) -> str:
        return self.defn.name

    @property
    def deprecated(self) -> bool:
        return self.defn.status == DEPRECATED

    @property
    def entity(self) -> bool:
        """Whether the view takes an entity (else it is a set view)."""
        return self.defn.entity

    @property
    def takes(self) -> str | None:
        return self.defn.takes

    @property
    def signature(self) -> str:
        """``state: string, limit?: integer``; empty for a view without parameters."""
        return signature(self.defn.params)

    def summary_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": self.name,
            "module": self.module,
            "summary": self.defn.summary,
        }
        if self.defn.takes is not None:
            data["takes"] = self.defn.takes
        if self.defn.default:
            data["default"] = True
        data["signature"] = self.signature
        data["status"] = self.defn.status
        return data

    def detail_json(self) -> dict[str, JsonValue]:
        """Everything of the view format (normalized) plus what it touches and the hash."""
        d = self.defn
        return {
            "name": self.name,
            "module": self.module,
            "summary": d.summary,
            "use_when": d.use_when,
            "status": d.status,
            "replaced_by": d.replaced_by,
            "takes": d.takes,
            "default": d.default,
            "signature": self.signature,
            "params": [p.to_json() for p in d.params],
            "max_bytes": d.max_bytes,
            "page_bytes": d.page_bytes,
            "sections": [s.to_json() for s in d.sections],
            "examples": [{"question": e.question, "params": dict(e.params)} for e in d.examples],
            "touches": {
                "types": [{"curie": t.curie, "module": t.module} for t in self.types],
                "relations": [{"curie": t.curie, "module": t.module} for t in self.relations],
                "attributes": list(self.attributes),
                "functions": list(self.functions),
            },
            "hash": self.hash,
        }


def find_view(views: Sequence[ViewEntry] | None, name: str) -> ViewEntry:
    """The view called ``name``; ``view.not_found`` with ``did_you_mean`` otherwise."""
    for entry in views or ():
        if entry.name == name:
            return entry
    names = sorted(v.name for v in views or ())
    raise GraphCliError(
        "view.not_found",
        f"no view {name!r}",
        details={"name": name, "did_you_mean": difflib.get_close_matches(name, names, n=5)},
    )


def load_view_entries(document: JsonObject) -> tuple[ViewEntry, ...]:
    """The entries of a catalog document (schema checked by the slot reader), in its order.
    A document of another shape raises ``JsonShapeError``."""
    found: list[ViewEntry] = []
    for value in req_list(document, "views"):
        entry = as_object(value, "views")
        touches = req_obj(entry, "touches", "views.")
        module = entry.get("module")
        found.append(
            ViewEntry(
                defn=ViewDef.from_json(entry),
                source=ViewDef.from_json(req_obj(entry, "source", "views.")),
                module=module if isinstance(module, str) else None,
                types=touched(touches, "types"),
                relations=touched(touches, "relations"),
                attributes=str_list(touches, "attributes", "touches."),
                functions=str_list(touches, "functions", "touches."),
                hash=req_str(entry, "hash"),
            )
        )
    return tuple(found)
