"""Rows of views: neighbours, and later listings, as lean rows (plan V5).

A row is an entity as a view projects it: its reference (the alias, else the id: the stable key a
consumer joins by), its label, the attributes asked for, and per-row neighbour lists. Nothing
here reads a store: everything comes through the ``Snapshot`` that is passed in, which is the
reader's filtered one, so a hidden entity is not a row, an edge to one is not a neighbour and a
masked value is absent.

``RowBuilder`` is the one builder. Its work is bounded: every group reads at most ``load_max``
edges and every revision it loads is charged to a shared ``Loads`` budget; a group that could not
read all its edges says so (``sampled``), and what is cut by ``max`` is counted (``more``).
Order follows the spec (the ``find`` sort grammar, planned by the caller) or, by default, the
card's order: normalized label, then id. The out-relations of a ``list`` row (``edge_order``) are
the exception: they default to the edge order ``find`` gives, so a set view replaces its reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import OrderKey, Pred
from graph_cli.kernel.query_eval import matches, sort_revisions
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kinetic.view_format import ROW_RELATION_MAX_DEFAULT, FieldSel
from graph_cli.kinetic.view_values import (
    cut,
    field_value,
    has_value,
    label_key,
    main_label,
    state_of,
)
from graph_cli.ontology_layer.affordances import ACTIVE
from graph_cli.ontology_layer.model import CompiledModel

ROW_LOAD_MAX: Final = 500  # edges one group reads (the largest ``max`` of a neighbour list)
LIST_ITEMS: Final = 5  # elements of a list value shown in an entity view

Direction = Literal["out", "in"]
Json = dict[str, JsonValue]
type Edges = Sequence[tuple[EntityId, EdgeRecord]]


class Loads:
    """A budget of revisions an answer may load; ``take`` hands out what is left."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.used = 0

    def take(self, wanted: int) -> int:
        granted = max(0, min(wanted, self.left))
        self.left -= granted
        self.used += granted
        return granted


class Loader:
    """The revisions of one answer: every revision is read once (a second section that needs it
    gets it from here) and each new one is charged to the budget. ``read`` admits a leading part
    of the ids it is asked for, so a section that cannot read all its neighbours knows it."""

    def __init__(self, snapshot: Snapshot, loads: Loads) -> None:
        self.snapshot = snapshot
        self.loads = loads
        self._seen: dict[EntityId, Revision | None] = {}

    def read(self, ids: Sequence[EntityId]) -> tuple[int, dict[EntityId, Revision]]:
        """``(n, revisions)``: the first ``n`` ids may be read (within the budget; ids read
        before are free) and ``revisions`` holds those that exist."""
        admitted = 0
        fresh: list[EntityId] = []
        for entity in ids:
            if entity not in self._seen and entity not in fresh:
                if self.loads.take(1) == 0:
                    break
                fresh.append(entity)
            admitted += 1
        if fresh:
            found = self.snapshot.get_many(fresh)
            for entity in fresh:
                self._seen[entity] = found.get(entity)
        wanted = ids[:admitted]
        return admitted, {e: r for e in wanted if (r := self._seen.get(e)) is not None}


@dataclass(frozen=True, slots=True)
class RowRelationSpec:
    """One per-row neighbour list; ``relation`` is ``None`` for ``all`` (every out-relation, keys
    only). ``where`` and ``order`` are planned (``plan_find``) with parameters substituted."""

    relation: str | None = None
    direction: Direction = "out"
    fields: FieldSel = None
    where: Pred | None = None
    order: tuple[OrderKey, ...] = ()
    max: int = ROW_RELATION_MAX_DEFAULT
    edge_order: bool = False  # out-relation without ``order``: the edges as ``find`` shows them


@dataclass(frozen=True, slots=True)
class Values:
    """The attributes of one entity as shown: the values and what was cut from them."""

    fields: Mapping[str, JsonValue] = field(default_factory=dict)
    chars: Mapping[str, int] = field(default_factory=dict)  # length of a string that was cut
    more: Mapping[str, int] = field(default_factory=dict)  # elements of a list that were cut

    def put(self, found: Json) -> None:
        if self.fields:
            found["fields"] = dict(self.fields)
        if self.chars:
            found["chars"] = dict(self.chars)
        if self.more:
            found["more"] = dict(self.more)


NO_VALUES: Final = Values()


@dataclass(frozen=True, slots=True)
class RowItem:
    """A neighbour in a per-row list."""

    ref: str
    label: str | None = None
    type: str | None = None  # only when the group's neighbours have more than one type
    state: str | None = None
    record_status: str | None = None  # only when the neighbour is not active
    qualifiers: Mapping[str, JsonValue] = field(default_factory=dict)
    values: Values = NO_VALUES
    entity: EntityId | None = None  # for the read signal; not in the JSON

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
        if self.qualifiers:
            found["qualifiers"] = dict(self.qualifiers)
        self.values.put(found)
        return found


@dataclass(frozen=True, slots=True)
class RowGroup:
    relation: str
    direction: Direction
    count: int
    items: tuple[RowItem, ...]
    sampled: int = 0  # the number of edges the items come from, when not all were read
    approx: bool = False  # ``count`` is the raw in-edge number, or a lower bound (see ``unread``)
    unread: int = 0  # edges that were not read: ``count`` may be off by up to this many

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found: Json = {"relation": self.relation, "direction": self.direction, "count": self.count}
        if self.items:
            found["items"] = [i.to_json() for i in self.items]
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
class Row:
    ref: str
    label: str | None = None
    type: str | None = None  # only when the section's rows have more than one type
    record_status: str | None = None  # only when the entity is not active
    qualifiers: Mapping[str, JsonValue] = field(default_factory=dict)  # of the edge to the entity
    values: Values = NO_VALUES
    relations: tuple[RowGroup, ...] = ()
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("label", self.label),
            ("type", self.type),
            ("record_status", self.record_status),
        ):
            if value is not None:
                found[key] = value
        if self.qualifiers:
            found["qualifiers"] = dict(self.qualifiers)
        self.values.put(found)
        if self.relations:
            found["relations"] = [g.to_json() for g in self.relations]
        return found

    def named(self) -> tuple[EntityId, ...]:
        """The entities the row names besides itself: the neighbours in its lists."""
        return tuple(i.entity for g in self.relations for i in g.items if i.entity is not None)


@dataclass(frozen=True, slots=True)
class Selection:
    """The edges of one relation as rows: filtered, ordered, with the exact count."""

    rows: tuple[tuple[EntityId, EdgeRecord, Revision], ...]
    count: int
    sampled: int  # edges read, when the group has more
    approx: bool  # edges were not all read: the count is a raw number or a bound (see ``unread``)
    unread: int = 0  # the edges that were not read: the count may be off by up to this many


@dataclass(frozen=True, slots=True)
class RowsResult:
    """The result of a published function for a ``rows`` section, handed in by the service (the
    function runs there): column names, rows as JSON cells, or the error that ended it."""

    function: str
    hash: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[Mapping[str, JsonValue], ...] = ()
    error: tuple[str, str] | None = None  # (code, message)


class RowBuilder:
    """Builds rows and per-row neighbour lists from a snapshot and a model (pure)."""

    def __init__(
        self,
        snapshot: Snapshot,
        model: CompiledModel,
        language: str | None,
        *,
        loads: Loads,
        loader: Loader | None = None,
        value_chars: int | None = None,
        list_items: int = LIST_ITEMS,
        label_chars: int | None = None,
        load_max: int = ROW_LOAD_MAX,
    ) -> None:
        self.snapshot = snapshot
        self.model = model
        self.language = language
        self.loader = loader or Loader(snapshot, loads)
        self.loads = self.loader.loads
        self.value_chars = value_chars  # None: values are never cut (set views)
        self.list_items = list_items
        self.label_chars = label_chars
        self.load_max = load_max

    # Values

    def values(self, revision: Revision, sel: FieldSel) -> Values:
        """The attributes asked for that have a value, as ``get`` shows them (cut in an entity
        view). ``all`` is every attribute the reader sees, never a ``blob``, by name."""
        cls = self.model.classes.get(str(revision.type))
        known = {} if cls is None else cls.attributes
        if sel is None:
            return NO_VALUES
        if sel == "all":
            names: Sequence[str] = sorted(n for n, a in known.items() if a.type != "blob")
        else:
            assert isinstance(sel, tuple)
            names = sel
        shown: dict[str, JsonValue] = {}
        chars: dict[str, int] = {}
        more: dict[str, int] = {}
        for name in names:
            value = revision.attrs.get(name)
            if name not in known or not has_value(value):
                continue
            if self.value_chars is None:
                shown[name] = value
                continue
            text, length, rest = field_value(value, self.value_chars, self.list_items)
            shown[name] = text
            if length is not None:
                chars[name] = length
            if rest:
                more[name] = rest
        return Values(shown, chars, more)

    def label(self, revision: Revision) -> str | None:
        text = main_label(self.model, revision, self.language)
        if text is None or self.label_chars is None:
            return text
        return cut(text, self.label_chars)[0]

    # Edges

    def select(
        self,
        edges: Edges,
        direction: Direction,
        where: Pred | None,
        order: Sequence[OrderKey],
        *,
        edge_order: bool = False,
    ) -> Selection:
        """Read the neighbours of ``edges`` (at most ``load_max``, within the budget), keep the
        in-edges of active sources and the entries that match ``where``, and order them
        (``edge_order``: as the edges come, when ``order`` is empty)."""
        wanted = edges[: self.load_max]
        admitted, loaded = self.loader.read([n for n, _ in wanted])
        sample = wanted[:admitted]
        inbound = direction == "in"
        rows: list[tuple[EntityId, EdgeRecord, Revision]] = []
        for neighbour, edge in sample:
            revision = loaded.get(neighbour)
            if revision is None:
                continue  # not readable for this reader: no row, no count, no id
            if inbound and revision.record_status != ACTIVE:
                continue  # an in-edge counts only while its source is active
            if where is not None and not matches(revision, where):
                continue
            rows.append((neighbour, edge, revision))
        partial = len(edges) > len(sample)
        unread = len(edges) - len(sample)
        if where is not None:
            count = len(rows)  # a lower bound when edges were not read (``unread``)
        elif inbound:
            count = len(edges) if partial else len(rows)  # raw when not all were read
        else:
            count = len(rows) + unread  # out-edges not read are counted as they stand
        return Selection(
            tuple(rows if edge_order and not order else self._ordered(rows, order)),
            count,
            len(sample) if partial else 0,
            partial,
            unread,
        )

    def _ordered(
        self,
        rows: list[tuple[EntityId, EdgeRecord, Revision]],
        order: Sequence[OrderKey],
    ) -> list[tuple[EntityId, EdgeRecord, Revision]]:
        if order:
            ranked = sort_revisions([r for _n, _e, r in rows], order)
            rank = {r.id: i for i, r in enumerate(ranked)}
            return sorted(rows, key=lambda row: (rank.get(row[0], 0), str(row[0])))
        labels = {n: main_label(self.model, r, self.language) for n, _e, r in rows}
        return sorted(rows, key=lambda row: label_key(labels[row[0]], row[0]))

    # Rows

    def rows(
        self,
        selection: Selection,
        fields: FieldSel,
        relations: Sequence[RowRelationSpec],
        limit: int,
    ) -> tuple[Row, ...]:
        """The first ``limit`` entries of ``selection`` as rows."""
        types = {str(r.type) for _n, _e, r in selection.rows}
        return tuple(
            self.row(revision, neighbour, edge, fields, relations, len(types) > 1)
            for neighbour, edge, revision in selection.rows[:limit]
        )

    def row(
        self,
        revision: Revision,
        entity: EntityId,
        edge: EdgeRecord | None,
        fields: FieldSel,
        relations: Sequence[RowRelationSpec],
        with_type: bool,
    ) -> Row:
        qualifiers = {} if edge is None else dict(edge.qualifiers)
        return Row(
            revision.alias or str(revision.id),
            self.label(revision),
            str(revision.type) if with_type else None,
            None if revision.record_status == ACTIVE else revision.record_status,
            qualifiers,
            self.values(revision, fields),
            self.groups(revision, relations),
            revision.id,
        )

    # Per-row neighbour lists

    def groups(
        self, revision: Revision, relations: Sequence[RowRelationSpec]
    ) -> tuple[RowGroup, ...]:
        found: list[RowGroup] = []
        for spec in relations:
            if spec.relation is None:
                names = sorted({str(e.rel) for e in revision.edges})
                found.extend(
                    g
                    for name in names
                    if (
                        g := self.group(
                            revision,
                            RowRelationSpec(name, "out", None, None, (), spec.max, spec.edge_order),
                            keys_only=True,
                        )
                    )
                )
            elif (group := self.group(revision, spec)) is not None:
                found.append(group)
        return tuple(found)

    def group(
        self, revision: Revision, spec: RowRelationSpec, *, keys_only: bool = False
    ) -> RowGroup | None:
        assert spec.relation is not None
        if spec.direction == "out":
            edges: Edges = [(e.target, e) for e in revision.edges if str(e.rel) == spec.relation]
        else:
            edges = self.snapshot.incoming(revision.id, TermRef(spec.relation))
        if not edges:
            return None
        selection = self.select(
            edges,
            spec.direction,
            spec.where,
            spec.order,
            edge_order=spec.edge_order and spec.direction == "out",
        )
        if selection.count == 0:
            return None
        types = {str(r.type) for _n, _e, r in selection.rows}
        items = tuple(
            self.item(revision_, neighbour, edge, spec.fields, len(types) > 1, keys_only)
            for neighbour, edge, revision_ in selection.rows[: spec.max]
        )
        return RowGroup(
            spec.relation,
            spec.direction,
            selection.count,
            items,
            selection.sampled,
            selection.approx,
            selection.unread,
        )

    def item(
        self,
        revision: Revision,
        neighbour: EntityId,
        edge: EdgeRecord,
        fields: FieldSel,
        with_type: bool,
        keys_only: bool,
    ) -> RowItem:
        qualifiers = dict(edge.qualifiers)
        active = revision.record_status == ACTIVE
        return RowItem(
            revision.alias or str(revision.id),
            None if keys_only else self.label(revision),
            str(revision.type) if with_type else None,
            state_of(self.model, revision) if active and not keys_only else None,
            None if active else revision.record_status,
            qualifiers,
            self.values(revision, fields),
            revision.id,
        )
