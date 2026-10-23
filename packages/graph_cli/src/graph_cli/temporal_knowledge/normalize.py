"""``normalize``: apply the ops of one change set to a working copy (contracts B3.6, B8, W.10).

Pure and deterministic given its inputs (B8.14). Ops apply in array order;
later ops see the effects of earlier ones; every touched entity gets exactly
one new revision, unless its content after all ops equals its current
revision (C.9): then it is unchanged and gets none. Normalize applies, the
validator judges: the only findings produced here are ``data.temporal.*``
(R.4); everything else is judged by ``ontology_layer.data_validator`` from
the post-state and the recorded ``OpEffect`` per op, so each problem is
reported once.

- **Ids.** One entity id per create, in op order, before anything else (so
  locals may be referenced before their create); then edge ids in op order:
  one per edge of a create, per ``link``, per supersede target and per
  ``retract`` with ``by`` (W.9).
- **Creates.** Absent attributes take their default; an absent lifecycle
  attribute takes the lifecycle's single initial state (none when it has
  several). ``record_status = active``.
- **Text values** (``attrs_text``, ``set_text``) are coerced per base type
  (``values.coerce_text``, B3.16); a value that does not coerce, or an
  unknown attribute, stays the given string; a ``many`` attribute becomes a
  one-element list.
- **List changes** (``add``, ``remove`` and their ``_text`` forms, coerced
  per item) apply to ``many`` attributes only: ``add`` appends each value
  not yet present (canonical equality), ``remove`` drops every equal value,
  a list left empty is unset. On any other attribute nothing changes and
  the validator reports it from ``OpEffect.lists``.
- **Aliases** (B1.8): a create of a type with a configured prefix (exact
  type) gets ``kernel.ids.format_alias`` (``<PREFIX>-<n:04d>``);
  ``set_alias`` renames, and an explicit alias above an auto prefix's
  counter advances it. Each entity keeps at
  most one allocation per tx.
- **Supersede, retract** set ``record_status`` and add their ``core:``
  edges; ``core:derived_from`` edges added without ``target_rev`` get the
  target's revision in the post-state.
- **Skipped ops** (with a finding): any op that changes a non-active entity
  (``record_not_active``), ``link``/``unlink``/create edges with
  ``core:supersedes`` or ``core:retracts`` (``reserved_relation``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    CreateOp,
    LinkOp,
    Op,
    OpEffect,
    RetractOp,
    SetAliasOp,
    SupersedeOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
)
from graph_cli.kernel.clock import parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import (
    AbbrevRef,
    AliasRef,
    EdgeId,
    EntityId,
    EntityRef,
    LocalRef,
    TermRef,
    TxId,
    format_alias,
)
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_eval import strict_equal
from graph_cli.kernel.records import (
    EdgeRecord,
    RecordStatus,
    Revision,
    content_hash,
    content_projection,
)
from graph_cli.ontology_layer.model import ClassDef, CompiledModel
from graph_cli.ontology_layer.values import coerce_text
from graph_cli.temporal_knowledge.working import WorkingState

SUPERSEDES: Final = TermRef("core:supersedes")
RETRACTS: Final = TermRef("core:retracts")
DERIVED_FROM: Final = TermRef("core:derived_from")
RESERVED_RELATIONS: Final = frozenset({SUPERSEDES, RETRACTS})


@dataclass
class _Work:
    """The mutable working copy of one touched entity."""

    type: TermRef
    record_status: RecordStatus
    attrs: dict[str, JsonValue]
    edges: list[EdgeRecord]
    alias: str | None
    valid_from: str | None
    valid_to: str | None
    previous: Revision | None

    @classmethod
    def of(cls, revision: Revision) -> _Work:
        return cls(
            type=revision.type,
            record_status=revision.record_status,
            attrs=dict(revision.attrs),
            edges=list(revision.edges),
            alias=revision.alias,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
            previous=revision,
        )


@dataclass
class _Run:
    base: Snapshot
    model: CompiledModel
    refs: Mapping[str, EntityId]
    new_id: Callable[[], UUID]
    alias_prefixes: Mapping[TermRef, str]
    allocate_aliases: bool
    counters: dict[str, int]
    work: dict[EntityId, _Work] = field(default_factory=dict)
    created: dict[EntityId, None] = field(default_factory=dict)
    doc_locals: dict[str, EntityId] = field(default_factory=dict)
    used_refs: dict[str, EntityId] = field(default_factory=dict)
    allocations: dict[EntityId, str] = field(default_factory=dict)
    added_edges: set[EdgeId] = field(default_factory=set)
    unchanged: dict[EntityId, Revision] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    effects: list[OpEffect] = field(default_factory=list)

    # ---- references and state ------------------------------------------

    def resolve(self, ref: EntityRef) -> EntityId:
        match ref:
            case LocalRef(name):
                if name in self.doc_locals:
                    return self.doc_locals[name]
                key = f"@{name}"
            case AliasRef(alias):
                key = alias
            case AbbrevRef():
                raise GraphCliError("internal.error", "abbreviations are resolved by services")
            case _:
                return ref
        if key not in self.refs:
            raise GraphCliError("internal.error", f"reference {key!r} was not resolved")
        self.used_refs[key] = self.refs[key]
        return self.refs[key]

    def peek(self, entity: EntityId) -> _Work | None:
        if entity in self.work:
            return self.work[entity]
        revision = self.base.get(entity)
        return None if revision is None else _Work.of(revision)

    def touch(self, entity: EntityId) -> _Work:
        found = self.peek(entity)
        if found is None:
            raise GraphCliError("internal.error", f"entity {entity} does not exist")
        self.work[entity] = found
        return found

    def cls(self, type: str) -> ClassDef | None:
        return self.model.classes.get(type)

    def local_of(self, entity: EntityId) -> str | None:
        for name, bound in self.doc_locals.items():
            if bound == entity:
                return name
        for key, bound in self.refs.items():
            if key.startswith("@") and bound == entity:
                return key[1:]
        return None

    def finding(self, code: str, entity: EntityId, path: str, message: str) -> None:
        current = self.peek(entity)
        target: dict[str, JsonValue] = {
            "id": str(entity),
            "type": current.type if current is not None else None,
            "path": path,
        }
        local = self.local_of(entity)
        if local is not None:
            target["local"] = local
        self.findings.append(Finding(Severity.ERROR, code, target, message))

    def active_or_report(self, entity: EntityId, op: str) -> bool:
        current = self.peek(entity)
        if current is None:
            raise GraphCliError("internal.error", f"entity {entity} does not exist")
        if current.record_status == "active":
            return True
        self.finding(
            "data.temporal.record_not_active",
            entity,
            "record_status",
            f"{op} refused: the entity is {current.record_status}",
        )
        return False

    def new_edge(
        self,
        rel: TermRef,
        target: EntityId,
        qualifiers: Mapping[str, JsonValue],
        target_rev: int | None,
    ) -> EdgeRecord:
        edge = EdgeRecord(EdgeId(self.new_id()), rel, target, dict(qualifiers), target_rev)
        self.added_edges.add(edge.edge_id)
        return edge

    def coerce(self, type: str, name: str, text: str) -> JsonValue:
        cls = self.cls(type)
        attribute = None if cls is None else cls.attributes.get(name)
        value = self.coerce_item(type, name, text)
        return [value] if attribute is not None and attribute.many else value

    def coerce_item(self, type: str, name: str, text: str) -> JsonValue:
        """One value of the attribute from text; the text itself if it does not coerce."""
        cls = self.cls(type)
        attribute = None if cls is None else cls.attributes.get(name)
        if attribute is None:
            return text
        try:
            return coerce_text(self.model.value_base(attribute.kind, attribute.type), text)
        except ValueError:
            return text

    def is_list(self, type: str, name: str) -> bool:
        cls = self.cls(type)
        attribute = None if cls is None else cls.attributes.get(name)
        return attribute is not None and attribute.many

    def allocate(self, entity: EntityId, alias: str) -> None:
        self.allocations[entity] = alias
        prefix, _, number = alias.rpartition("-")
        if prefix in self.alias_prefixes.values() and int(number) > self.counters.get(prefix, 0):
            self.counters[prefix] = int(number)

    # ---- ops -----------------------------------------------------------

    def create(self, index: int, op: CreateOp, entity: EntityId) -> None:
        cls = self.cls(op.type)
        attrs: dict[str, JsonValue] = dict(op.attrs)
        for name, text in op.attrs_text.items():
            attrs[name] = self.coerce(op.type, name, text)
        initial: list[tuple[str, JsonValue]] = []
        if cls is not None:
            for name, attribute in cls.attributes.items():
                if name not in attrs:
                    if attribute.default is not None:
                        attrs[name] = attribute.default
                    elif attribute.kind == "lifecycle":
                        lifecycle = self.model.lifecycles.get(attribute.type)
                        if lifecycle is not None and len(lifecycle.initial) == 1:
                            attrs[name] = lifecycle.initial[0]
                if attribute.kind == "lifecycle":
                    initial.append((name, attrs.get(name)))
        work = _Work(op.type, "active", attrs, [], None, op.valid_from, op.valid_to, None)
        self.work[entity] = work
        for spec in op.edges:
            if spec.rel in RESERVED_RELATIONS:
                self.finding(
                    "data.temporal.reserved_relation",
                    entity,
                    f"edges.{spec.rel}",
                    f"{spec.rel} edges are created only by their operation",
                )
                continue
            work.edges.append(
                self.new_edge(spec.rel, self.resolve(spec.target), spec.qualifiers, spec.target_rev)
            )
        self.check_interval(entity, op.valid_from, op.valid_to)
        prefix = self.alias_prefixes.get(op.type)
        if prefix is not None and self.allocate_aliases:
            number = self.counters.get(prefix, 0) + 1
            work.alias = format_alias(prefix, number)
            self.allocate(entity, work.alias)
        names = tuple(sorted({*op.attrs, *op.attrs_text}))
        self.effects.append(OpEffect(index, op.OP, entity, names=names, initial=tuple(initial)))

    def check_interval(self, entity: EntityId, start: str | None, end: str | None) -> None:
        if start is not None and end is not None and _instant(start) > _instant(end):
            self.finding(
                "data.temporal.invalid_interval",
                entity,
                "valid_from",
                f"valid_from {start} is after valid_to {end}",
            )

    def update(self, index: int, op: UpdateOp) -> None:
        entity = self.resolve(op.id)
        lists = op.list_names()
        names = tuple(sorted({*op.set, *op.set_text, *op.unset, *lists}))
        if not self.active_or_report(entity, op.OP):
            self.effects.append(
                OpEffect(index, op.OP, entity, applied=False, names=names, lists=lists)
            )
            return
        work = self.touch(entity)
        work.attrs.update(op.set)
        for name, text in op.set_text.items():
            work.attrs[name] = self.coerce(work.type, name, text)
        for name in op.unset:
            work.attrs.pop(name, None)
        for name in lists:
            if self.is_list(work.type, name):  # otherwise the validator reports it
                self.change_list(work.attrs, work.type, name, op)
        self.effects.append(OpEffect(index, op.OP, entity, names=names, lists=lists))

    def change_list(self, attrs: dict[str, JsonValue], type: str, name: str, op: UpdateOp) -> None:
        """Append the values not yet present, in order; drop every value removed."""
        current = attrs.get(name)
        items = list(current) if isinstance(current, list | tuple) else []
        texts = op.add_text.get(name, ()) or op.remove_text.get(name, ())
        given = [
            *op.add.get(name, ()),
            *op.remove.get(name, ()),
            *(self.coerce_item(type, name, text) for text in texts),
        ]
        if name in op.add or name in op.add_text:
            for value in given:
                if not any(strict_equal(value, item) for item in items):
                    items.append(value)
        else:
            items = [item for item in items if not any(strict_equal(item, v) for v in given)]
        if items:
            attrs[name] = items
        else:
            attrs.pop(name, None)

    def reserved(self, entity: EntityId, rel: TermRef) -> bool:
        if rel not in RESERVED_RELATIONS:
            return False
        self.finding(
            "data.temporal.reserved_relation",
            entity,
            f"edges.{rel}",
            f"{rel} edges are written only by their operation",
        )
        return True

    def link(self, index: int, op: LinkOp) -> None:
        entity = self.resolve(op.id)
        spec = op.edge
        if not self.active_or_report(entity, op.OP) or self.reserved(entity, spec.rel):
            self.effects.append(OpEffect(index, op.OP, entity, applied=False, rel=spec.rel))
            return
        work = self.touch(entity)
        edge = self.new_edge(spec.rel, self.resolve(spec.target), spec.qualifiers, spec.target_rev)
        work.edges.append(edge)
        self.effects.append(OpEffect(index, op.OP, entity, edge_id=edge.edge_id, rel=spec.rel))

    def unlink(self, index: int, op: UnlinkOp) -> None:
        entity = self.resolve(op.id)
        if not self.active_or_report(entity, op.OP):
            self.effects.append(OpEffect(index, op.OP, entity, applied=False, rel=op.rel))
            return
        current = self.peek(entity)
        assert current is not None
        if op.edge_id is not None:
            matches = [e for e in current.edges if e.edge_id == op.edge_id]
            wanted = f"edge {op.edge_id}"
        else:
            assert op.rel is not None and op.target is not None
            target = self.resolve(op.target)
            matches = [e for e in current.edges if e.rel == op.rel and e.target == target]
            wanted = f"a {op.rel} edge to {target}"
        if len(matches) != 1:
            problem = f"no {wanted}" if not matches else f"{len(matches)} edges match {wanted}"
            self.effects.append(
                OpEffect(index, op.OP, entity, applied=False, rel=op.rel, problem=problem)
            )
            return
        (edge,) = matches
        if self.reserved(entity, edge.rel):
            self.effects.append(OpEffect(index, op.OP, entity, applied=False, rel=edge.rel))
            return
        work = self.touch(entity)
        work.edges = [e for e in work.edges if e.edge_id != edge.edge_id]
        self.effects.append(
            OpEffect(index, op.OP, entity, edge_id=edge.edge_id, rel=edge.rel, edge=edge)
        )

    def transition(self, index: int, op: TransitionOp) -> None:
        entity = self.resolve(op.id)
        if not self.active_or_report(entity, op.OP):
            self.effects.append(
                OpEffect(index, op.OP, entity, applied=False, attr=op.attr, to_state=op.to)
            )
            return
        current = self.peek(entity)
        assert current is not None
        cls = self.cls(current.type)
        lifecycle_attrs = (
            [] if cls is None else [n for n, a in cls.attributes.items() if a.kind == "lifecycle"]
        )
        attr, problem = op.attr, None
        if attr is not None and attr not in lifecycle_attrs:
            problem = f"{attr!r} is not a lifecycle attribute of {current.type}"
        elif attr is None and len(lifecycle_attrs) != 1:
            problem = (
                f"{current.type} has no lifecycle attribute"
                if not lifecycle_attrs
                else f"{current.type} has several lifecycle attributes; name one"
            )
        else:
            attr = attr or lifecycle_attrs[0]
        if problem is not None:
            self.effects.append(
                OpEffect(
                    index, op.OP, entity, applied=False, attr=attr, to_state=op.to, problem=problem
                )
            )
            return
        assert attr is not None
        work = self.touch(entity)
        before = work.attrs.get(attr)
        work.attrs[attr] = op.to
        self.effects.append(
            OpEffect(index, op.OP, entity, attr=attr, from_state=before, to_state=op.to)
        )

    def supersede(self, index: int, op: SupersedeOp) -> None:
        by = self.resolve(op.by)
        targets = tuple(self.resolve(t) for t in op.targets)
        involved = [by, *(t for t in targets if t != by)]
        ok = [self.active_or_report(entity, op.OP) for entity in involved]
        if not all(ok):
            self.effects.append(OpEffect(index, op.OP, by, applied=False, targets=targets))
            return
        work = self.touch(by)
        for target in targets:
            work.edges.append(self.new_edge(SUPERSEDES, target, {}, None))
        for target in targets:
            self.touch(target).record_status = "superseded"
        self.effects.append(OpEffect(index, op.OP, by, targets=targets))

    def retract(self, index: int, op: RetractOp) -> None:
        entity = self.resolve(op.id)
        by = None if op.by is None else self.resolve(op.by)
        involved = [entity] if by is None or by == entity else [entity, by]
        ok = [self.active_or_report(e, op.OP) for e in involved]
        if not all(ok):
            self.effects.append(OpEffect(index, op.OP, entity, applied=False))
            return
        if by is not None:
            self.touch(by).edges.append(self.new_edge(RETRACTS, entity, {}, None))
        self.touch(entity).record_status = "retracted"
        self.effects.append(OpEffect(index, op.OP, entity, targets=() if by is None else (by,)))

    def set_alias(self, index: int, op: SetAliasOp) -> None:
        entity = self.resolve(op.id)
        if not self.active_or_report(entity, op.OP):
            self.effects.append(OpEffect(index, op.OP, entity, applied=False, alias=op.alias))
            return
        work = self.touch(entity)
        if work.alias != op.alias:
            work.alias = op.alias
            self.allocate(entity, op.alias)
        self.effects.append(OpEffect(index, op.OP, entity, alias=op.alias))

    # ---- result --------------------------------------------------------

    def fill_target_revs(self) -> None:
        for work in self.work.values():
            work.edges = [
                replace(edge, target_rev=self.post_rev(edge.target))
                if edge.edge_id in self.added_edges
                and edge.rel == DERIVED_FROM
                and edge.target_rev is None
                else edge
                for edge in work.edges
            ]

    def find_unchanged(self) -> None:
        """Touched entities whose content equals their current revision (C.9)."""
        for entity, work in self.work.items():
            previous = work.previous
            if previous is not None and content_hash(_projection(work)) == previous.content_hash:
                self.unchanged[entity] = previous

    def post_rev(self, entity: EntityId) -> int | None:
        if entity in self.work and entity not in self.unchanged:
            previous = self.work[entity].previous
            return 1 if previous is None else previous.rev + 1
        revision = self.base.get(entity)
        return None if revision is None else revision.rev


def _instant(value: str) -> datetime:
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    return parse_timestamp(value)


def _projection(work: _Work) -> dict[str, JsonValue]:
    return content_projection(
        type=work.type,
        record_status=work.record_status,
        alias=work.alias,
        attrs=work.attrs,
        edges=tuple(work.edges),
        valid_from=work.valid_from,
        valid_to=work.valid_to,
    )


def _revision(
    entity: EntityId,
    work: _Work,
    *,
    recorded_at: datetime,
    tx_id: TxId,
    tx_seq: int,
    pin_version: str,
) -> Revision:
    edges = tuple(work.edges)
    projection = _projection(work)
    previous = work.previous
    return Revision(
        id=entity,
        rev=1 if previous is None else previous.rev + 1,
        prev_rev=None if previous is None else previous.rev,
        type=work.type,
        record_status=work.record_status,
        attrs=dict(work.attrs),
        edges=edges,
        alias=work.alias,
        valid_from=work.valid_from,
        valid_to=work.valid_to,
        created_at=recorded_at if previous is None else previous.created_at,
        recorded_at=recorded_at,
        tx_id=tx_id,
        tx_seq=tx_seq,
        ontology_version=pin_version,
        content_hash=content_hash(projection),
    )


def normalize(
    ops: Sequence[Op],
    base: Snapshot,
    model: CompiledModel,
    *,
    refs: Mapping[str, EntityId],
    new_id: Callable[[], UUID],
    recorded_at: datetime,
    tx_id: TxId,
    tx_seq: int,
    pin_version: str,
    alias_counters: Mapping[str, int],
    alias_prefixes: Mapping[TermRef, str],
    allocate_aliases: bool = True,
) -> WorkingState:
    """Apply ``ops`` to a working copy of ``base`` (W.10).

    ``refs`` maps the text of every alias and of every local bound by an
    earlier document (``@name``) to its id (services resolve them, W.4);
    full ids are used as given and locals declared here are bound to the
    ids minted for their creates. ``allocate_aliases`` is false in dry-run.
    """
    run = _Run(
        base=base,
        model=model,
        refs=refs,
        new_id=new_id,
        alias_prefixes=alias_prefixes,
        allocate_aliases=allocate_aliases,
        counters=dict(alias_counters),
    )
    create_ids: list[EntityId] = []
    for op in ops:
        if isinstance(op, CreateOp):
            entity = EntityId(new_id())
            create_ids.append(entity)
            run.created[entity] = None
            if op.local is not None:
                run.doc_locals[op.local] = entity
    pending = iter(create_ids)
    for index, op in enumerate(ops):
        match op:
            case CreateOp():
                run.create(index, op, next(pending))
            case UpdateOp():
                run.update(index, op)
            case LinkOp():
                run.link(index, op)
            case UnlinkOp():
                run.unlink(index, op)
            case TransitionOp():
                run.transition(index, op)
            case SupersedeOp():
                run.supersede(index, op)
            case RetractOp():
                run.retract(index, op)
            case SetAliasOp():
                run.set_alias(index, op)
    run.find_unchanged()  # before target revs: an unchanged target keeps its rev
    run.fill_target_revs()
    revisions = {
        entity: _revision(
            entity,
            work,
            recorded_at=recorded_at,
            tx_id=tx_id,
            tx_seq=tx_seq,
            pin_version=pin_version,
        )
        for entity, work in run.work.items()
        if entity not in run.unchanged
    }
    bound = {key[1:]: entity for key, entity in refs.items() if key.startswith("@")}
    resolution: dict[str, JsonValue] = {
        "locals": {name: str(entity) for name, entity in run.doc_locals.items()},
        "refs": {key: str(entity) for key, entity in sorted(run.used_refs.items())},
    }
    return WorkingState(
        base=base,
        revisions=revisions,
        unchanged=run.unchanged,
        created=frozenset(run.created),
        locals={**bound, **run.doc_locals},
        alias_allocations=tuple(run.allocations.items()),
        resolution=resolution,
        findings=tuple(run.findings),
        effects=tuple(run.effects),
    )
