"""The generated entity card: what is relevant about one entity, in one bounded answer (T17).

``generated_spec`` derives a ``ViewSpec`` from the ontology (the class of the entity), ``render``
turns the spec, one entity revision, the reader's snapshot and model into a ``ViewModel`` and
``ViewModel.to_json`` is the format-neutral answer (``graph_cli.view_model/1``): a ``view`` and an
``entity`` header and a list of ``sections``, each with a ``kind``. A consumer ignores a kind it
does not know, so authored views (stage 2) can add kinds and read the same document. The four kinds
of the generated card:

- ``fields``: the label, the lifecycle state, required and unique attributes with their values;
  every other attribute that has a value is only named in ``other``.
- ``relations``: one group per (relation, direction) with the count, the first few neighbours by
  name and, when the neighbours share one lifecycle attribute, the count per state.
- ``files``: the stored files the entity was taken from (``files:from_file``) with their pages; for
  a file itself the variant ``cited_by``: who cites it and which pages.
- ``affordances``: what applies next, names only (``affordance_summary``).

Everything is built from the snapshot and the model that are passed in, so a limited reader's
filtered views limit the card too. Nothing is stored or cached. The work per card is bounded
(``LOAD_MAX`` neighbours per shown group; a larger group is sampled and says so) and so is the
answer (``BYTE_LIMIT``): fixed caps first, then one deterministic byte guard that trims in a
fixed order and sets ``cut``. Every cut is counted (``more``, ``more_groups``, ``other``,
``other_more``), nothing is dropped silently. No key has an empty value: a part with nothing
in it is left out (a card of an entity without values has no ``fields`` section; the
``affordances`` section of an entity that cannot do anything is only its ``kind``).
``more_groups`` names the relations (without direction) that have edges and are not shown;
the relations of the groups past the eighth are not read, so an in-relation whose sources
are all inactive may be named there although it would have had a count of zero.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.langtag import text_of
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import OrderKey, Pred
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kinetic import view_format as fmt
from graph_cli.kinetic.params import bind_json
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.kinetic.view_format import (
    ENTITY_MAX_BYTES,
    FIELDS_MAX_DEFAULT,
    GROUPS_MAX_DEFAULT,
    ITEMS_MAX_DEFAULT,
    VALUE_CHARS_DEFAULT,
    FieldSel,
)
from graph_cli.kinetic.view_rows import (
    NO_VALUES,
    Loader,
    Loads,
    Row,
    RowBuilder,
    RowRelationSpec,
    RowsResult,
)
from graph_cli.kinetic.view_values import (
    cut as _cut,
)
from graph_cli.kinetic.view_values import (
    field_value,
)
from graph_cli.kinetic.view_values import (
    has_value as _has_value,
)
from graph_cli.kinetic.view_values import (
    label_key as _label_key,
)
from graph_cli.kinetic.view_values import (
    lifecycle_attribute as _lifecycle_attribute,
)
from graph_cli.kinetic.view_values import (
    main_label as _main_label,
)
from graph_cli.kinetic.view_values import (
    state_of as _state,
)
from graph_cli.ontology_layer.affordances import ACTIVE
from graph_cli.ontology_layer.model import AttributeDef, Bound, CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.cypher.values import Value
from graph_cli.query.lookup import label_text, text_values
from graph_cli.query.plan import Clause, Operand, plan_clauses

VIEW_MODEL_SCHEMA: Final = "graph_cli.view_model/1"
CARD: Final = "card"
GENERATED: Final = "generated"
AUTHORED: Final = "authored"
FILES_MODULE_RELATION: Final = "files:from_file"
FILE_CLASS: Final = "files:File"

BYTE_LIMIT: Final = ENTITY_MAX_BYTES  # the one definition is the format's: a view may only lower it
FIELDS_MAX: Final = FIELDS_MAX_DEFAULT
VALUE_CHARS: Final = VALUE_CHARS_DEFAULT
GUARD_VALUE_CHARS: Final = 40
LIST_ITEMS: Final = 5
OTHER_MAX: Final = 12
ALT_LABELS_MAX: Final = 3
GROUPS_MAX: Final = GROUPS_MAX_DEFAULT
ITEMS_MAX: Final = ITEMS_MAX_DEFAULT
LABEL_CHARS: Final = 48
KEY_CHARS: Final = 60
FILES_MAX: Final = 5
FILES_LOAD_MAX: Final = 50
LOAD_MAX: Final = 100
NAMES_MAX: Final = 5
ENTITY_LOADS: Final = 1000  # revisions the authored sections of one entity view may load
ERROR_CHARS: Final = 300  # the message of a section error is cut here
ROWS_CELL_CHARS: Final = 200  # a string cell of a ``rows`` section is cut here in an entity view

Direction = Literal["out", "in"]
Json = dict[str, JsonValue]


# ---- the spec (what stage 2 will read from a published file) ---------------------------------


@dataclass(frozen=True, slots=True)
class FieldChoice:
    """A candidate for the ``fields`` section: an attribute and the role it plays on the card."""

    name: str
    role: str | None  # label, alt_label, state, or None


@dataclass(frozen=True, slots=True)
class FieldsSpec:
    """The ``fields`` section: ``choices`` as given (the generated card), or, for an authored
    view, ``names`` (the listed attributes, roles read from the entity's class, or ``all``) or
    ``derive`` (the generated choice of the entity's own class)."""

    choices: tuple[FieldChoice, ...]
    max_items: int = FIELDS_MAX
    value_chars: int = VALUE_CHARS
    list_items: int = LIST_ITEMS
    max_alt_labels: int = ALT_LABELS_MAX
    other_max: int = OTHER_MAX
    id: str | None = None
    title: str | None = None
    names: tuple[str, ...] | Literal["all"] | None = None
    derive: bool = False
    name_other: bool = True  # attributes that are not shown are named in ``other``


@dataclass(frozen=True, slots=True)
class RelationsSpec:
    max_groups: int = GROUPS_MAX
    max_items: int = ITEMS_MAX
    load_max: int = LOAD_MAX
    label_chars: int = LABEL_CHARS
    key_chars: int = KEY_CHARS  # the one key field of a neighbour; 0 shows none
    order: Literal["label"] = "label"  # first by name: deterministic, cacheable
    id: str | None = None
    title: str | None = None
    only: tuple[tuple[str, Direction], ...] | None = None  # authored: these, in this order


@dataclass(frozen=True, slots=True)
class FilesSpec:
    max_items: int = FILES_MAX
    load_max: int = FILES_LOAD_MAX
    label_chars: int = LABEL_CHARS
    id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class AffordancesSpec:
    max_names: int = NAMES_MAX
    id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class NeighborsSpec:
    """One relation of the entity as rows. ``where`` and ``order`` are planned (``plan_find``)
    with the view's parameters substituted."""

    id: str
    relation: str
    direction: Direction = "out"
    title: str | None = None
    where: Pred | None = None
    order: tuple[OrderKey, ...] = ()
    fields: FieldSel = None
    relations: tuple[RowRelationSpec, ...] = ()
    max: int = 10
    value_chars: int = VALUE_CHARS
    label_chars: int = LABEL_CHARS


@dataclass(frozen=True, slots=True)
class RowsSpec:
    """The rows of a published function; the service runs it and hands the result to ``render``."""

    id: str
    function: str
    title: str | None = None
    max: int | None = 10  # None: a set view, the rows are paged, never cut
    cell_chars: int | None = ROWS_CELL_CHARS  # None: values are never cut (set views)


@dataclass(frozen=True, slots=True)
class ListSpec:
    """A type listing as rows (set views): the active entities of ``classes`` that match
    ``where``, in ``order`` (the ``find`` order: by id, by default), paged by the caller.
    ``with_type`` is decided by the classes, not by the data: several concrete classes put the
    class on every row."""

    id: str
    types: tuple[str, ...]
    classes: tuple[str, ...]  # the types and their subclasses, as ``find --type`` expands them
    title: str | None = None
    where: Pred | None = None
    order: tuple[OrderKey, ...] = ()
    fields: FieldSel = None
    relations: tuple[RowRelationSpec, ...] = ()
    with_type: bool = False


type SectionSpec = (
    FieldsSpec | RelationsSpec | FilesSpec | AffordancesSpec | NeighborsSpec | RowsSpec | ListSpec
)


@dataclass(frozen=True, slots=True)
class ViewSpec:
    name: str
    origin: str
    sections: tuple[SectionSpec, ...]
    byte_limit: int = BYTE_LIMIT
    hash: str | None = None  # of the published view an authored spec comes from
    page_bytes: int | None = None  # set views: the page size the file asks for


def _plain(attribute: AttributeDef) -> bool:
    """Whether an attribute can be a key field: not a label (shown by its role) and not the hash of
    a stored file (long, and of no use to a reader of the card)."""
    return attribute.role not in LABEL_ROLES and attribute.type != "blob"


def field_choices(model: CompiledModel, class_curie: str) -> tuple[FieldChoice, ...]:
    """The key fields of a class in order: the main label and the alternative labels, the
    lifecycle attributes, the attributes marked ``required``, the attributes marked ``unique``."""
    cls = model.classes.get(class_curie)
    attributes = {} if cls is None else cls.attributes
    choices: list[FieldChoice] = []
    seen: set[str] = set()

    def add(name: str, role: str | None) -> None:
        if name not in seen:
            seen.add(name)
            choices.append(FieldChoice(name, role))

    for name in sorted(attributes):
        if attributes[name].role == "label":
            add(name, "label")
    for name in sorted(attributes):
        if attributes[name].role == "alt_label":
            add(name, "alt_label")
    for name in sorted(attributes):
        if attributes[name].kind == "lifecycle":
            add(name, "state")
    for name in sorted(attributes):
        if _plain(attributes[name]) and attributes[name].required:
            add(name, None)
    for name in sorted(attributes):
        if _plain(attributes[name]) and attributes[name].unique:
            add(name, None)
    return tuple(choices)


def generated_spec(model: CompiledModel, class_curie: str) -> ViewSpec:
    """The card of a class, derived from the ontology (nothing is stored).

    Key fields in order: the main label and the alternative labels, the lifecycle attributes,
    the attributes marked ``required``, the attributes marked ``unique``. These are the only
    statements about importance that every ontology makes. Hidden labels and the hash of a stored
    file (type ``blob``) are never candidates.
    """
    choices = field_choices(model, class_curie)
    return ViewSpec(
        CARD,
        GENERATED,
        (FieldsSpec(choices), RelationsSpec(), FilesSpec(), AffordancesSpec()),
    )


# ---- the spec of an authored view ------------------------------------------------------------


def _direction(text: str) -> Direction:
    return "in" if text == "in" else "out"


def _far_end(model: CompiledModel, relation: str, direction: Direction) -> tuple[str, ...]:
    """The classes at the far end of ``relation`` read in ``direction`` as declared (empty:
    any class)."""
    declared = model.relations[relation]
    return declared.range if direction == "out" else declared.domain


def _operand(value: Value) -> Operand:
    if isinstance(value, list):
        return [_operand(item) for item in value]
    assert isinstance(value, str | int | bool | Decimal)
    return value


def _plan(
    model: CompiledModel,
    types: tuple[str, ...],
    where: tuple[str, ...],
    order: tuple[str, ...],
    params: Mapping[str, Value],
) -> tuple[Pred | None, tuple[OrderKey, ...]]:
    """``where`` (a ``$name`` as the typed operand of the bound parameter) and ``order`` planned
    by the ``find`` planner against ``types``; a published view compiled against the model, so
    nothing fails."""
    clauses: list[Clause] = []
    for entry in where:
        left, name = fmt.where_parts(entry)
        if name is None:
            clauses.append(entry)
        elif name in params:
            clauses.append((left, _operand(params[name])))
        else:  # a parameter that was never bound must not be matched as the text "$name"
            raise GraphCliError("internal.error", f"the parameter {name!r} of a view is not bound")
    return plan_clauses(model, types, clauses, order)


def _row_relations(
    model: CompiledModel,
    relations: tuple[fmt.RowRelation, ...],
    params: Mapping[str, Value],
    *,
    edge_order: bool = False,
) -> tuple[RowRelationSpec, ...]:
    """The per-row lists of a section. ``edge_order`` (``list`` sections): out-relations without
    ``order`` keep the edge order ``find`` gives; everywhere else they follow the label."""
    found: list[RowRelationSpec] = []
    for rel in relations:
        if rel.relation is None:
            found.append(RowRelationSpec(None, "out", None, None, (), rel.max, edge_order))
            continue
        direction = _direction(rel.direction)
        where, order = _plan(
            model, _far_end(model, rel.relation, direction), rel.where, rel.order, params
        )
        found.append(
            RowRelationSpec(rel.relation, direction, rel.fields, where, order, rel.max, edge_order)
        )
    return tuple(found)


def spec_of(
    entry: ViewEntry, model: CompiledModel, params: Mapping[str, Value] | None = None
) -> ViewSpec:
    """The spec of a published entity view: its normalized sections one to one, ``where`` and
    ``order`` planned against ``model`` (the reader's) with the bound ``params`` substituted.
    A parameter that is not given takes its default (``view.parameter_missing`` for a required
    one). Pure; the same entry, model and parameters give the same spec."""
    values = dict(params or {})
    missing = [p for p in entry.defn.params if p.name not in values]
    if missing:
        values.update(bind_json("view", entry.name, missing, {}, model))
    sections: list[SectionSpec] = []
    for section in entry.defn.sections:
        if isinstance(section, fmt.FieldsSection):
            names = section.fields
            sections.append(
                FieldsSpec(
                    (),
                    section.max,
                    section.value_chars,
                    id=section.id,
                    title=section.title,
                    names=names,
                    derive=names is None,
                    name_other=not isinstance(names, tuple),
                )
            )
        elif isinstance(section, fmt.RelationsSection):
            only = None
            if section.relations is not None:
                only = tuple((r.relation, _direction(r.direction)) for r in section.relations)
            sections.append(
                RelationsSpec(
                    section.max_groups,
                    section.max_items,
                    id=section.id,
                    title=section.title,
                    only=only,
                )
            )
        elif isinstance(section, fmt.FilesSection):
            sections.append(FilesSpec(id=section.id, title=section.title))
        elif isinstance(section, fmt.AffordancesSection):
            sections.append(AffordancesSpec(id=section.id, title=section.title))
        elif isinstance(section, fmt.NeighborsSection):
            direction = _direction(section.direction)
            end = _far_end(model, section.relation, direction)
            where, order = _plan(model, end, section.where, section.order, values)
            sections.append(
                NeighborsSpec(
                    section.id,
                    section.relation,
                    direction,
                    section.title,
                    where,
                    order,
                    section.fields,
                    _row_relations(model, section.relations, values),
                    section.max,
                )
            )
        elif isinstance(section, fmt.RowsSection):
            sections.append(
                RowsSpec(
                    section.id,
                    section.function,
                    section.title,
                    section.max if entry.entity else None,
                    ROWS_CELL_CHARS if entry.entity else None,
                )
            )
        elif isinstance(section, fmt.ListSection):
            classes = tuple(sorted({c for t in section.types for c in model.subtypes(t)}))
            where, order = _plan(model, section.types, section.where, section.order, values)
            concrete = [c for c in classes if not model.classes[c].abstract]
            sections.append(
                ListSpec(
                    section.id,
                    section.types,
                    classes,
                    section.title,
                    where,
                    order,
                    section.fields,
                    _row_relations(model, section.relations, values, edge_order=True),
                    len(concrete) > 1,
                )
            )
        else:
            raise ValueError(f"a {section.kind} section is not part of this view")
    limit = entry.defn.max_bytes or BYTE_LIMIT
    return ViewSpec(
        entry.name,
        AUTHORED,
        tuple(sections),
        min(limit, BYTE_LIMIT),
        entry.hash,
        entry.defn.page_bytes,
    )


# ---- the view model --------------------------------------------------------------------------


def _head(kind: str, id: str | None, title: str | None) -> Json:
    """The start of a section: its kind and, for an authored view, its id and title."""
    found: Json = {"kind": kind}
    if id is not None:
        found["id"] = id
    if title is not None:
        found["title"] = title
    return found


@dataclass(frozen=True, slots=True)
class FieldItem:
    name: str
    role: str | None
    value: JsonValue
    chars: int | None = None  # the length of a string that was cut
    more: int = 0  # elements of a list that were cut

    def to_json(self) -> Json:
        found: Json = {"name": self.name}
        if self.role is not None:
            found["role"] = self.role
        found["value"] = self.value
        if self.chars is not None:
            found["chars"] = self.chars
        if self.more:
            found["more"] = self.more
        return found


@dataclass(frozen=True, slots=True)
class FieldsSection:
    items: tuple[FieldItem, ...]
    other: tuple[str, ...] = ()  # names of attributes with a value that are not shown
    other_more: int = 0  # names cut from ``other``
    id: str | None = None
    title: str | None = None

    def to_json(self) -> Json:
        found = _head("fields", self.id, self.title)
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
        if self.other:
            found["other"] = list(self.other)
        if self.other_more:
            found["other_more"] = self.other_more
        return found


@dataclass(frozen=True, slots=True)
class Neighbour:
    ref: str
    label: str | None = None
    type: str | None = None  # only when the group's neighbours have more than one type
    state: str | None = None
    record_status: str | None = None  # only when the neighbour is not active
    qualifiers: Mapping[str, JsonValue] = field(default_factory=dict)
    entity: EntityId | None = None  # for the read signal; not in the JSON
    key_field: tuple[str, str] | None = None  # (name, value) of its first key field

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("label", self.label),
            ("type", self.type),
            ("state", self.state),
            ("record_status", self.record_status),
        ):
            if value is not None:
                found[key] = value
        if self.key_field is not None:
            found["field"] = {"name": self.key_field[0], "value": self.key_field[1]}
        if self.qualifiers:
            found["qualifiers"] = dict(self.qualifiers)
        return found


@dataclass(frozen=True, slots=True)
class Group:
    relation: str
    direction: Direction
    count: int
    items: tuple[Neighbour, ...]
    states: Mapping[str, int] = field(default_factory=dict)
    sampled: int = 0  # the number of edges items and states come from, when not all
    approx: bool = False  # ``count`` is the raw number of in-edges, inactive sources included
    missing: int = 0  # edges short of the relation's minimum

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found: Json = {"relation": self.relation, "direction": self.direction, "count": self.count}
        if self.states:
            found["states"] = dict(self.states)
        if self.items:  # none when the sample held only inactive sources
            found["items"] = [i.to_json() for i in self.items]
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        if self.approx:
            found["approx"] = True
        if self.missing:
            found["missing"] = self.missing
        return found


@dataclass(frozen=True, slots=True)
class RelationsSection:
    groups: tuple[Group, ...]
    more_groups: tuple[str, ...] = ()  # relation names with edges that are not shown
    id: str | None = None
    title: str | None = None

    def to_json(self) -> Json:
        found = _head("relations", self.id, self.title)
        if self.groups:
            found["groups"] = [g.to_json() for g in self.groups]
        if self.more_groups:
            found["more_groups"] = list(self.more_groups)
        return found


@dataclass(frozen=True, slots=True)
class FileRef:
    ref: str
    title: str | None = None
    page: int | None = None
    last_page: int | None = None
    locator: str | None = None
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("title", self.title),
            ("page", self.page),
            ("last_page", self.last_page),
            ("locator", self.locator),
        ):
            if value is not None:
                found[key] = value
        return found


@dataclass(frozen=True, slots=True)
class FilesSection:
    items: tuple[FileRef, ...]
    more: int = 0
    sampled: int = 0
    id: str | None = None
    title: str | None = None

    def to_json(self) -> Json:
        found = _head("files", self.id, self.title)
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        return found


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    label: str | None = None
    page: int | None = None
    last_page: int | None = None
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("label", self.label),
            ("page", self.page),
            ("last_page", self.last_page),
        ):
            if value is not None:
                found[key] = value
        return found


@dataclass(frozen=True, slots=True)
class CitedBySection:
    """The ``files`` section of a stored file: who cites it, which pages."""

    count: int
    items: tuple[Citation, ...]
    page_min: int | None = None
    page_max: int | None = None
    pages_cited: int = 0
    sampled: int = 0
    approx: bool = False
    id: str | None = None
    title: str | None = None

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found = _head("files", self.id, self.title)
        found["variant"] = "cited_by"
        found["count"] = self.count
        if self.page_min is not None:
            found["page_min"] = self.page_min
        if self.page_max is not None:
            found["page_max"] = self.page_max
        if self.pages_cited:
            found["pages_cited"] = self.pages_cited
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        if self.approx:
            found["approx"] = True
        return found


@dataclass(frozen=True, slots=True)
class AffordancesSection:
    summary: Mapping[str, JsonValue]  # ``affordance_summary``, plus ``detail``
    id: str | None = None
    title: str | None = None

    def to_json(self) -> Json:
        return {**_head("affordances", self.id, self.title), **self.summary}


@dataclass(frozen=True, slots=True)
class NeighborsSection:
    """One relation of the entity as rows; ``count`` is what the reader sees (the entries that
    match ``where``), ``more`` what ``max`` cut, ``sampled`` the edges read when there are more."""

    id: str
    relation: str
    direction: Direction
    items: tuple[Row, ...]
    count: int
    sampled: int = 0
    approx: bool = False
    title: str | None = None
    unread: int = 0  # edges not read: ``count`` may be off by up to this many

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found = _head("neighbors", self.id, self.title)
        found["relation"] = self.relation
        found["direction"] = self.direction
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
        found["count"] = self.count
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        if self.approx:
            found["approx"] = True
        if self.unread:
            found["unread"] = self.unread
        return found


@dataclass(frozen=True, slots=True)
class RowsSection:
    """The rows of a published function (``columns`` and ``items`` as the function returned them,
    long strings cut), or the ``error`` that ended it: the rest of the view still comes."""

    id: str
    function: str
    function_hash: str | None = None
    columns: tuple[str, ...] = ()
    items: tuple[Mapping[str, JsonValue], ...] = ()
    count: int = 0
    error: tuple[str, str] | None = None  # (code, message)
    title: str | None = None
    cells_cut: int = 0  # cells that were shortened (strings, lists), counted, not dropped silently
    start: int = 0  # set views: the offset of the first row when a page starts inside the section
    paged: bool = False  # set views: the cursor says what is left, a section has no ``more``

    @property
    def more(self) -> int:
        return 0 if self.paged else max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found = _head("rows", self.id, self.title)
        function: Json = {"name": self.function}
        if self.function_hash is not None:
            function["hash"] = self.function_hash
        found["function"] = function
        if self.error is not None:
            found["error"] = {"code": self.error[0], "message": self.error[1]}
            return found
        found["columns"] = list(self.columns)
        if self.items:
            found["items"] = [dict(i) for i in self.items]
        found["count"] = self.count
        if self.more:
            found["more"] = self.more
        if self.cells_cut:
            found["cells_cut"] = self.cells_cut
        if self.start:
            found["from"] = self.start
        return found


@dataclass(frozen=True, slots=True)
class StubSection:
    """What is left of a section of an authored view that had to go to keep the answer within
    its limit: its kind and id, and that it was cut."""

    kind: str
    id: str | None = None

    def to_json(self) -> Json:
        found = _head(self.kind, self.id, None)
        found["cut"] = True
        return found


@dataclass(frozen=True, slots=True)
class ListSection:
    """The rows of a type listing on one page of a set view: ``count`` is every row of the
    section, ``start`` the offset of the first row of this page (``from``)."""

    id: str
    types: tuple[str, ...]
    items: tuple[Row, ...]
    count: int
    start: int = 0
    title: str | None = None

    def to_json(self) -> Json:
        found = _head("list", self.id, self.title)
        found["type"] = self.types[0] if len(self.types) == 1 else list(self.types)
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
        found["count"] = self.count
        if self.start:
            found["from"] = self.start
        return found


type Section = (
    FieldsSection
    | RelationsSection
    | FilesSection
    | CitedBySection
    | AffordancesSection
    | NeighborsSection
    | RowsSection
    | StubSection
    | ListSection
)


@dataclass(frozen=True, slots=True)
class ViewModel:
    name: str
    origin: str
    entity: Mapping[str, JsonValue] | None  # ``None``: a set view takes no entity
    sections: tuple[Section, ...]
    cut: bool = False
    hash: str | None = None  # of the published view (authored views only)
    next: str | None = None  # set views: the cursor of the next page; ``None``: this is the last
    sections_cut: int = 0  # trailing sections that had to go, when even their stubs did not fit
    oversize: bool = False  # set views: the one row of this page is larger than the page limit

    def to_json(self) -> Json:
        """The compact answer; ``bytes`` is the size of this very document in UTF-8."""
        view: Json = {"name": self.name, "origin": self.origin}
        if self.hash is not None:
            view["hash"] = self.hash
        found: Json = {"view": view}
        if self.entity is not None:
            found["entity"] = dict(self.entity)
        found["sections"] = [s.to_json() for s in self.sections]
        if self.sections_cut:
            found["sections_cut"] = self.sections_cut
        if self.next is not None:
            found["next"] = self.next
            found["more"] = True
        if self.oversize:
            found["oversize"] = True
        found["bytes"] = 0
        if self.cut:
            found["cut"] = True
        size = len(dumps(found))
        for _ in range(4):  # the number of digits can only grow, so this settles at once
            found["bytes"] = size
            again = len(dumps(found))
            if again == size:
                break
            size = again
        return found

    def size(self) -> int:
        return len(dumps(self.to_json()))

    def shown(self) -> tuple[EntityId, ...]:
        """The entities the card names besides its own: the neighbours, files and citations
        that are in the answer (after the byte guard), each once."""
        found: dict[EntityId, None] = {}
        for section in self.sections:
            if isinstance(section, RelationsSection):
                named = [i.entity for g in section.groups for i in g.items]
            elif isinstance(section, FilesSection | CitedBySection):
                named = [i.entity for i in section.items]
            elif isinstance(section, NeighborsSection | ListSection):
                named = [e for row in section.items for e in (row.entity, *row.named())]
            else:
                continue
            found.update({e: None for e in named if e is not None})
        return tuple(found)


def dumps(data: JsonValue) -> bytes:
    """The compact UTF-8 JSON the byte limit is measured on."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()


# ---- the affordance summary ------------------------------------------------------------------


def affordance_summary(
    affordances: Mapping[str, JsonValue], spec: AffordancesSpec | None = None
) -> Json:
    """Names only of what ``get --affordances`` lists, at most ``max_names`` per list.

    ``transitions`` has one entry per lifecycle attribute: the states it may move to and the
    states it may not, each with the ids of the guards that fail (never the prose); ``actions``
    the names that apply and how many are blocked; ``functions`` the names. What is cut is
    counted (``actions_more``, ``functions_more``, ``transitions_more``, ``views_more``). ``views``
    names the other entity views of the entity's class (present only when there are any).
    """
    limit = (spec or AffordancesSpec()).max_names
    found: Json = {}
    transitions: list[JsonValue] = []
    for raw in _list(affordances.get("transitions")):
        entry = _mapping(raw)
        allowed: list[JsonValue] = []
        blocked: list[JsonValue] = []
        for target_raw in _list(entry.get("to")):
            target = _mapping(target_raw)
            state = target.get("state")
            if not isinstance(state, str):
                continue
            if target.get("allowed") is True:
                allowed.append(state)
            else:
                guards = [
                    g
                    for guard in _list(target.get("blocked_by"))
                    if isinstance(g := _mapping(guard).get("guard"), str)
                ]
                blocked.append({"state": state, "by": guards[:limit]})
        if not allowed and not blocked:
            continue
        item: Json = {"attr": entry.get("attr")}
        if allowed:
            item["allowed"] = allowed[:limit]
        if len(allowed) > limit:
            item["allowed_more"] = len(allowed) - limit
        if blocked:
            item["blocked"] = blocked[:limit]
        if len(blocked) > limit:
            item["blocked_more"] = len(blocked) - limit
        transitions.append(item)
    if transitions:
        found["transitions"] = transitions[:limit]
        if len(transitions) > limit:
            found["transitions_more"] = len(transitions) - limit
    apply: list[JsonValue] = []
    blocked_actions = 0
    for raw in _list(affordances.get("actions")):
        entry = _mapping(raw)
        name = entry.get("action")
        if entry.get("applies") is True and isinstance(name, str):
            if name not in apply:
                apply.append(name)
        else:
            blocked_actions += 1
    actions: Json = {}
    if apply:
        actions["apply"] = apply[:limit]
    if blocked_actions:
        actions["blocked"] = blocked_actions
    if actions:
        found["actions"] = actions
    more = max(0, len(apply) - limit) + _count(affordances.get("actions_more"))
    if more:
        found["actions_more"] = more
    functions: list[JsonValue] = []
    for raw in _list(affordances.get("functions")):
        name = _mapping(raw).get("function")
        if isinstance(name, str) and name not in functions:
            functions.append(name)
    if functions:
        found["functions"] = functions[:limit]
    more = max(0, len(functions) - limit) + _count(affordances.get("functions_more"))
    if more:
        found["functions_more"] = more
    views: list[JsonValue] = []
    for raw in _list(affordances.get("views")):
        name = _mapping(raw).get("view")
        if isinstance(name, str) and name not in views:
            views.append(name)
    if views:
        found["views"] = views[:limit]
    more = max(0, len(views) - limit) + _count(affordances.get("views_more"))
    if more:
        found["views_more"] = more
    return found


def _list(value: JsonValue) -> list[JsonValue]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def _count(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# ---- render ----------------------------------------------------------------------------------


def render(
    spec: ViewSpec,
    entity: Revision,
    snapshot: Snapshot,
    model: CompiledModel,
    *,
    language: str | None = None,
    affordances: Mapping[str, JsonValue] | None = None,
    superseded_by: Sequence[str] = (),
    retracted_by: Sequence[str] = (),
    file_classes: Collection[str] | None = None,
    rows: Mapping[str, RowsResult] | None = None,
) -> ViewModel:
    """The card of ``entity`` as ``snapshot`` and ``model`` show it (pure; reads only through
    ``snapshot``). ``language`` is the graph's language, the one a main label is shown in;
    ``affordances`` the object ``get --affordances`` builds (None: no affordances section content).
    ``file_classes`` are the classes that count as stored files (the family of ``files:File``);
    a limited reader's model may lack ``files:File`` itself although it sees a subclass, so the
    service passes the family of the stored model; None: the family of ``model``.
    ``rows`` are the results of the functions behind the ``rows`` sections of an authored view,
    by section id: the function runs in the service, this only formats and trims them.
    """
    ref = entity.alias or str(entity.id)
    header: Json = {"id": str(entity.id)}
    if entity.alias is not None:
        header["alias"] = entity.alias
    header["type"] = str(entity.type)
    header["rev"] = entity.rev
    header["record_status"] = entity.record_status
    label = _main_label(model, entity, language)
    if label is not None:
        header["label"] = _cut(label, VALUE_CHARS)[0]
    if superseded_by:
        header["superseded_by"] = list(superseded_by)
    if retracted_by:
        header["retracted_by"] = list(retracted_by)
    is_file = str(entity.type) in (file_classes_of(model) if file_classes is None else file_classes)
    candidates, citations = _candidates(entity, snapshot, model, is_file, _wanted(spec))
    # one read of each revision; the authored sections share a budget, the generated card has
    # none (its caps per group bound it, and a budget could only change what it shows)
    loader = Loader(snapshot, Loads(sys.maxsize if spec.origin == GENERATED else ENTITY_LOADS))
    sections: list[Section] = []
    for section_spec in spec.sections:
        built: Section | None = None
        if isinstance(section_spec, FieldsSpec):
            built = _fields(section_spec, entity, model, language)
        elif isinstance(section_spec, RelationsSpec):
            built = _relations(section_spec, candidates, loader, model, language)
        elif isinstance(section_spec, FilesSpec):
            built = _files(section_spec, entity, is_file, citations, loader, model, language)
        elif isinstance(section_spec, AffordancesSpec):
            summary = affordance_summary(affordances or {}, section_spec)
            if summary:
                summary = {**summary, "detail": f"graph get {ref} --affordances"}
            built = AffordancesSection(summary)
        elif isinstance(section_spec, NeighborsSpec):
            built = _neighbors(section_spec, entity, loader, model, language)
        elif isinstance(section_spec, RowsSpec):
            built = _rows(section_spec, rows)
        else:
            raise ValueError("a list section belongs to a set view: render it with render_set")
        if built is not None:
            sections.append(_named(built, section_spec))
    card = ViewModel(spec.name, spec.origin, header, tuple(sections), hash=spec.hash)
    return _guard(card, spec.byte_limit)


def _wanted(spec: ViewSpec) -> frozenset[str] | None:
    """The relations the ``relations`` sections of an authored view list, so that no other
    relation is read; ``None`` when a section takes every relation (the generated card)."""
    wanted: set[str] = set()
    for section in spec.sections:
        if isinstance(section, RelationsSpec):
            if section.only is None:
                return None
            wanted.update(relation for relation, _direction in section.only)
    return frozenset(wanted)


def _named(section: Section, spec: SectionSpec) -> Section:
    """``section`` with the id and title of an authored spec (the generated card has none)."""
    if isinstance(section, NeighborsSection | RowsSection | StubSection | ListSection) or (
        spec.id is None and spec.title is None
    ):
        return section
    return replace(section, id=spec.id, title=spec.title)


def _neighbors(
    spec: NeighborsSpec,
    entity: Revision,
    loader: Loader,
    model: CompiledModel,
    language: str | None,
) -> NeighborsSection:
    builder = RowBuilder(
        loader.snapshot,
        model,
        language,
        loads=loader.loads,
        loader=loader,
        value_chars=spec.value_chars,
        label_chars=spec.label_chars,
    )
    if spec.direction == "out":
        edges = [(e.target, e) for e in entity.edges if str(e.rel) == spec.relation]
    else:
        edges = list(loader.snapshot.incoming(entity.id, TermRef(spec.relation)))
    selection = builder.select(edges, spec.direction, spec.where, spec.order)
    items = builder.rows(selection, spec.fields, spec.relations, spec.max)
    return NeighborsSection(
        spec.id,
        spec.relation,
        spec.direction,
        items,
        selection.count,
        selection.sampled,
        selection.approx,
        spec.title,
        selection.unread,
    )


def _cell(value: JsonValue, limit: int | None) -> tuple[JsonValue, bool]:
    """A cell of a function's row as an entity view shows it: strings cut, lists to a few
    elements, and whether anything was cut (``limit`` None, a set view: nothing is cut)."""
    if limit is None:
        return value, False
    if isinstance(value, str):
        text, chars = _cut(value, limit)
        return text, chars is not None
    if isinstance(value, list):
        cells = [_cell(item, limit) for item in value[:LIST_ITEMS]]
        return [c for c, _ in cells], len(value) > LIST_ITEMS or any(w for _, w in cells)
    if isinstance(value, Mapping):
        found = {k: _cell(v, limit) for k, v in value.items()}
        return {k: c for k, (c, _) in found.items()}, any(w for _, w in found.values())
    return value, False


def rows_error(spec: RowsSpec, results: Mapping[str, RowsResult] | None) -> RowsSection | None:
    """The section of a ``rows`` spec whose function did not give rows: it was not run
    (``view.section_unavailable``) or it failed; ``None`` when there are rows to show."""
    result = (results or {}).get(spec.id)
    if result is None:
        message = "the function of this section was not run"
        error = ("view.section_unavailable", message)
        return RowsSection(spec.id, spec.function, error=error, title=spec.title)
    if result.error is not None:
        code, text = result.error
        error = (code, _cut(text, ERROR_CHARS)[0])
        return RowsSection(spec.id, spec.function, result.hash, error=error, title=spec.title)
    return None


def _rows(spec: RowsSpec, results: Mapping[str, RowsResult] | None) -> RowsSection:
    failed = rows_error(spec, results)
    if failed is not None:
        return failed
    result = (results or {})[spec.id]
    items: list[Mapping[str, JsonValue]] = []
    cells_cut = 0
    for row in result.rows[: spec.max]:
        cells = {column: _cell(value, spec.cell_chars) for column, value in row.items()}
        cells_cut += sum(1 for _, was_cut in cells.values() if was_cut)
        items.append({column: cell for column, (cell, _) in cells.items()})
    return RowsSection(
        spec.id,
        spec.function,
        result.hash,
        result.columns,
        tuple(items),
        len(result.rows),
        title=spec.title,
        cells_cut=cells_cut,
    )


def _field_value(value: JsonValue, spec: FieldsSpec) -> tuple[JsonValue, int | None, int]:
    return field_value(value, spec.value_chars, spec.list_items)


def _role_of(attribute: AttributeDef | None) -> str | None:
    if attribute is None:
        return None
    if attribute.role in ("label", "alt_label"):
        return attribute.role
    return "state" if attribute.kind == "lifecycle" else None


def _choices(
    spec: FieldsSpec, model: CompiledModel, class_curie: str, known: Mapping[str, AttributeDef]
) -> tuple[FieldChoice, ...]:
    """The candidates of the section: as the spec gives them (the generated card), the listed
    names with the roles of the entity's class, ``all`` (the key fields first, then every other
    attribute by name, never a ``blob``) or the generated choice of the entity's own class."""
    if spec.names is None:
        return field_choices(model, class_curie) if spec.derive else spec.choices
    if spec.names != "all":
        return tuple(FieldChoice(name, _role_of(known.get(name))) for name in spec.names)
    key = [c for c in field_choices(model, class_curie) if c.role is not None]
    seen = {c.name for c in key}
    rest = [
        FieldChoice(n, None) for n in sorted(known) if n not in seen and known[n].type != "blob"
    ]
    return (*key, *rest)


def _fields(
    spec: FieldsSpec, entity: Revision, model: CompiledModel, language: str | None
) -> FieldsSection | None:
    cls = model.classes.get(str(entity.type))
    known = {} if cls is None else cls.attributes
    items: list[FieldItem] = []
    accounted: set[str] = set()  # attributes shown or deliberately never named
    alt_names: list[str] = []
    alt_texts: list[str] = []
    for choice in _choices(spec, model, str(entity.type), known):
        value = entity.attrs.get(choice.name)
        if choice.name not in known or not _has_value(value):
            continue
        if choice.role == "alt_label":
            alt_names.append(choice.name)
            for alt, _tag in text_values(value):
                if alt not in alt_texts:
                    alt_texts.append(alt)
            continue
        if len(items) >= spec.max_items:
            continue
        if choice.role == "label":
            text = label_text(value, (language,))
            if text is None:
                continue
            shown, chars = _cut(text, spec.value_chars)
            items.append(FieldItem(choice.name, "label", shown, chars))
        else:
            shown_value, chars, more = _field_value(value, spec)
            items.append(FieldItem(choice.name, choice.role, shown_value, chars, more))
        accounted.add(choice.name)
    if alt_texts and len(items) < spec.max_items:
        # one item for all alternative labels, placed after the main label
        position = 1 if items and items[0].role == "label" else 0
        shown_texts: list[JsonValue] = [
            _cut(text, spec.value_chars)[0] for text in alt_texts[: spec.max_alt_labels]
        ]
        items.insert(
            position,
            FieldItem(
                alt_names[0],
                "alt_label",
                shown_texts,
                None,
                max(0, len(alt_texts) - spec.max_alt_labels),
            ),
        )
        accounted.update(alt_names)
    other = sorted(
        name
        for name, attribute in known.items()
        if spec.name_other
        and name not in accounted
        and attribute.role != "hidden_label"
        and _has_value(entity.attrs.get(name))
    )
    if not items and not other:
        return None  # nothing to show: no section
    return FieldsSection(
        tuple(items), tuple(other[: spec.other_max]), max(0, len(other) - spec.other_max)
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    relation: str
    direction: Direction
    edges: tuple[tuple[EntityId, EdgeRecord], ...]  # (neighbour, edge) in stored order


def _bound(model: CompiledModel, relation: str, direction: Direction) -> Bound:
    declared = model.relations.get(relation)
    if declared is None:
        return Bound(0, None)
    return declared.out if direction == "out" else declared.into


def file_classes_of(model: CompiledModel) -> frozenset[str]:
    """The classes of the ``files:File`` family of ``model`` (none without the files module)."""
    return frozenset(model.subtypes(FILE_CLASS)) if FILE_CLASS in model.classes else frozenset()


def _candidates(
    entity: Revision,
    snapshot: Snapshot,
    model: CompiledModel,
    is_file: bool,
    wanted: frozenset[str] | None = None,
) -> tuple[list[_Candidate], list[_Candidate]]:
    """The (relation, direction) pairs that have at least one edge the reader sees, in card order;
    the second list is the ``files:from_file`` edges of a stored file (its citations)."""
    out: dict[str, list[tuple[EntityId, EdgeRecord]]] = {}
    for edge in entity.edges:
        name = str(edge.rel)
        if name == FILES_MODULE_RELATION or (wanted is not None and name not in wanted):
            continue
        out.setdefault(name, []).append((edge.target, edge))
    found = [_Candidate(name, "out", tuple(edges)) for name, edges in out.items()]
    citations: list[_Candidate] = []
    if is_file:  # read whatever the model of the reader says about the relation: its range is
        # ``files:File``, a class a limited reader may not see although it sees a subclass
        cited = snapshot.incoming(entity.id, TermRef(FILES_MODULE_RELATION))
        if cited:
            citations.append(_Candidate(FILES_MODULE_RELATION, "in", tuple(cited)))
    for name in sorted(model.relations):
        if name == FILES_MODULE_RELATION or (wanted is not None and name not in wanted):
            continue
        if not model.is_a_any(str(entity.type), model.relations[name].range):
            continue
        incoming = snapshot.incoming(entity.id, TermRef(name))
        if incoming:
            found.append(_Candidate(name, "in", tuple(incoming)))

    def order(candidate: _Candidate) -> tuple[bool, bool, bool, str]:
        bound = _bound(model, candidate.relation, candidate.direction)
        return (
            candidate.direction == "in",
            bound.max != 1,
            bound.min < 1,
            candidate.relation,
        )

    found.sort(key=order)
    return found, citations


def _relations(
    spec: RelationsSpec,
    candidates: Sequence[_Candidate],
    loader: Loader,
    model: CompiledModel,
    language: str | None,
) -> RelationsSection | None:
    groups: list[Group] = []
    unshown: list[str] = []
    if spec.only is not None:  # an authored list: these relations, in this order
        by_key = {(c.relation, c.direction): c for c in candidates}
        candidates = [by_key[key] for key in spec.only if key in by_key]
    for position, candidate in enumerate(candidates):
        if len(groups) >= spec.max_groups:
            unshown.extend(c.relation for c in candidates[position:])
            break
        group = _group(spec, candidate, loader, model, language)
        if group is not None:
            groups.append(group)
    more: list[str] = []
    for name in unshown:
        if name not in more:
            more.append(name)
    if not groups and not more:
        return None
    return RelationsSection(tuple(groups), tuple(more))


def _group(
    spec: RelationsSpec,
    candidate: _Candidate,
    loader: Loader,
    model: CompiledModel,
    language: str | None,
) -> Group | None:
    edges = candidate.edges
    inbound = candidate.direction == "in"
    sample = edges[: spec.load_max]
    admitted, loaded = loader.read([neighbour for neighbour, _ in sample])
    sample = sample[:admitted]  # what the budget of the answer allows
    rows: list[tuple[EntityId, EdgeRecord, Revision | None]] = []
    for neighbour, edge in sample:
        revision = loaded.get(neighbour)
        if inbound and (revision is None or revision.record_status != ACTIVE):
            continue  # an in-edge counts only while its source is active
        rows.append((neighbour, edge, revision))
    sampled = len(edges) > len(sample)
    count = (len(edges) if sampled else len(rows)) if inbound else len(edges)
    if count == 0:
        return None
    labels = {n: None if r is None else _main_label(model, r, language) for n, _edge, r in rows}
    types = {str(r.type) for _n, _e, r in rows if r is not None}
    ordered = sorted(rows, key=lambda row: _label_key(labels[row[0]], row[0]))
    items = tuple(
        _neighbour(spec, model, n, edge, r, labels[n], len(types) > 1)
        for n, edge, r in ordered[: spec.max_items]
    )
    states: dict[str, int] = {}
    attrs = set()
    for _n, _e, r in rows:
        if r is None or r.record_status != ACTIVE:
            continue
        attrs.add(_lifecycle_attribute(model, r))
        state = _state(model, r)
        if state is not None:
            states[state] = states.get(state, 0) + 1
    if len(attrs) != 1 or None in attrs or count <= len(items):
        states = {}  # one lifecycle attribute, and only when some neighbours are not shown
    bound = _bound(model, candidate.relation, candidate.direction)
    return Group(
        candidate.relation,
        candidate.direction,
        count,
        items,
        dict(sorted(states.items())),
        len(sample) if sampled else 0,
        inbound and sampled,
        max(0, bound.min - count),
    )


def _neighbour(
    spec: RelationsSpec,
    model: CompiledModel,
    neighbour: EntityId,
    edge: EdgeRecord,
    revision: Revision | None,
    label: str | None,
    with_type: bool,
) -> Neighbour:
    qualifiers = dict(edge.qualifiers)
    if revision is None:
        return Neighbour(str(neighbour), qualifiers=qualifiers, entity=neighbour)
    return Neighbour(
        revision.alias or str(revision.id),
        None if label is None else _cut(label, spec.label_chars)[0],
        str(revision.type) if with_type else None,
        _state(model, revision) if revision.record_status == ACTIVE else None,
        None if revision.record_status == ACTIVE else revision.record_status,
        qualifiers,
        revision.id,
        _key_field(spec, model, revision),
    )


def _key_field(
    spec: RelationsSpec, model: CompiledModel, revision: Revision
) -> tuple[str, str] | None:
    """The first key field of a neighbour that tells more than its label and state: its first
    ``required`` attribute, else its first ``unique`` one (by name), that has a text value.
    Read from the revision and the model the reader sees, so a hidden attribute is not there;
    no revision is loaded for it."""
    cls = model.classes.get(str(revision.type))
    if cls is None or spec.key_chars <= 0:
        return None
    attributes = cls.attributes
    for flag in ("required", "unique"):
        for name in sorted(attributes):
            attribute = attributes[name]
            if (
                not getattr(attribute, flag)
                or attribute.kind == "lifecycle"
                or not _plain(attribute)
            ):
                continue
            value = revision.attrs.get(name)
            text = text_of(value)
            if text:
                return name, _cut(text, spec.key_chars)[0]
    return None


def _page(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _locator(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _files(
    spec: FilesSpec,
    entity: Revision,
    is_file: bool,
    citations: Sequence[_Candidate],
    loader: Loader,
    model: CompiledModel,
    language: str | None,
) -> FilesSection | CitedBySection | None:
    if is_file:
        return _cited_by(spec, citations, loader, model, language)
    edges = [(e.target, e) for e in entity.edges if str(e.rel) == FILES_MODULE_RELATION]
    if not edges:
        return None
    sample = edges[: spec.load_max]
    admitted, loaded = loader.read([target for target, _ in sample])
    sample = sample[:admitted]
    rows: list[tuple[tuple[bool, str, str], int, int, str, FileRef]] = []
    for target, edge in sample:
        revision = loaded.get(target)
        title = None if revision is None else _main_label(model, revision, language)
        ref = FileRef(
            str(target) if revision is None else revision.alias or str(revision.id),
            None if title is None else _cut(title, spec.label_chars)[0],
            _page(edge.qualifiers.get("page")),
            _page(edge.qualifiers.get("last_page")),
            _locator(edge.qualifiers.get("locator")),
            target,
        )
        rows.append(
            (
                _label_key(title, target),
                ref.page or 0,
                ref.last_page or 0,
                ref.locator or "",
                ref,
            )
        )
    rows.sort(key=lambda row: row[:4])
    shown = tuple(row[4] for row in rows[: spec.max_items])
    return FilesSection(
        shown, max(0, len(edges) - len(shown)), len(sample) if len(edges) > len(sample) else 0
    )


def _merged_length(intervals: list[tuple[int, int]]) -> int:
    """The number of distinct pages the closed intervals cover (no page is enumerated)."""
    total = 0
    end = 0
    for low, high in sorted(intervals):
        low = max(low, end + 1)
        if high >= low:
            total += high - low + 1
            end = high
    return total


def _cited_by(
    spec: FilesSpec,
    citations: Sequence[_Candidate],
    loader: Loader,
    model: CompiledModel,
    language: str | None,
) -> CitedBySection | None:
    if not citations:
        return None
    edges = list(citations[0].edges)
    edges.sort(
        key=lambda pair: (
            _page(pair[1].qualifiers.get("page")) is None,
            _page(pair[1].qualifiers.get("page")) or 0,
            _page(pair[1].qualifiers.get("last_page")) or 0,
            str(pair[0]),
        )
    )
    sample = edges[: spec.load_max]
    admitted, loaded = loader.read([source for source, _ in sample])
    sample = sample[:admitted]
    rows = [
        (source, edge, revision)
        for source, edge in sample
        if (revision := loaded.get(source)) is not None and revision.record_status == ACTIVE
    ]
    sampled = len(edges) > len(sample)
    count = len(edges) if sampled else len(rows)
    if count == 0:
        return None
    # the page statistics: the active sources when all were read, else every edge (approximate)
    spans: list[tuple[int, int]] = []
    for _source, edge in edges if sampled else [(s, e) for s, e, _r in rows]:
        page = _page(edge.qualifiers.get("page"))
        if page is not None:
            last = _page(edge.qualifiers.get("last_page"))
            spans.append((page, max(page, last or page)))
    labelled = [(s, e, r, _main_label(model, r, language)) for s, e, r in rows]
    labelled.sort(
        key=lambda row: (
            _page(row[1].qualifiers.get("page")) is None,
            _page(row[1].qualifiers.get("page")) or 0,
            _label_key(row[3], row[0]),
        )
    )
    items = tuple(
        Citation(
            revision.alias or str(revision.id),
            None if label is None else _cut(label, spec.label_chars)[0],
            _page(edge.qualifiers.get("page")),
            _page(edge.qualifiers.get("last_page")),
            revision.id,
        )
        for _s, edge, revision, label in labelled[: spec.max_items]
    )
    return CitedBySection(
        count,
        items,
        min((low for low, _ in spans), default=None),
        max((high for _, high in spans), default=None),
        _merged_length(spans),
        len(sample) if sampled else 0,
        sampled,
    )


# ---- the byte guard --------------------------------------------------------------------------


type Stage = Callable[[Any], Iterator[Section]]
"""One trimming stage of one section: yields the section after each elementary step, so the
guard can stop as soon as the document fits. A generator keeps what a stage must remember."""


def _with(card: ViewModel, index: int, section: Section) -> ViewModel:
    """``card`` with the section at ``index`` replaced (never by kind: a view may have two of a
    kind and each is trimmed on its own)."""
    sections = list(card.sections)
    sections[index] = section
    return replace(card, sections=tuple(sections))


def _guard(card: ViewModel, limit: int) -> ViewModel:
    """Trim in a fixed order until the JSON fits ``limit``; header, label, state, the affordance
    summary and a section's error are never trimmed. The same input gives the same bytes.

    The stages come in a fixed order (``_STAGES``); a stage works through the sections it trims
    from the last to the first, one elementary step at a time, and the guard returns as soon as
    the document fits."""
    if card.size() <= limit:
        return card
    card = replace(card, cut=True)
    for kinds, stage in _STAGES:
        for index in reversed(range(len(card.sections))):
            if not isinstance(card.sections[index], kinds):
                continue
            for trimmed in stage(card.sections[index]):
                card = _with(card, index, trimmed)
                if card.size() <= limit:
                    return card
    if card.origin == AUTHORED:  # the generated card keeps its bytes: it has no such stages
        for final in _FINAL:
            for trimmed_card in final(card):
                card = trimmed_card
                if card.size() <= limit:
                    return card
    return card


def _row_without_values(row: Row) -> Row:
    lists = tuple(
        replace(g, items=tuple(replace(i, values=NO_VALUES) for i in g.items))
        for g in row.relations
    )
    return replace(row, values=NO_VALUES, relations=lists)


def _drop_row_values(section: NeighborsSection) -> Iterator[Section]:
    """First the attributes of the rows (and of their neighbour lists) go."""
    yield replace(section, items=tuple(_row_without_values(r) for r in section.items))


def _lists_to_one(section: NeighborsSection) -> Iterator[Section]:
    """Then the per-row neighbour lists shrink to one item each."""
    items = tuple(
        replace(r, relations=tuple(replace(g, items=g.items[:1]) for g in r.relations))
        for r in section.items
    )
    yield replace(section, items=items)


def _kept(section: NeighborsSection | RowsSection, keep: int) -> Section:
    """``section`` with its first ``keep`` rows (the counts stay)."""
    # the two kinds hold other items; ``replace`` does the same for both
    return replace(section, items=section.items[:keep])  # type: ignore[arg-type]


def _rows_to_few(section: NeighborsSection | RowsSection) -> Iterator[Section]:
    """Then the rows of a ``neighbors`` or ``rows`` section shrink to three, then to one."""
    for keep in (3, 1):
        if len(section.items) > keep:
            yield _kept(section, keep)


def _key_fields_go(section: RelationsSection) -> Iterator[Section]:
    """The key fields of the neighbours of the generated groups go first."""
    groups = tuple(
        replace(g, items=tuple(replace(i, key_field=None) for i in g.items)) for g in section.groups
    )
    yield replace(section, groups=groups)


def _group_items_to_one(section: RelationsSection) -> Iterator[Section]:
    """Items per group to 1, last group first."""
    groups = list(section.groups)
    for index in reversed(range(len(groups))):
        if len(groups[index].items) > 1:
            groups[index] = replace(groups[index], items=groups[index].items[:1])
            yield replace(section, groups=tuple(groups))


def _groups_to_more(section: RelationsSection) -> Iterator[Section]:
    """Then the trailing groups move to ``more_groups``."""
    groups = list(section.groups)
    moved: list[str] = []
    while groups:
        dropped = groups.pop()
        if dropped.relation not in moved:
            moved.insert(0, dropped.relation)
        names = [*moved, *(n for n in section.more_groups if n not in moved)]
        yield replace(section, groups=tuple(groups), more_groups=tuple(names))


def _values_shortened(section: FieldsSection) -> Iterator[Section]:
    """Then values shrink to 40 characters."""
    yield replace(section, items=tuple(_shorten(item) for item in section.items))


def _fields_to_other(section: FieldsSection) -> Iterator[Section]:
    """Then trailing fields move to ``other``."""
    items = list(section.items)
    moved_names: list[str] = []
    while True:
        last = next(
            (i for i in reversed(range(len(items))) if items[i].role not in ("label", "state")),
            None,
        )
        if last is None:
            return
        moved_names.append(items.pop(last).name)
        every = sorted({*section.other, *moved_names})
        yield replace(
            section,
            items=tuple(items),
            other=tuple(every[:OTHER_MAX]),
            other_more=section.other_more + max(0, len(every) - OTHER_MAX),
        )


def _files_to_few(section: FilesSection) -> Iterator[Section]:
    """File references to one, then none."""
    for keep in (1, 0):
        if len(section.items) > keep:
            yield replace(
                section,
                items=section.items[:keep],
                more=section.more + len(section.items) - keep,
            )


def _citations_to_few(section: CitedBySection) -> Iterator[Section]:
    """Citations to one, then none."""
    for keep in (1, 0):
        if len(section.items) > keep:
            yield replace(section, items=section.items[:keep])


def _no_rows(section: NeighborsSection | RowsSection) -> Iterator[Section]:
    """Last of all: the rows of an authored section go, the count stays."""
    if section.items:
        yield _kept(section, 0)


def _titles_go(card: ViewModel) -> Iterator[ViewModel]:
    """Last resort of an authored view: the section titles go, last section first."""
    for index in reversed(range(len(card.sections))):
        section = card.sections[index]
        if not isinstance(section, StubSection) and section.title is not None:
            card = _with(card, index, replace(section, title=None))
            yield card


def _labels_shortened(card: ViewModel) -> Iterator[ViewModel]:
    """Then the label and state of an authored ``fields`` section shrink to 40 characters and
    its alternative labels to one."""
    for index in reversed(range(len(card.sections))):
        section = card.sections[index]
        if isinstance(section, FieldsSection) and section.id is not None:
            card = _with(
                card, index, replace(section, items=tuple(_pared(i) for i in section.items))
            )
            yield card


def _pared(item: FieldItem) -> FieldItem:
    if isinstance(item.value, list) and item.role == "alt_label":
        return replace(item, value=item.value[:1], more=item.more + max(0, len(item.value) - 1))
    shrinks = item.role in ("label", "state") and isinstance(item.value, str)
    if shrinks and len(str(item.value)) > GUARD_VALUE_CHARS + 1:
        text, _ = _cut(str(item.value).removesuffix("…"), GUARD_VALUE_CHARS)
        return replace(item, value=text, chars=item.chars or len(str(item.value)))
    return item


def _sections_to_stubs(card: ViewModel) -> Iterator[ViewModel]:
    """Then whole sections go, last first, and leave their kind and id behind."""
    for index in reversed(range(len(card.sections))):
        section = card.sections[index]
        if not isinstance(section, StubSection):
            stub = StubSection(str(section.to_json()["kind"]), section.id)
            card = _with(card, index, stub)
            yield card


def _stubs_folded(card: ViewModel) -> Iterator[ViewModel]:
    """At the very end the trailing stubs fold into one count (``sections_cut``): at the lowest
    limits even 32 stubs would not fit, and the answer then says how many sections went."""
    while card.sections and isinstance(card.sections[-1], StubSection):
        card = replace(card, sections=card.sections[:-1], sections_cut=card.sections_cut + 1)
        yield card


_FINAL: Final[tuple[Callable[[ViewModel], Iterator[ViewModel]], ...]] = (
    _titles_go,
    _labels_shortened,
    _sections_to_stubs,
    _stubs_folded,
)

# The generated card has one section per kind, so for it the stages below run in the order it
# always had (relations, fields, files, citations); the authored kinds come first.
_STAGES: Final[tuple[tuple[type | tuple[type, ...], Stage], ...]] = (
    (NeighborsSection, _drop_row_values),
    (NeighborsSection, _lists_to_one),
    ((NeighborsSection, RowsSection), _rows_to_few),
    (RelationsSection, _key_fields_go),
    (RelationsSection, _group_items_to_one),
    (RelationsSection, _groups_to_more),
    (FieldsSection, _values_shortened),
    (FieldsSection, _fields_to_other),
    (FilesSection, _files_to_few),
    (CitedBySection, _citations_to_few),
    ((NeighborsSection, RowsSection), _no_rows),
)


def _shorten(item: FieldItem) -> FieldItem:
    if item.role in ("label", "state") or not isinstance(item.value, str):
        return item
    if len(item.value) <= GUARD_VALUE_CHARS + 1:
        return item
    text, _ = _cut(item.value.removesuffix("…"), GUARD_VALUE_CHARS)
    return replace(item, value=text, chars=item.chars or len(item.value))
