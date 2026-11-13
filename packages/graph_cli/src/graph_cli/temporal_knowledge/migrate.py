"""Plan a migration: every current revision under the target model (migration plan M2-M4).

``plan_migration`` is a pure function from the old and the new model, the derived renames, the
migration file and a snapshot to one new revision per entity that changes. It reads every
current revision of every record status (B8.3: non-active entities migrate too, status kept)
and writes nothing; the caller overlays ``MigrationPlan.overlay`` on the snapshot and judges it
with ``check_graph(new, overlay)``, and commits ``MigrationPlan.revisions`` as one tx.

Per entity, in this order:

1. the derived renames: the class (``Renames.classes``), the stored attributes
   (``Renames.attributes``, keyed by the new declaring class: an attribute is renamed when
   the old class of the entity declares it in a class whose new name is that key; the old
   model decides, so a retyped entity's unrelated attribute of the same name stays) and the
   relation of every edge;
2. the ``retype`` step whose ``from`` is the entity's original class (at most one per entity),
   with its attribute map; it takes part in step 1, so every later step sees the new class
   and the new attribute names;
3. the other steps in file order. A step naming a class applies to that class and its
   subclasses in the new model, judged by the entity's class after steps 1 and 2.

``fill`` and ``map_values`` write stored values directly: they bypass lifecycle transitions,
guards and mutability (maintenance). Nothing else is checked here.

A revision keeps ``id``, ``created_at``, ``alias``, ``record_status`` and the valid time; an
entity whose recomputed content hash equals the current one gets no revision.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import EntityId, TermRef, TxId
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import (
    RECORD_STATUSES,
    EdgeRecord,
    Revision,
    content_hash,
    content_projection,
)
from graph_cli.ontology_layer.data_validator import blob_values
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import (
    Convert,
    Drop,
    DropEdges,
    DropQualifier,
    Fill,
    MapQualifierValues,
    MapValues,
    MigrationDoc,
    RenameQualifier,
    Retype,
    Step,
)
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.values import coerce_text, with_language
from graph_cli.ontology_layer.vocabulary import LANG_BASES
from graph_cli.temporal_knowledge.working import WorkingState

_ALL: Query = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)
SAMPLES = 3  # entity ids kept per effect
_PREVIEW_FINDINGS = 20  # findings in ``MigrationPlan.to_json``

MANY_TO_SINGLE = "data.migration.many_to_single"


@dataclass(frozen=True, slots=True)
class MigrationStamp:
    """What every migrated revision records: the tx it belongs to and the target version.

    A dry run passes provisional values, as the write path does for dry runs (a placeholder
    ``tx_id``, ``tx_seq`` after the head, the clock's time)."""

    tx_id: TxId
    tx_seq: int
    recorded_at: datetime
    version: str  # the target ontology version


@dataclass(frozen=True, slots=True)
class Effect:
    """What one derived rename or one step did: the entities and edges it changed.

    ``kind`` is ``rename_class``, ``rename_relation``, ``rename_attribute`` or a step name
    (``retype``, ``fill``, ...); ``index`` is the position of a step in the file. ``by_type``
    counts the changed entities by their new class; ``samples`` are the smallest ids."""

    kind: str
    what: Mapping[str, JsonValue]
    entities: int
    edges: int
    by_type: Mapping[str, int]
    samples: tuple[EntityId, ...]
    index: int | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"kind": self.kind, **self.what}
        if self.index is not None:
            data["index"] = self.index
        data["entities"] = self.entities
        data["edges"] = self.edges
        data["by_type"] = dict(sorted(self.by_type.items()))
        data["samples"] = [str(e) for e in self.samples]
        return data


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """The result of ``plan_migration``.

    ``revisions`` holds the new revision of every migrated entity, sorted by id; ``examined``
    is the number of current revisions read (so ``examined - len(revisions)`` are unchanged).
    ``renames`` lists the derived renames (classes, relations, attributes, sorted), ``steps``
    one effect per step in file order; ``edges_changed`` counts the edges that were renamed,
    requalified or removed."""

    revisions: Mapping[EntityId, Revision]
    findings: tuple[Finding, ...]
    renames: tuple[Effect, ...]
    steps: tuple[Effect, ...]
    examined: int
    edges_changed: int
    by_type: Mapping[str, int]

    @property
    def unused_steps(self) -> tuple[int, ...]:
        """Indexes of the steps that changed nothing (``admin.migration_step_unused``)."""
        return tuple(e.index for e in self.steps if e.entities == 0 and e.index is not None)

    def to_json(self) -> dict[str, JsonValue]:
        """The preview: totals, one entry per derived rename and step, the first findings."""
        return {
            "entities": len(self.revisions),
            "unchanged": self.examined - len(self.revisions),
            "edges": self.edges_changed,
            "by_type": dict(sorted(self.by_type.items())),
            "renames": [e.to_json() for e in self.renames],
            "steps": [e.to_json() for e in self.steps],
            "unused_steps": list(self.unused_steps),
            "findings": {
                "count": len(self.findings),
                "items": [f.to_json() for f in self.findings[:_PREVIEW_FINDINGS]],
            },
        }

    def overlay(self, snapshot: Snapshot) -> WorkingState:
        """``snapshot`` with the migrated revisions on top, for ``check_graph``."""
        return WorkingState(
            base=snapshot,
            revisions=self.revisions,
            unchanged={},
            created=frozenset(),
            locals={},
            alias_allocations=(),
            resolution={},
            findings=(),
            effects=(),
        )

    def blobs(self, model: CompiledModel) -> tuple[str, ...]:
        """Every ``blob`` value of the migrated revisions under ``model``, sorted and unique
        (the tx's ``blobs``, as ``services.write`` lists them for a write)."""
        found = {h for r in self.revisions.values() for h in blob_values(model, r)}
        return tuple(sorted(found))


def plan_migration(
    old: CompiledModel,
    new: CompiledModel,
    renames: Renames,
    migration: MigrationDoc | None,
    snapshot: Snapshot,
    *,
    stamp: MigrationStamp,
    language: str | None = None,
) -> MigrationPlan:
    """The migrated revisions of every entity of ``snapshot`` that changes.

    ``renames`` are those of ``diff.derive_renames(old, new)``, ``migration`` is the checked
    file (``None`` = renames only), ``language`` the graph language that ``convert`` to
    ``lang_string`` and ``lang_text`` tags with. ``old`` is the model the snapshot was written
    under: it says which class declares a stored attribute."""
    engine = _Engine(
        old, new, renames, () if migration is None else migration.steps, stamp, language
    )
    items = snapshot.query(_ALL).items
    for revision in sorted(items, key=lambda r: r.id):
        engine.revise(revision)
    return engine.plan(len(items))


# ---- bookkeeping ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Tally:
    entities: int = 0
    edges: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    samples: list[EntityId] = field(default_factory=list)

    def add(self, entity: EntityId, type_: str, edges: int) -> None:
        self.entities += 1
        self.edges += edges
        self.by_type[type_] += 1
        if len(self.samples) < SAMPLES:  # entities arrive in id order
            self.samples.append(entity)

    def effect(self, kind: str, what: Mapping[str, JsonValue], index: int | None = None) -> Effect:
        return Effect(
            kind,
            what,
            self.entities,
            self.edges,
            MappingProxyType(dict(self.by_type)),
            tuple(self.samples),
            index,
        )


class _Entity:
    """One entity under change: the new class, attributes and edges, and what changed
    (counts of changed edges per effect key, flushed when the revision is written)."""

    def __init__(self, revision: Revision, type_: str) -> None:
        self.revision = revision
        self.type = type_
        self.attrs: dict[str, JsonValue] = {}
        self.edges: list[EdgeRecord] = list(revision.edges)
        self.touched: dict[tuple[str, ...], int] = {}
        self.findings: list[Finding] = []

    def touch(self, key: tuple[str, ...], edges: int = 0) -> None:
        self.touched[key] = self.touched.get(key, 0) + edges


class _Engine:
    def __init__(
        self,
        old: CompiledModel,
        new: CompiledModel,
        renames: Renames,
        steps: Sequence[Step],
        stamp: MigrationStamp,
        language: str | None,
    ) -> None:
        self.old = old
        self.new = new
        self.renames = renames
        self.steps = tuple(steps)
        self.stamp = stamp
        self.language = language
        self.retypes = [(i, s) for i, s in enumerate(self.steps) if isinstance(s, Retype)]
        self.fills = {
            i: self.fill_value(s) for i, s in enumerate(self.steps) if isinstance(s, Fill)
        }
        for step in self.steps:
            if isinstance(step, Convert) and step.to in LANG_BASES and language is None:
                raise ValueError(f"convert to {step.to} needs the graph language")
        self.tallies: dict[tuple[str, ...], _Tally] = {}
        self.revisions: dict[EntityId, Revision] = {}
        self.findings: list[Finding] = []
        self.by_type: Counter[str] = Counter()
        self.edges_changed = 0
        self._renamed: dict[tuple[str, str], tuple[tuple[str, str], str] | None] = {}
        self._in_type: dict[tuple[str, str], bool] = {}

    # ---- model helpers -------------------------------------------------------------------

    def attribute(self, type_: str, name: str, step: str) -> AttributeDef:
        cls = self.new.classes.get(type_)
        found = None if cls is None else cls.attributes.get(name)
        if found is None:
            raise ValueError(f"{step}: {type_} has no attribute {name!r} in the new model")
        return found

    def fill_value(self, step: Fill) -> JsonValue:
        """The stored form of ``fill.value``: the text conversion of ``attrs_text`` (what a
        write would store), a list for a ``many`` attribute."""
        attribute = self.attribute(step.type, step.attribute, "fill")
        value: JsonValue
        if attribute.kind in ("enum", "lifecycle"):
            value = step.value
        else:
            base = self.new.value_base(attribute.kind, attribute.type)
            value = coerce_text(base, step.value)
            if base in LANG_BASES:
                value = with_language(value, self.language)
        return [value] if attribute.many else value

    def is_a(self, type_: str, ancestor: str) -> bool:
        key = (type_, ancestor)
        found = self._in_type.get(key)
        if found is None:
            found = self._in_type[key] = self.new.is_a(type_, ancestor)
        return found

    def attribute_rename(self, original: str, name: str) -> tuple[tuple[str, str], str] | None:
        """``((declaring class, name), new name)`` when the stored attribute ``name`` of an
        entity of the old class ``original`` is renamed by ``renamed_from``: the class that
        declares it in the old model, under its new name, keys ``Renames.attributes``."""
        key = (original, name)
        if key not in self._renamed:
            found = None
            cls = self.old.classes.get(original)
            attribute = None if cls is None else cls.attributes.get(name)
            if attribute is not None:
                owner = self.renames.classes.get(attribute.declared_in, attribute.declared_in)
                target = self.renames.attributes.get((owner, name))
                found = None if target is None else ((owner, name), target)
            self._renamed[key] = found
        return self._renamed[key]

    # ---- one entity ----------------------------------------------------------------------

    def revise(self, revision: Revision) -> None:
        original = str(revision.type)
        found = next(((i, s) for i, s in self.retypes if s.from_ == original), None)
        retype = None if found is None else found[1]
        renamed = self.renames.classes.get(original, original)
        entity = _Entity(revision, retype.to if retype is not None else renamed)
        if found is not None:
            entity.touch(("step", str(found[0])))
        elif renamed != original:
            entity.touch(("class", original))
        self.rename_attributes(entity, original, retype)
        self.rename_relations(entity)
        for index, step in enumerate(self.steps):
            self.apply(entity, index, step)
        self.finish(entity)

    def rename_attributes(self, entity: _Entity, original: str, retype: Retype | None) -> None:
        explicit = {} if retype is None else retype.attributes
        renamed: dict[str, JsonValue] = {}
        for name, value in entity.revision.attrs.items():
            if name in explicit:
                renamed[explicit[name]] = value
            elif (derived := self.attribute_rename(original, name)) is not None:
                key, target = derived
                renamed[target] = value
                entity.touch(("attribute", *key))
            else:
                entity.attrs[name] = value
        entity.attrs.update(renamed)  # a renamed value wins a clash with a stored name

    def rename_relations(self, entity: _Entity) -> None:
        if not self.renames.relations:
            return
        for position, edge in enumerate(entity.edges):
            target = self.renames.relations.get(edge.rel)
            if target is not None:
                entity.edges[position] = replace(edge, rel=TermRef(target))
                entity.touch(("relation", str(edge.rel)), 1)

    def apply(self, entity: _Entity, index: int, step: Step) -> None:
        key = ("step", str(index))
        match step:
            case Retype():
                return  # applied with the renames
            case Fill():
                if self.is_a(entity.type, step.type) and entity.attrs.get(step.attribute) in (
                    None,
                    [],
                ):
                    entity.attrs[step.attribute] = self.fills[index]
                    entity.touch(key)
            case MapValues():
                if self.is_a(entity.type, step.type):
                    self.map_attribute(entity, key, step)
            case Drop():
                if self.is_a(entity.type, step.type) and step.attribute in entity.attrs:
                    del entity.attrs[step.attribute]
                    entity.touch(key)
            case Convert():
                if (
                    self.is_a(entity.type, step.type)
                    and entity.attrs.get(step.attribute) is not None
                ):
                    self.convert(entity, key, step)
            case DropEdges():
                kept = [e for e in entity.edges if e.rel != step.relation]
                if len(kept) != len(entity.edges):
                    entity.touch(key, len(entity.edges) - len(kept))
                    entity.edges = kept
            case MapQualifierValues() | RenameQualifier() | DropQualifier():
                self.qualifiers(entity, key, step)

    def map_attribute(self, entity: _Entity, key: tuple[str, ...], step: MapValues) -> None:
        value = entity.attrs.get(step.attribute)
        mapped = _map_value(value, step.values)
        if mapped != value:
            entity.attrs[step.attribute] = mapped
            entity.touch(key)

    def qualifiers(
        self,
        entity: _Entity,
        key: tuple[str, ...],
        step: MapQualifierValues | RenameQualifier | DropQualifier,
    ) -> None:
        changed = 0
        for position, edge in enumerate(entity.edges):
            if edge.rel != step.relation:
                continue
            qualifiers = _change_qualifiers(dict(edge.qualifiers), step)
            if qualifiers != edge.qualifiers:
                entity.edges[position] = replace(edge, qualifiers=qualifiers)
                changed += 1
        if changed:
            entity.touch(key, changed)

    def convert(self, entity: _Entity, key: tuple[str, ...], step: Convert) -> None:
        value = entity.attrs[step.attribute]
        converted: JsonValue = value
        if step.to in LANG_BASES:
            converted = _tag(value, self.language)
        elif step.to == "many":
            converted = value if isinstance(value, list) else [value]
        elif isinstance(value, list):
            if len(value) > 1:
                entity.findings.append(self.too_many(entity, step, len(value)))
                return
            converted = value[0] if value else None
        if converted == value:
            return
        if converted is None:
            del entity.attrs[step.attribute]
        else:
            entity.attrs[step.attribute] = converted
        entity.touch(key)

    def too_many(self, entity: _Entity, step: Convert, count: int) -> Finding:
        return Finding(
            Severity.ERROR,
            MANY_TO_SINGLE,
            {"id": str(entity.revision.id), "type": entity.type, "path": f"attrs.{step.attribute}"},
            f"{step.attribute!r} holds {count} values and cannot become single",
            expected="at most 1 value",
            actual=count,
        )

    # ---- result --------------------------------------------------------------------------

    def finish(self, entity: _Entity) -> None:
        previous = entity.revision
        edges = tuple(entity.edges)
        same = (
            entity.type == previous.type
            and entity.attrs == previous.attrs
            and edges == previous.edges
        )
        projection = content_projection(
            type=entity.type,
            record_status=previous.record_status,
            alias=previous.alias,
            attrs=entity.attrs,
            edges=edges,
            valid_from=previous.valid_from,
            valid_to=previous.valid_to,
        )
        digest = previous.content_hash if same else content_hash(projection)
        self.findings += entity.findings
        if digest == previous.content_hash:
            return
        stamp = self.stamp
        self.revisions[previous.id] = Revision(
            id=previous.id,
            rev=previous.rev + 1,
            prev_rev=previous.rev,
            type=TermRef(entity.type),
            record_status=previous.record_status,
            attrs=entity.attrs,
            edges=edges,
            alias=previous.alias,
            valid_from=previous.valid_from,
            valid_to=previous.valid_to,
            created_at=previous.created_at,
            recorded_at=stamp.recorded_at,
            tx_id=stamp.tx_id,
            tx_seq=stamp.tx_seq,
            ontology_version=stamp.version,
            content_hash=digest,
        )
        self.by_type[entity.type] += 1
        self.edges_changed += _edges_changed(previous.edges, edges)
        for key, edge_count in entity.touched.items():
            self.tallies.setdefault(key, _Tally()).add(previous.id, entity.type, edge_count)

    def plan(self, examined: int) -> MigrationPlan:
        def tally(key: tuple[str, ...]) -> _Tally:
            return self.tallies.get(key, _Tally())

        renames = self.renames
        effects = [
            tally(("class", old)).effect("rename_class", {"from": old, "to": new})
            for old, new in sorted(renames.classes.items())
        ]
        effects += [
            tally(("relation", old)).effect("rename_relation", {"from": old, "to": new})
            for old, new in sorted(renames.relations.items())
        ]
        effects += [
            tally(("attribute", owner, old)).effect(
                "rename_attribute", {"class": owner, "from": old, "to": new}
            )
            for (owner, old), new in sorted(renames.attributes.items())
        ]
        steps = []
        for index, step in enumerate(self.steps):
            body = step.to_json()
            assert isinstance(body, dict)
            what = body[step.NAME]
            assert isinstance(what, dict)
            steps.append(tally(("step", str(index))).effect(step.NAME, what, index))
        return MigrationPlan(
            revisions=MappingProxyType(dict(sorted(self.revisions.items()))),
            findings=tuple(self.findings),
            renames=tuple(effects),
            steps=tuple(steps),
            examined=examined,
            edges_changed=self.edges_changed,
            by_type=MappingProxyType(dict(self.by_type)),
        )


# ---- value helpers -------------------------------------------------------------------------


def _map_value(value: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """Enum values and states of a single value or a ``many`` list; others stay."""
    if isinstance(value, str):
        return values.get(value, value)
    if isinstance(value, list):
        return [values.get(v, v) if isinstance(v, str) else v for v in value]
    return value


def _tag(value: JsonValue, language: str | None) -> JsonValue:
    """Plain text of a single value or a ``many`` list tagged with the graph language."""
    if isinstance(value, list):
        return [with_language(v, language) if isinstance(v, str) else v for v in value]
    return with_language(value, language) if isinstance(value, str) else value


def _change_qualifiers(
    qualifiers: dict[str, JsonValue], step: MapQualifierValues | RenameQualifier | DropQualifier
) -> dict[str, JsonValue]:
    match step:
        case MapQualifierValues():
            if step.qualifier in qualifiers:
                qualifiers[step.qualifier] = _map_value(qualifiers[step.qualifier], step.values)
        case RenameQualifier():
            if step.from_ in qualifiers:
                qualifiers[step.to] = qualifiers.pop(step.from_)
        case DropQualifier():
            qualifiers.pop(step.qualifier, None)
    return qualifiers


def _edges_changed(before: Sequence[EdgeRecord], after: Sequence[EdgeRecord]) -> int:
    """Edges of ``before`` that are gone or differ in ``after`` (by edge id)."""
    kept = {e.edge_id: e for e in after}
    return sum(1 for e in before if kept.get(e.edge_id) != e)
