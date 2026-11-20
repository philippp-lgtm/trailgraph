"""The model documents of a graph, read into one immutable ``Model``.

``Model.of`` takes the ``graph_cli.model/1`` documents (what ``graph describe`` prints) and
the graph block of the manifest, checks their schemas and keeps what the blocks of the skill
render: the types, relations and modules, and the active entries of the compiled function,
action and view catalogs (deprecated entries are left out everywhere). The catalogs are
separate documents with their own schemas; the reader takes only their public shape and
never imports ``graph_cli.kinetic``. A ``ValueError`` names the document it cannot read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.json_access import as_list, as_object, opt_str, req_list, req_obj, req_str

MODEL_SCHEMA: Final = "graph_cli.model/1"
FUNCTIONS_SCHEMA: Final = "graph_cli.functions/1"  # the compiled catalog, ``graph_cli.kinetic``
ACTIONS_SCHEMA: Final = "graph_cli.action_catalog/1"  # the compiled action catalog
VIEWS_SCHEMA: Final = "graph_cli.view_catalog/1"  # the compiled view catalog


@dataclass(frozen=True, slots=True)
class Model:
    name: str
    title: str | None
    language: str | None  # the graph language of the manifest (BCP 47)
    blocks: frozenset[str]  # the blocks the template uses: one block may leave to another
    bundle: str
    version: str
    prefixes: Mapping[str, str]  # type CURIE -> alias prefix, types of the model only
    types: Mapping[str, JsonObject]  # CURIE -> the ``type`` body of ``describe type``
    relations: Mapping[str, JsonObject]  # CURIE -> one item of ``describe relations``
    patterns: Mapping[str, str]  # relation CURIE -> its ``describe schema`` pattern
    modules: Sequence[JsonObject]  # the items of ``describe modules``, built-ins included
    pages: Mapping[str, JsonObject]  # module id -> ``module`` body of ``describe module``
    functions: Sequence[JsonObject]  # active catalog entries, by module then name
    actions: Sequence[JsonObject]  # active action catalog entries, by module then name
    views: Sequence[JsonObject]  # active view catalog entries, by module then name

    @staticmethod
    def of(
        model: Mapping[str, JsonValue],
        graph: Mapping[str, JsonValue],
        blocks: frozenset[str] = frozenset(),
    ) -> Model:
        bundle = as_object(dict(model), "model")
        listing = _document(bundle, "types")
        relations = _document(bundle, "relations")
        schema = _document(bundle, "schema")
        modules = objects(bundle, "modules")
        pages: dict[str, JsonObject] = {}
        for module_id, value in sorted(req_obj(bundle, "module").items()):
            body = req_obj(_document({"module": value}, "module"), "module")
            if req_str(body, "id") != module_id or "/" in module_id:
                raise ValueError(f"module document {module_id!r} describes {body.get('id')!r}")
            pages[module_id] = body
        types: dict[str, JsonObject] = {}
        for curie, value in sorted(req_obj(bundle, "type").items()):
            body = req_obj(_document({"type": value}, "type"), "type")
            if req_str(body, "curie") != curie:
                raise ValueError(f"type document {curie!r} describes {body.get('curie')!r}")
            types[curie] = body
        listed = sorted(
            req_str(as_object(t, "types[]"), "curie") for t in req_list(listing, "types")
        )
        if listed != sorted(types):
            raise ValueError("the type documents do not match the types listing")
        ontology = req_obj(listing, "ontology")
        block = as_object(dict(graph), "graph")
        prefixes = {
            curie: str(prefix)
            for curie, prefix in sorted(req_obj(block, "alias_prefixes").items())
            if curie in types
        }
        return Model(
            name=req_str(block, "name"),
            title=opt_str(block, "title"),
            language=opt_str(block, "language"),
            blocks=blocks,
            bundle=req_str(ontology, "bundle"),
            version=req_str(ontology, "version"),
            prefixes=prefixes,
            types=types,
            relations={
                req_str(item, "curie"): item
                for item in (as_object(r, "relations[]") for r in req_list(relations, "relations"))
            },
            patterns={
                req_str(item, "relation"): req_str(item, "pattern")
                for item in (
                    as_object(r, "relationships[]") for r in req_list(schema, "relationships")
                )
            },
            modules=modules,
            pages=pages,
            functions=_active_functions(bundle),
            actions=_active_actions(bundle),
            views=_active_views(bundle),
        )

    @property
    def modular(self) -> bool:
        """Two or more modules besides the built-in ones: the model is read module by module."""
        return len(self.pages) >= 2

    @property
    def listed(self) -> list[str]:
        """The entity types of the graph: those with an alias prefix, else every concrete one."""
        if self.prefixes:
            return sorted(self.prefixes)
        return [curie for curie, body in self.types.items() if body.get("abstract") is not True]

    def touching(self) -> list[str]:
        """Relations from or to a listed type."""
        found = set()
        for curie in self.listed:
            body = self.types[curie]
            for key in ("relations_out", "relations_in"):
                found |= {req_str(as_object(r, key), "curie") for r in req_list(body, key)}
        return sorted(found)


def _active_functions(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled catalog document, if the bundle has one."""
    catalog = bundle.get("functions")
    if catalog is None:
        return []
    document = as_object(catalog, "functions")
    if document.get("schema") != FUNCTIONS_SCHEMA:
        raise ValueError(f"functions: expected {FUNCTIONS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in objects(document, "functions") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _active_actions(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled action catalog document, if the bundle has one."""
    catalog = bundle.get("actions")
    if catalog is None:
        return []
    document = as_object(catalog, "actions")
    if document.get("schema") != ACTIONS_SCHEMA:
        raise ValueError(f"actions: expected {ACTIONS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in objects(document, "actions") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _active_views(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled view catalog document, if the bundle has one."""
    catalog = bundle.get("views")
    if catalog is None:
        return []
    document = as_object(catalog, "views")
    if document.get("schema") != VIEWS_SCHEMA:
        raise ValueError(f"views: expected {VIEWS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in objects(document, "views") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _document(bundle: JsonObject, key: str) -> JsonObject:
    doc = req_obj(bundle, key)
    if doc.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"{key}: expected {MODEL_SCHEMA}, got {doc.get('schema')!r}")
    return doc


def objects(doc: JsonObject, key: str) -> list[JsonObject]:
    return [as_object(item, f"{key}[]") for item in req_list(doc, key)]


def strings(value: JsonValue) -> list[str]:
    return [str(item) for item in as_list(value, "list")] if value is not None else []
