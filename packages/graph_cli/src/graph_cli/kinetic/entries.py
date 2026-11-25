"""The entries of a compiled function catalog as the app shows them (plan K8, K9).

``load_entries`` reads the catalog document ``graph_cli.functions/1`` that
``compile_catalog`` wrote into typed entries; ``summary_json`` and
``detail_json`` are the shapes of ``graph describe functions`` and
``graph describe function``. Pure: no storage, no model.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str
from graph_cli.kinetic.format import FunctionDef, ParamDef
from graph_cli.kinetic.return_types import ColumnType

DEPRECATED: Final = "deprecated"


class Named(Protocol):
    """A catalog entry: a function, an action or a view."""

    @property
    def name(self) -> str: ...


def seen_replacement(replaced_by: str | None, visible: Iterable[Named] | None) -> str | None:
    """The entry a deprecated one was replaced by, only when the reader sees that one too
    (``visible``: the entries it may see, ``None`` for none): a name the reader cannot see is
    not told in a listing, a description or a warning."""
    if replaced_by is None or all(entry.name != replaced_by for entry in visible or ()):
        return None
    return replaced_by


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

    def detail_json(self, types: Sequence[ColumnType | None] | None = None) -> dict[str, JsonValue]:
        """Everything of the function format plus the normalized query text and the hash.
        ``types`` are the types of the columns against the caller's model
        (``compile.column_types``), shown as ``type`` (``null``: untyped), ``class``, ``enum``,
        ``many`` and ``nullable`` of each column; without them the columns show what the author
        declared."""
        d = self.defn
        found = types if types is not None else [r.declared for r in d.returns]
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
            "returns": [
                {"column": r.column, "summary": r.summary, **column_type_json(t)}
                for r, t in zip(d.returns, found, strict=True)
            ],
            "examples": [{"question": e.question, "params": dict(e.params)} for e in d.examples],
            "touches": {
                "types": [{"curie": t.curie, "module": t.module} for t in self.types],
                "relations": [{"curie": t.curie, "module": t.module} for t in self.relations],
            },
            "query": self.query,
            "hash": self.hash,
        }


def column_type_json(column: ColumnType | None) -> dict[str, JsonValue]:
    """The type keys of a column in ``describe function``; empty ones when it is untyped."""
    if column is None:
        return {"type": None, "class": None, "enum": None, "many": False, "nullable": True}
    return dict(column.to_json())


def signature(params: Sequence[ParamDef]) -> str:
    return ", ".join(p.signature for p in params)


def find_entry(functions: Sequence[FunctionEntry] | None, name: str) -> FunctionEntry:
    """The function called ``name``; ``function.not_found`` with ``did_you_mean`` otherwise."""
    for f in functions or ():
        if f.name == name:
            return f
    names = sorted(f.name for f in functions or ())
    raise GraphCliError(
        "function.not_found",
        f"no function {name!r}",
        details={"name": name, "did_you_mean": difflib.get_close_matches(name, names, n=5)},
    )


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
                types=touched(touches, "types"),
                relations=touched(touches, "relations"),
                hash=req_str(entry, "hash"),
            )
        )
    return tuple(found)


def touched(touches: JsonObject, key: str) -> tuple[Touched, ...]:
    """The ``key`` list of a catalog entry's ``touches`` (types or relations)."""
    out: list[Touched] = []
    for value in req_list(touches, key, "touches."):
        item = as_object(value, key)
        module = item.get("module")
        out.append(Touched(req_str(item, "curie"), module if isinstance(module, str) else None))
    return tuple(out)
