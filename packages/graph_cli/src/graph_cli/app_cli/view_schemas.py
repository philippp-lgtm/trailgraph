"""JSON Schema 2020-12 of the view model (``kinetic.card.ViewModel.to_json``,
``graph_cli.view_model/1``): the sections of a view answer, one schema per kind and variant.

Every section has a ``kind``; the schemas follow the code that builds them (the section classes of
``kinetic.card``, the rows of ``kinetic.view_rows``), branch by branch, so that no real answer is
refused:

- ``fields``, ``relations``, ``files`` (and its ``variant: cited_by`` form for a stored file),
  ``affordances``, ``neighbors``, ``list`` and ``rows`` (and the form of a ``rows`` section whose
  function gave an ``error`` instead of rows);
- the **stub** ``{kind, id, cut: true}`` that every kind can shrink to when the byte guard trims
  an answer of an authored view.

Two documents use them. The ``view`` command declares ``view_model``: every kind, ``id`` optional
(the generated card has no ids) and the cells of a ``rows`` section any value; a consumer that meets
a kind it does not know accepts it, as the format promises. The tool manifest declares the answer
of each view tool by ``view_schema``: only the sections the view declares, each by its id, the rows
of a ``rows`` section typed by the columns of its function, and only the keys of the view's flavour
(an entity view has the entity and no pages, a set view has pages and no entity).

Published schemas are open (a key the code adds does not break a consumer); the tests close every
object that does not say otherwise (``tests/support/output_schemas.py``), so a section key the code
adds without declaring it fails the suite. The objects of one section are not extended with
``allOf``: each variant is complete in itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    STRING,
    Schema,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kinetic.view_format import KINDS

STRINGS: Final = array(STRING)
DIRECTION: Final = enum("out", "in")
ROLE: Final = enum("label", "alt_label", "state")  # kinetic.card.FieldChoice.role
COUNTS: Final = obj(extra=INTEGER)
ORIGIN: Final = "authored"
# the kinds whose section always has an id, also in the generic schema (``kinetic.card``: the
# sections that exist only in authored views)
_ID_REQUIRED: Final = ("neighbors", "list", "rows")
# the text every ``next`` of a set view gets, so a harness reads the paging contract in the schema
NEXT_TEXT: Final = (
    "Present when more is true: pass it as _cursor, with the same other arguments, to get the "
    "next page. view.cursor_stale means a write landed in between: start again from the first page."
)
CUT_TEXT: Final = (
    "In an entity view a long text cell is cut and ends in an ellipsis, so a cell of a type "
    "with a pattern, format or enum may not match it."
)


def described(schema: Schema, text: str) -> Schema:
    return {**schema, "description": text}


def _kind(kind: str) -> Schema:
    return {"const": kind}


def _section(
    kind: str,
    required: Mapping[str, Schema] | None = None,
    optional: Mapping[str, Schema] | None = None,
    *,
    id: str | None = None,
) -> Schema:
    """A section object: ``kind``, ``id`` (the section's own when ``id`` is given, required for
    the kinds of authored views) and ``title``, then the keys of the kind."""
    first: dict[str, Schema] = {"kind": _kind(kind)}
    later: dict[str, Schema] = {}
    if id is not None or kind in _ID_REQUIRED:
        first["id"] = STRING if id is None else {"const": id}
    else:
        later["id"] = STRING
    later["title"] = STRING
    return obj({**first, **(required or {})}, {**later, **(optional or {})})


# ---- the parts the sections share -------------------------------------------------------------


def _parts() -> dict[str, Schema]:
    """The definitions the section schemas refer to."""
    neighbour = obj(
        {"ref": STRING},
        {
            "label": STRING,
            "type": STRING,
            "state": STRING,
            "record_status": STRING,
            "field": obj({"name": STRING, "value": STRING}),
            "qualifiers": obj(extra=ANY),
        },
    )
    group = obj(
        {"relation": STRING, "direction": DIRECTION, "count": INTEGER},
        {
            "states": COUNTS,
            "items": array(ref("view_neighbour")),
            "more": INTEGER,
            "sampled": INTEGER,
            "approx": BOOLEAN,
            "missing": INTEGER,
        },
    )
    values: dict[str, Schema] = {
        "qualifiers": obj(extra=ANY),
        "fields": obj(extra=ANY),
        "chars": COUNTS,  # the length of a text that was cut, by attribute
        "more": COUNTS,  # the elements of a list that were cut, by attribute
    }
    row_item = obj(
        {"ref": STRING},
        {"label": STRING, "type": STRING, "state": STRING, "record_status": STRING, **values},
    )
    row_group = obj(
        {"relation": STRING, "direction": DIRECTION, "count": INTEGER},
        {
            "items": array(ref("view_row_item")),
            "more": INTEGER,
            "sampled": INTEGER,
            "approx": BOOLEAN,
            "unread": INTEGER,
        },
    )
    row = obj(
        {"ref": STRING},
        {
            "label": STRING,
            "type": STRING,
            "record_status": STRING,
            **values,
            "relations": array(ref("view_row_group")),
        },
    )
    return {
        "view_entity": obj(
            {"id": STRING, "type": STRING, "rev": INTEGER, "record_status": STRING},
            {"alias": STRING, "label": STRING, "superseded_by": STRINGS, "retracted_by": STRINGS},
        ),
        "view_field_item": obj(
            {"name": STRING, "value": ANY},
            {"role": ROLE, "chars": INTEGER, "more": INTEGER},
        ),
        "view_neighbour": neighbour,
        "view_group": group,
        "view_file": obj(
            {"ref": STRING},
            {"title": STRING, "page": INTEGER, "last_page": INTEGER, "locator": STRING},
        ),
        "view_citation": obj(
            {"ref": STRING}, {"label": STRING, "page": INTEGER, "last_page": INTEGER}
        ),
        "view_row_item": row_item,
        "view_row_group": row_group,
        "view_row": row,
    }


# ---- one schema per kind and variant ----------------------------------------------------------


def fields(id: str | None = None) -> Schema:
    """``card.FieldsSection``: the shown attributes, the names of the others (``other``)."""
    return _section(
        "fields",
        optional={
            "items": array(ref("view_field_item")),
            "other": STRINGS,
            "other_more": INTEGER,
        },
        id=id,
    )


def relations(id: str | None = None) -> Schema:
    """``card.RelationsSection``: one group per relation and direction."""
    return _section(
        "relations",
        optional={"groups": array(ref("view_group")), "more_groups": STRINGS},
        id=id,
    )


def files(id: str | None = None) -> Schema:
    """``card.FilesSection``: the stored files an entity was taken from."""
    return _section(
        "files",
        optional={"items": array(ref("view_file")), "more": INTEGER, "sampled": INTEGER},
        id=id,
    )


def cited_by(id: str | None = None) -> Schema:
    """``card.CitedBySection``: the ``files`` section of a stored file, who cites it."""
    return _section(
        "files",
        {"variant": _kind("cited_by"), "count": INTEGER},
        {
            "page_min": INTEGER,
            "page_max": INTEGER,
            "pages_cited": INTEGER,
            "items": array(ref("view_citation")),
            "more": INTEGER,
            "sampled": INTEGER,
            "approx": BOOLEAN,
        },
        id=id,
    )


def affordances(id: str | None = None) -> Schema:
    """``card.AffordancesSection``: the keys of ``card.affordance_summary`` and ``detail`` inline
    (the section has no key of its own)."""
    transition = obj(
        {"attr": nullable(STRING)},
        {
            "allowed": STRINGS,
            "allowed_more": INTEGER,
            "blocked": array(obj({"state": STRING, "by": STRINGS})),
            "blocked_more": INTEGER,
        },
    )
    return _section(
        "affordances",
        optional={
            "transitions": array(transition),
            "transitions_more": INTEGER,
            "actions": obj({}, {"apply": STRINGS, "blocked": INTEGER}),
            "actions_more": INTEGER,
            "functions": STRINGS,
            "functions_more": INTEGER,
            "views": STRINGS,
            "views_more": INTEGER,
            "detail": STRING,
        },
        id=id,
    )


def neighbors(id: str | None = None) -> Schema:
    """``card.NeighborsSection``: one relation of the entity as rows."""
    return _section(
        "neighbors",
        {"relation": STRING, "direction": DIRECTION, "count": INTEGER},
        {
            "items": array(ref("view_row")),
            "more": INTEGER,
            "sampled": INTEGER,
            "approx": BOOLEAN,
            "unread": INTEGER,
        },
        id=id,
    )


def listing(id: str | None = None) -> Schema:
    """``card.ListSection``: a type listing as rows on one page of a set view; ``from`` is the
    offset of the first row when the page starts inside the section."""
    return _section(
        "list",
        {"type": {"anyOf": [STRING, STRINGS]}, "count": INTEGER},
        {"items": array(ref("view_row")), "from": INTEGER},
        id=id,
    )


def _function(name: str | None) -> Schema:
    """The ``function`` of a ``rows`` section: its name (the section's own when given) and hash."""
    return obj({"name": STRING if name is None else {"const": name}}, {"hash": STRING})


def rows(
    id: str | None = None,
    row: Schema | None = None,
    *,
    function: str | None = None,
    cut: bool = False,
) -> Schema:
    """``card.RowsSection`` with rows: the rows of a function (``function``: its name); ``row``
    types one of them (any object when not given), ``cut`` says its text cells may have been cut
    (an entity view). ``more`` and ``cells_cut`` belong to an entity view, ``from`` to a set
    view."""
    items = array(obj(extra=ANY) if row is None else row)
    return _section(
        "rows",
        {"function": _function(function), "columns": STRINGS, "count": INTEGER},
        {
            "items": described(items, CUT_TEXT) if cut else items,
            "more": INTEGER,
            "cells_cut": INTEGER,
            "from": INTEGER,
        },
        id=id,
    )


def rows_error(id: str | None = None, function: str | None = None) -> Schema:
    """``card.RowsSection`` whose function gave no rows: the ``error`` and nothing else."""
    return _section(
        "rows",
        {"function": _function(function), "error": obj({"code": STRING, "message": STRING})},
        id=id,
    )


def stub(kind: str | None = None, id: str | None = None) -> Schema:
    """``card.StubSection``: what is left of a section the byte guard removed. The generated card
    has no section ids, so ``id`` is optional unless the section's own is given."""
    kind_schema = STRING if kind is None else _kind(kind)
    required: dict[str, Schema] = {"kind": kind_schema, "cut": {"const": True}}
    if id is None:
        return obj(required, {"id": STRING})
    return obj({**required, "id": {"const": id}})


# kind -> the builders of its variants (``rows`` has its own, a row is typed per view)
_BUILDERS: Final = {
    "fields": (fields,),
    "relations": (relations,),
    "files": (files, cited_by),
    "affordances": (affordances,),
    "neighbors": (neighbors,),
    "list": (listing,),
}


def _catch_all() -> Schema:
    """A section of a kind this program does not know: a consumer ignores it (open here, closed
    to its ``kind`` in the tests, so a kind the code adds without a schema fails the suite)."""
    return {
        "type": "object",
        "properties": {"kind": {"type": "string", "not": {"enum": list(KINDS)}}},
        "required": ["kind"],
    }


def _view_model() -> Schema:
    """``ViewModel.to_json`` in general: a view, the entity's header (an entity view; a set view
    has none) and sections. ``cut`` only when the byte guard trimmed the answer, ``sections_cut``
    when whole sections went; ``next`` and ``more`` only on a page of a set view that does not
    reach the end, ``oversize`` on a page that holds one row larger than its limit; ``view.hash``
    only for an authored view."""
    return obj(
        {
            "view": obj({"name": STRING, "origin": STRING}, {"hash": STRING}),
            "sections": array(ref("view_section")),
            "bytes": INTEGER,
        },
        {
            "entity": ref("view_entity"),
            "cut": BOOLEAN,
            "sections_cut": INTEGER,
            "next": described(STRING, NEXT_TEXT),
            "more": BOOLEAN,
            "oversize": BOOLEAN,
        },
    )


def _definitions() -> dict[str, Schema]:
    sections: dict[str, Schema] = {
        "view_section_fields": fields(),
        "view_section_relations": relations(),
        "view_section_files": files(),
        "view_section_cited_by": cited_by(),
        "view_section_affordances": affordances(),
        "view_section_neighbors": neighbors(),
        "view_section_list": listing(),
        "view_section_rows": rows(),
        "view_section_rows_error": rows_error(),
        "view_section_stub": stub(),
    }
    return {
        **_parts(),
        **sections,
        "view_section": {"anyOf": [*(ref(name) for name in sections), _catch_all()]},
        "view_model": _view_model(),
    }


DEFS: Final[dict[str, Schema]] = _definitions()


# ---- the answer of one view -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionShape:
    """A section a view declares: its ``kind`` and ``id``, and for a ``rows`` section its
    ``function`` and ``row``, the schema of one row of it (``None``: not known, any object)."""

    kind: str
    id: str
    function: str | None = None
    row: Schema | None = None


def _variants(shape: SectionShape, *, entity: bool) -> list[Schema]:
    """What the section of ``shape`` may look like in an answer: its variants, then its stub."""
    if shape.kind == "rows":
        found = [
            rows(
                shape.id, shape.row, function=shape.function, cut=entity and shape.row is not None
            ),
            rows_error(shape.id, shape.function),
        ]
    else:
        found = [build(shape.id) for build in _BUILDERS[shape.kind]]
    return [*found, stub(shape.kind, shape.id)]


def view_schema(name: str, sections: Sequence[SectionShape], *, entity: bool) -> JsonObject:
    """The answer of the view ``name`` (with ``$ref`` to ``DEFS``; ``standalone`` inlines them):
    only its declared sections, each by id, and the keys of its flavour. An ``entity`` view shows
    one entity, trimmed by a byte guard (``cut``, ``sections_cut``) and has no pages; a set view
    has pages (``next`` and ``more``, ``oversize``) and no entity."""
    required: dict[str, Schema] = {
        "view": obj({"name": _kind(name), "origin": _kind(ORIGIN), "hash": STRING}),
    }
    optional: dict[str, Schema]
    if entity:
        required["entity"] = ref("view_entity")
        optional = {"sections_cut": INTEGER, "cut": BOOLEAN}
    else:
        optional = {
            "next": described(STRING, NEXT_TEXT),
            "more": BOOLEAN,
            "oversize": BOOLEAN,
        }
    allowed: list[Schema] = [v for shape in sections for v in _variants(shape, entity=entity)]
    required["sections"] = array({"anyOf": allowed})
    required["bytes"] = INTEGER
    return dict(obj(required, optional))


def cut_tolerant(schema: JsonObject) -> JsonObject:
    """``schema`` without what a cut text cannot meet: ``pattern``, ``format`` and ``enum`` of the
    schemas of strings (an entity view cuts long strings and ends them in an ellipsis)."""
    found = _tolerant(schema)
    assert isinstance(found, dict)
    return found


def _tolerant(schema: JsonValue) -> JsonValue:
    if isinstance(schema, list):
        return [_tolerant(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    text = schema.get("type") == "string"
    kept: dict[str, JsonValue] = {}
    for key, value in schema.items():
        if text and key in ("pattern", "format", "enum"):
            continue
        if key == "properties" and isinstance(value, Mapping):  # names, not keywords
            kept[key] = {name: _tolerant(sub) for name, sub in value.items()}
        else:
            kept[key] = _tolerant(value)
    return kept
