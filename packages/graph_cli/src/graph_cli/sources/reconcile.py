"""The reconcile planner: the ops that bring declared scopes to the state a source hands over
(reconcile plan R-K4 to R-K9, R-K13).

``plan_reconcile`` is pure over a ``Snapshot``, a ``CompiledModel`` and the parsed ``reconcile``
part of a document; the write service calls it under the graph lock on the snapshot the caller
sees and appends ``ReconcilePlan.ops`` to the document's own ops (``expanded``), so the tx
records ordinary ops. Nothing here reads a store, a policy or the clock.

A scope speaks for the active entities of its type (and subtypes) whose key attribute starts
with ``<source>/<under>``; an item's stored key is ``<source>/<item key>``. Per scope the planner

- reads the key namespace once (every active entity of the family that declares the key
  attribute, under the prefix), so an entity of another type holding a wanted key is found and
  refused (``reconcile.type_mismatch``) instead of failing later on the unique check;
- applies ``moves`` (a key update that keeps the identity, or an adoption of a keyless entity)
  and matches items to entities by key;
- diffs the ``fields`` of a matched entity (a field the item leaves out is unset; attributes
  outside ``fields`` are never read for change) after the coercion normalize applies, so an item
  that restates the stored value emits nothing, and diffs the ``edges`` relations by
  ``(rel, target, qualifiers)``;
- with ``complete``, retires the scope entities no item names (and ``keep`` does not hold),
  as a retract or a lifecycle transition, and with ``revive`` brings a retired entity back;
- judges the guards (``source_empty``, ``retire_limit``, ``shape``, ...) before it returns.

Ops come in phases, scope after scope within each: moves, creates (item order), updates, edge
changes (per entity, unlinks before links), revives, retires (by key). The document's own ops
run before all of them and must not touch an entity of a scope (``reconcile.scope_conflict``),
since the plan judges the state before them. Values of an item that do not fit the ontology are
not judged here: the planner emits them and the validator refuses the whole document.

Not read for change: ``valid_from`` and ``valid_to`` of an item apply when an entity is created
(an update op has no valid time), so a matched entity keeps its interval.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeGuard
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    MAX_RECONCILE_OPS,
    ChangeSet,
    CreateOp,
    EdgeSpec,
    LinkOp,
    MoveSpec,
    Op,
    ReconcileEdge,
    ReconcileItem,
    ReconcileScope,
    RetireSpec,
    RetractOp,
    SetAliasOp,
    SupersedeOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    ValueRef,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import AliasRef, EdgeId, EntityId, EntityRef, LocalRef, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.query_eval import strict_equal
from graph_cli.kernel.records import Revision, SourceRef
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.stored_values import (
    coerce_attribute,
    language_tagged,
)

MIN_RETIRE_LIMIT: Final = 5  # retires above max(5, 10% of the scope) need ``allow_retire``
RETIRE_SHARE: Final = 10  # one tenth
MAX_LISTED_KEYS: Final = 20  # retired keys a summary and a refusal list
_PAGE: Final = 1000
_TEXT_BASES: Final = ("string", "text")
_KEY_KINDS: Final = ("base", "datatype")


# ---- the result ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeSummary:
    """What the plan does in one scope. ``unchanged`` counts items that need no op at all;
    ``moved`` and ``adopted`` count key updates, ``kept`` the entities ``keep`` left alone.
    ``retired_keys`` are item keys (without the source name), sorted."""

    type: str
    under: str
    complete: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    moved: int = 0
    adopted: int = 0
    kept: int = 0
    retired: int = 0
    revived: int = 0
    retired_keys: tuple[str, ...] = ()

    def to_json(self, *, max_keys: int | None = MAX_LISTED_KEYS) -> dict[str, JsonValue]:
        """The scope entry of the answer (plan section 4); ``max_keys=None`` lists every key."""
        keys = self.retired_keys if max_keys is None else self.retired_keys[:max_keys]
        return {
            "type": self.type,
            "under": self.under,
            "complete": self.complete,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "moved": self.moved,
            "adopted": self.adopted,
            "kept": self.kept,
            "retired": self.retired,
            "revived": self.revived,
            "retired_keys": list(keys),
        }


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    """The ops of the reconcile part, in the order they run, and one summary per scope."""

    ops: tuple[Op, ...]
    scopes: tuple[ScopeSummary, ...]
    # the locals of items that matched an entity, so the answer binds them like the locals of
    # created entities: a pipeline gets the id of everything it sent
    bound: Mapping[str, EntityId] = field(default_factory=dict)

    def to_json(self, source: SourceRef, *, max_keys: int | None = MAX_LISTED_KEYS) -> JsonValue:
        """The ``reconcile`` block of a write answer."""
        return {
            "source": source.to_json(),
            "scopes": [s.to_json(max_keys=max_keys) for s in self.scopes],
        }


def default_reason(source: SourceRef) -> str:
    """The reason of a reconcile write whose document gives none (R-K8)."""
    parts = ["reconcile", source.name]
    if source.version is not None:
        parts.append(source.version)
    if source.run is not None:
        parts.extend(["run", source.run])
    return " ".join(parts)


def expanded(changeset: ChangeSet, plan: ReconcilePlan) -> ChangeSet:
    """The change set the tx records: the document's own ops, then the plan's, without the
    reconcile part. A document that gives no reason gets ``default_reason``."""
    reason = changeset.reason
    if reason is None and changeset.source is not None and plan.ops:
        reason = default_reason(changeset.source)
    return ChangeSet(
        ops=(*changeset.ops, *plan.ops),
        reason=reason,
        idempotency_key=changeset.idempotency_key,
        expect_seq=changeset.expect_seq,
        source=changeset.source,
    )


# ---- entry point ---------------------------------------------------------------------------


def plan_reconcile(
    source: SourceRef,
    scopes: Sequence[ReconcileScope],
    snapshot: Snapshot,
    model: CompiledModel,
    *,
    frame_ops: Sequence[Op] = (),
    bindings: Mapping[str, EntityId] | None = None,
    language: str | None = None,
    max_ops: int = MAX_RECONCILE_OPS,
) -> ReconcilePlan:
    """Plan ``scopes`` of ``source`` on ``snapshot``.

    ``frame_ops`` are the document's own ops (they must not touch a scope, and their locals are
    taken names), ``bindings`` the locals of earlier documents of the invocation, ``language`` the
    graph language that untagged language-tagged text takes. Raises ``GraphCliError``:
    ``reconcile.*`` for a guard (the write service audits these as ``invalid`` refusals),
    ``term.unknown`` for a type, relation or attribute the model lacks, ``changeset.malformed``
    for a retire mode the type's lifecycle cannot do, ``changeset.local_ref_duplicate`` for a
    local named twice and ``changeset.too_large`` above ``max_ops`` ops with the frame's.
    """
    return _Planner(source, snapshot, model, language, bindings or {}, frame_ops).plan(
        scopes, max_ops
    )


# ---- internals -----------------------------------------------------------------------------


def _is_id(value: object) -> TypeGuard[EntityId]:
    """``EntityId`` is a ``NewType`` of ``UUID``, which ``isinstance`` cannot name."""
    return isinstance(value, UUID)


def _refuse(code: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(code, message, details=details)


@dataclass(slots=True)
class _Item:
    scope: _Scope
    index: int
    item: ReconcileItem
    key: str  # the stored key, ``<source>/<item key>``
    type: str  # the type a create gives it
    entity: Revision | None = None
    local: str | None = None  # the local a created entity is declared under
    referenced: bool = False  # an edge target given by value points at it

    @property
    def path(self) -> str:
        return f"reconcile[{self.scope.index}].items[{self.index}]"


@dataclass(slots=True)
class _Scope:
    index: int
    scope: ReconcileScope
    source: str  # the source name
    prefix: str  # ``<source>/<under>``
    attribute: AttributeDef  # the key attribute
    family: frozenset[str]  # the scope type and its subtypes
    held: dict[str, Revision] = field(default_factory=dict)  # stored key -> entity, key family
    items: list[_Item] = field(default_factory=list)
    claimed: dict[str, Revision] = field(default_factory=dict)  # new key -> entity moved to it
    moved_ids: set[EntityId] = field(default_factory=set)
    moves: list[UpdateOp] = field(default_factory=list)
    summary: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("created", "updated", "unchanged", "moved", "adopted", "kept", "retired", "revived"),
            0,
        )
    )
    retired_keys: list[str] = field(default_factory=list)
    creates: list[CreateOp] = field(default_factory=list)
    updates: list[UpdateOp] = field(default_factory=list)
    edge_ops: list[Op] = field(default_factory=list)
    revives: list[TransitionOp] = field(default_factory=list)
    retires: list[Op] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"reconcile[{self.index}]"

    def stored(self, key: str) -> str:
        """The stored key value of an item key (or of ``under``): ``<source>/<key>``."""
        return f"{self.source}/{key}"


class _Planner:
    def __init__(
        self,
        source: SourceRef,
        snapshot: Snapshot,
        model: CompiledModel,
        language: str | None,
        bindings: Mapping[str, EntityId],
        frame_ops: Sequence[Op],
    ) -> None:
        self.source = source
        self.snapshot = snapshot
        self.model = model
        self.language = language
        self.bindings = bindings
        self.frame_ops = tuple(frame_ops)
        self.by_key: dict[tuple[str, str], list[_Item]] = {}  # (key attribute, stored key)
        self.locals: dict[str, EntityId | LocalRef] = {}
        self.taken: set[str] = set(bindings)
        self.matched: dict[str, EntityId] = {}  # local of an item -> the entity it matched
        self.lookups: dict[tuple[str, str, str | int], EntityId] = {}
        self.lifecycles: dict[tuple[str, RetireSpec], tuple[str, frozenset[str]]] = {}

    # ---- the whole plan ----

    def plan(self, scopes: Sequence[ReconcileScope], max_ops: int) -> ReconcilePlan:
        states = [self.prepare(index, scope) for index, scope in enumerate(scopes)]
        self.check_overlap(states)
        for state in states:
            self.check_shape(state)
            self.scan(state)
        self.check_conflicts(states)
        for state in states:
            self.apply_moves(state)
            self.match(state)
        self.check_locals(states)
        for state in states:
            for found in state.items:
                self.mark_references(found)
        for state in states:
            self.mint_locals(state)
        for state in states:
            self.build(state)
        ops: list[Op] = []
        for phase in ("moves", "creates", "updates", "edge_ops", "revives", "retires"):
            for state in states:
                ops.extend(getattr(state, phase))
        total = len(self.frame_ops) + len(ops)
        if total > max_ops:
            raise _refuse(
                "changeset.too_large",
                f"the document expands to {total} ops, the limit is {max_ops}; hand the source"
                " over as partial scopes in several documents, then one complete run",
                limit_ops=max_ops,
                actual_ops=total,
            )
        return ReconcilePlan(
            tuple(ops), tuple(self.summary(state) for state in states), dict(self.matched)
        )

    @staticmethod
    def summary(state: _Scope) -> ScopeSummary:
        scope = state.scope
        return ScopeSummary(
            type=scope.type,
            under=scope.under,
            complete=scope.complete,
            retired_keys=tuple(state.retired_keys),
            **state.summary,
        )

    # ---- scopes ----

    def prepare(self, index: int, scope: ReconcileScope) -> _Scope:
        path = f"reconcile[{index}]"
        cls = self.model.classes.get(scope.type)
        if cls is None:
            raise self.unknown("type", scope.type, f"{path}.type")
        for rel in scope.edges:
            if rel not in self.model.relations:
                raise self.unknown("relation", rel, f"{path}.edges")
        attribute = cls.attributes.get(scope.key)
        problem = None
        if attribute is None:
            problem = f"{scope.type} has no attribute {scope.key!r}"
        elif not attribute.unique:
            problem = f"{scope.key!r} is not unique"
        elif attribute.many:
            problem = f"{scope.key!r} holds several values"
        elif (
            attribute.kind not in _KEY_KINDS
            or self.model.value_base(attribute.kind, attribute.type) not in _TEXT_BASES
        ):
            problem = f"{scope.key!r} is not plain text"
        elif attribute.mutability == "immutable" and any(m.old_key for m in scope.moves):
            problem = f"{scope.key!r} is immutable, so a key cannot move"
        if problem is not None or attribute is None:
            raise _refuse(
                "reconcile.key_attribute",
                f"{path}.key: {problem}; the key attribute is a unique, single-valued text"
                " attribute of the scope type",
                path=f"{path}.key",
                type=scope.type,
                attribute=scope.key,
            )
        if scope.retire.transition is not None:
            self.lifecycle(scope.type, scope.retire, path)
        return _Scope(
            index=index,
            scope=scope,
            source=self.source.name,
            prefix=f"{self.source.name}/{scope.under}",
            attribute=attribute,
            family=frozenset(self.model.subtypes(scope.type)),
        )

    @staticmethod
    def unknown(kind: str, term: str, path: str) -> GraphCliError:
        return _refuse(
            "term.unknown", f"{path}: unknown {kind} {term!r}", kind=kind, term=term, path=path
        )

    def check_overlap(self, states: Sequence[_Scope]) -> None:
        for later, state in enumerate(states):
            for other in states[:later]:
                if self.overlaps(other, state):
                    raise _refuse(
                        "reconcile.scope_overlap",
                        f"{state.path} covers entities that {other.path} covers: the scopes name"
                        " related types under overlapping prefixes",
                        path=state.path,
                        other=other.path,
                    )

    @staticmethod
    def overlaps(a: _Scope, b: _Scope) -> bool:
        if a.scope.key == b.scope.key and a.attribute.declared_in == b.attribute.declared_in:
            # one key namespace: the prefixes decide
            return a.prefix.startswith(b.prefix) or b.prefix.startswith(a.prefix)
        return bool(a.family & b.family)

    def check_shape(self, state: _Scope) -> None:
        expect = state.scope.expect
        count = len(state.scope.items)
        if expect is None:
            return
        low, high = expect.min_items, expect.max_items
        if (low is not None and count < low) or (high is not None and count > high):
            raise _refuse(
                "reconcile.shape",
                f"{state.path} has {count} items, expected {_range(low, high)}",
                path=state.path,
                items=count,
                min_items=low,
                max_items=high,
            )

    def scan(self, state: _Scope) -> None:
        """Every active entity of the key family whose key starts with the scope's prefix."""
        types = tuple(TermRef(t) for t in self.model.subtypes(state.attribute.declared_in))
        where = Cmp(state.scope.key, "prefix", state.prefix)
        offset = 0
        while True:
            page = self.snapshot.query(Query(types=types, where=where, limit=_PAGE, offset=offset))
            for revision in page.items:
                value = revision.attrs.get(state.scope.key)
                if isinstance(value, str):
                    state.held[value] = revision
            offset += len(page.items)
            if not page.items or offset >= page.total:
                return

    def members(self, state: _Scope) -> list[Revision]:
        """The entities of the scope: the held ones of its type family."""
        return [r for r in state.held.values() if r.type in state.family]

    # ---- the document's own ops ----

    def check_conflicts(self, states: Sequence[_Scope]) -> None:
        scope_of: dict[EntityId, _Scope] = {}
        for state in states:
            for revision in self.members(state):
                scope_of[revision.id] = state
        for index, op in enumerate(self.frame_ops):
            for entity in self.touched(op):
                owner = scope_of.get(entity)
                if owner is not None:
                    raise _refuse(
                        "reconcile.scope_conflict",
                        f"ops[{index}] touches {entity}, an entity of {owner.path}: a reconcile"
                        " scope is written by its items only",
                        path=f"ops[{index}]",
                        scope=owner.path,
                        entity=str(entity),
                    )
            if isinstance(op, CreateOp):
                for state in states:
                    if self.creates_in(op, state):
                        raise _refuse(
                            "reconcile.scope_conflict",
                            f"ops[{index}] creates an entity under the keys of {state.path}: a"
                            " reconcile scope is written by its items only",
                            path=f"ops[{index}]",
                            scope=state.path,
                        )

    def creates_in(self, op: CreateOp, state: _Scope) -> bool:
        if op.type not in self.model.subtypes(state.attribute.declared_in):
            return False
        key = state.scope.key
        value = op.attrs.get(key, op.attrs_text.get(key))
        return isinstance(value, str) and value.startswith(state.prefix)

    def touched(self, op: Op) -> list[EntityId]:
        """The entities an op changes (an op subject the planner cannot resolve is none)."""
        match op:
            case UpdateOp() | LinkOp() | UnlinkOp() | TransitionOp() | RetractOp() | SetAliasOp():
                refs: list[EntityRef] = [op.id]
            case SupersedeOp():
                refs = [op.by, *op.targets]
            case _:
                refs = []
        found = []
        for ref in refs:
            entity = self.resolve_ref(ref)
            if _is_id(entity):
                found.append(entity)
        return found

    def resolve_ref(self, ref: EntityRef) -> EntityId | LocalRef | None:
        if _is_id(ref):
            return ref
        if isinstance(ref, AliasRef):
            found = self.snapshot.resolve_alias(ref.alias)
            return None if found is None else found[0]
        if isinstance(ref, LocalRef):
            return self.bindings.get(ref.name, ref)
        return None

    # ---- moves, adoptions and item matching ----

    @staticmethod
    def move_error(path: str, message: str) -> GraphCliError:
        return _refuse("reconcile.move_invalid", f"{path}: {message}", path=path)

    def apply_moves(self, state: _Scope) -> None:
        """Validate ``moves`` and turn each into a key update that keeps the entity's identity.

        A re-run is a no-op: a move whose old key is gone and whose new key is held, an adoption
        of an entity that already holds the key."""
        scope = state.scope
        item_keys = {state.stored(i.key) for i in scope.items}
        named: set[str] = set()
        entities: set[EntityId] = set()
        for number, move in enumerate(scope.moves):
            path = f"{state.path}.moves[{number}]"
            target = state.stored(move.to)
            old_key = None if move.old_key is None else state.stored(move.old_key)
            if target not in item_keys:
                raise self.move_error(path, f"{move.to!r} is not the key of an item")
            for key in (old_key, target):
                if key is None:
                    continue
                if key in named:
                    raise self.move_error(path, f"the key {key!r} is named by two moves")
                named.add(key)
            if old_key in item_keys:
                raise self.move_error(path, f"{move.old_key!r} is still the key of an item")
            holder = state.held.get(target)
            if old_key is not None:
                entity = state.held.get(old_key)
                if entity is None:
                    if holder is None:
                        raise self.move_error(path, "neither key is held by an entity")
                    continue  # moved by an earlier run
                if holder is not None:
                    raise self.move_error(path, f"{move.to!r} is held by another entity")
            else:
                entity = self.adoptee(state, move, target, holder, path)
                if entity is None:
                    continue  # adopted by an earlier run
            if entity.type not in state.family:
                raise self.mismatch(path, f"{entity.id} is a {entity.type}, outside {state.path}")
            if entity.id in entities:
                raise self.move_error(path, f"{entity.id} is named by two moves")
            entities.add(entity.id)
            state.claimed[target] = entity
            state.moved_ids.add(entity.id)
            state.moves.append(UpdateOp(id=entity.id, set={scope.key: target}))
            state.summary["moved" if old_key is not None else "adopted"] += 1

    def adoptee(
        self, state: _Scope, move: MoveSpec, target: str, holder: Revision | None, path: str
    ) -> Revision | None:
        """The entity an adoption gives the key to; ``None`` when it holds the key already."""
        assert move.id is not None
        ref = self.resolve_ref(move.id)
        entity = self.snapshot.get(ref) if _is_id(ref) else None
        if entity is None or entity.record_status != "active":
            raise self.move_error(path, "the id names no active entity")
        current = entity.attrs.get(state.scope.key)
        if current == target:
            return None
        if current is not None:
            raise self.move_error(path, f"{entity.id} already holds the key {current!r}")
        if holder is not None:
            raise self.move_error(path, f"{move.to!r} is held by another entity")
        return entity

    @staticmethod
    def mismatch(path: str, message: str) -> GraphCliError:
        return _refuse("reconcile.type_mismatch", f"{path}: {message}", path=path)

    def match(self, state: _Scope) -> None:
        """Pair every item with the entity that holds its key (or the one moved to it)."""
        scope = state.scope
        for index, item in enumerate(scope.items):
            key = state.stored(item.key)
            entity = state.claimed.get(key) or state.held.get(key)
            path = f"{state.path}.items[{index}]"
            if entity is not None:
                if entity.type not in state.family:
                    raise self.mismatch(
                        path,
                        f"the key is held by {entity.id}, a {entity.type} outside {state.path}",
                    )
                if item.type is not None and entity.type != item.type:
                    raise self.mismatch(
                        path,
                        f"the key is held by {entity.id}, a {entity.type}, and the item says"
                        f" {item.type}: a type does not change",
                    )
                type_curie: str = entity.type
            else:
                type_curie = item.type or scope.type
                cls = self.model.classes.get(type_curie)
                if cls is None:
                    raise self.unknown("type", type_curie, f"{path}.type")
                if type_curie not in state.family or cls.abstract:
                    raise self.mismatch(
                        path,
                        f"{type_curie} is not a concrete type of {scope.type}; an item of an"
                        " abstract scope names its type",
                    )
            found = _Item(state, index, item, key, type_curie, entity)
            state.items.append(found)
            self.by_key.setdefault((scope.key, key), []).append(found)

    def check_locals(self, states: Sequence[_Scope]) -> None:
        """Locals of items are names like those of ``ops``: one owner each."""
        declared: dict[str, str] = {name: "an earlier document" for name in self.bindings}
        for number, op in enumerate(self.frame_ops):
            if isinstance(op, CreateOp) and op.local is not None:
                declared.setdefault(op.local, f"ops[{number}].local")
        self.locals = dict(self.bindings)
        for state in states:
            for found in state.items:
                name = found.item.local
                if name is None:
                    continue
                if name in declared:
                    raise _refuse(
                        "changeset.local_ref_duplicate",
                        f"{found.path}.local: @{name} is bound twice (also {declared[name]})",
                        name=name,
                        path=f"{found.path}.local",
                    )
                declared[name] = f"{found.path}.local"
                found.local = name
                if found.entity is None:
                    self.locals[name] = LocalRef(name)
                else:
                    self.locals[name] = self.matched[name] = found.entity.id
        self.taken = set(declared)

    # ---- edge targets ----

    def value_target(self, ref: ValueRef, path: str) -> _Item | EntityId:
        """The item of this document that will hold ``ref.value`` in ``ref.attr``, else the
        active entity of the type family that holds it."""
        cls = self.model.classes.get(ref.type)
        if cls is None:
            raise self.unknown("type", ref.type, f"{path}.type")
        attribute = cls.attributes.get(ref.attr)
        if attribute is None:
            raise self.unknown("attribute", ref.attr, f"{path}.attr")
        if not attribute.unique:
            raise self.no_target(path, f"{ref.attr!r} is not unique, so it names no single entity")
        family = frozenset(self.model.subtypes(ref.type))
        for found in self.by_key.get((ref.attr, str(ref.value)), ()):
            if found.type in family:
                return found
        cached = self.lookups.get((ref.type, ref.attr, ref.value))
        if cached is not None:
            return cached
        numeric = self.model.value_base(attribute.kind, attribute.type) == "decimal"
        page = self.snapshot.query(
            Query(
                types=tuple(TermRef(t) for t in sorted(family)),
                where=Cmp(ref.attr, "eq", ref.value, numeric=numeric),
                limit=2,
            )
        )
        if len(page.items) != 1:
            what = "no active entity" if not page.items else "several active entities"
            raise self.no_target(path, f"{what} of {ref.type} hold {ref.attr} = {ref.value!r}")
        self.lookups[(ref.type, ref.attr, ref.value)] = page.items[0].id
        return page.items[0].id

    @staticmethod
    def no_target(path: str, message: str) -> GraphCliError:
        return _refuse("reconcile.target_not_found", f"{path}: {message}", path=path)

    def mark_references(self, found: _Item) -> None:
        for number, edge in enumerate(found.item.edges):
            if isinstance(edge.target, ValueRef):
                hit = self.value_target(edge.target, f"{found.path}.edges[{number}].target")
                if isinstance(hit, _Item):
                    hit.referenced = True

    def mint_locals(self, state: _Scope) -> None:
        """A created item that another item points at by value is declared under a local."""
        for found in state.items:
            if found.entity is None and found.local is None and found.referenced:
                name = f"reconcile_{state.index}_{found.index}"
                while name in self.taken:
                    name += "_"
                self.taken.add(name)
                found.local = name
                self.locals[name] = LocalRef(name)

    def edge_target(self, found: _Item, number: int, edge: ReconcileEdge) -> EntityId | LocalRef:
        """The target as an op names it: an id where the planner knows one, else a local."""
        path = f"{found.path}.edges[{number}].target"
        target = edge.target
        if isinstance(target, ValueRef):
            hit = self.value_target(target, path)
            if not isinstance(hit, _Item):
                return hit
            if hit.entity is not None:
                return hit.entity.id
            assert hit.local is not None
            return LocalRef(hit.local)
        if isinstance(target, AliasRef):
            alias = self.snapshot.resolve_alias(target.alias)
            if alias is None:
                raise self.no_target(path, f"no entity has the alias {target.alias}")
            return alias[0]
        if isinstance(target, LocalRef):
            return self.locals.get(target.name, target)
        if _is_id(target):
            return target
        raise _refuse("internal.error", f"{path}: an abbreviation cannot be a target")

    def desired_edges(
        self, found: _Item
    ) -> list[tuple[TermRef, EntityId | LocalRef, dict[str, JsonValue]]]:
        """The item's edges as ``(rel, target, qualifiers)``, qualifiers as stored, each once."""
        wanted: list[tuple[TermRef, EntityId | LocalRef, dict[str, JsonValue]]] = []
        for number, edge in enumerate(found.item.edges):
            relation = self.model.relations.get(edge.rel)
            qualifiers = {
                name: language_tagged(
                    self.model,
                    None if relation is None else relation.qualifiers.get(name),
                    value,
                    self.language,
                )
                for name, value in edge.qualifiers.items()
            }
            target = self.edge_target(found, number, edge)
            if not any(
                rel == edge.rel and t == target and strict_equal(q, qualifiers)
                for rel, t, q in wanted
            ):
                wanted.append((edge.rel, target, qualifiers))
        return wanted

    # ---- the ops of a scope ----

    def build(self, state: _Scope) -> None:
        scope = state.scope
        for found in state.items:
            if found.entity is None:
                self.create(state, found)
            else:
                self.diff(state, found, found.entity)
        members = self.members(state)
        keep = {state.stored(k) for k in scope.keep}
        state.summary["kept"] = sum(1 for r in members if r.attrs.get(scope.key) in keep)
        if scope.complete:
            self.retire(state, keep)

    def create(self, state: _Scope, found: _Item) -> None:
        item = found.item
        edges = tuple(
            EdgeSpec(rel, target, qualifiers)
            for rel, target, qualifiers in self.desired_edges(found)
        )
        state.creates.append(
            CreateOp(
                type=TermRef(found.type),
                attrs={state.scope.key: found.key, **item.attrs},
                attrs_text=dict(item.attrs_text),
                local=found.local,
                edges=edges,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
        )
        state.summary["created"] += 1

    def diff(self, state: _Scope, found: _Item, entity: Revision) -> None:
        scope = state.scope
        changed = self.diff_fields(state, found, entity) | self.diff_edges(state, found, entity)
        revive = scope.retire.revive is not None and self.is_retired(entity, scope, state.path)
        if revive:
            state.revives.append(
                TransitionOp(id=entity.id, to=str(scope.retire.revive), attr=scope.retire.attr)
            )
            state.summary["revived"] += 1
        if changed:
            state.summary["updated"] += 1
        elif not revive and entity.id not in state.moved_ids:
            state.summary["unchanged"] += 1

    def diff_fields(self, state: _Scope, found: _Item, entity: Revision) -> bool:
        """An update of the ``fields`` that differ from the stored values: after the coercion a
        write applies, so a restated value is no change; a field the item omits is unset."""
        item = found.item
        cls = self.model.classes.get(entity.type)
        values: dict[str, JsonValue] = {}
        texts: dict[str, str] = {}
        unset: list[str] = []
        for name in state.scope.fields:
            attribute = None if cls is None else cls.attributes.get(name)
            if name in item.attrs:
                wanted = language_tagged(self.model, attribute, item.attrs[name], self.language)
            elif name in item.attrs_text:
                wanted = language_tagged(
                    self.model,
                    attribute,
                    coerce_attribute(self.model, attribute, item.attrs_text[name]),
                    self.language,
                )
            else:
                if name in entity.attrs:
                    unset.append(name)
                continue
            if name in entity.attrs and strict_equal(wanted, entity.attrs[name]):
                continue
            if name in item.attrs:
                values[name] = item.attrs[name]
            else:
                texts[name] = item.attrs_text[name]
        if not (values or texts or unset):
            return False
        state.updates.append(
            UpdateOp(id=entity.id, set=values, set_text=texts, unset=tuple(sorted(unset)))
        )
        return True

    def diff_edges(self, state: _Scope, found: _Item, entity: Revision) -> bool:
        """The current edges of the scope's relations against the item's, by target and
        qualifiers: extra ones are unlinked, missing ones linked."""
        wanted = self.desired_edges(found)
        unlinks: list[Op] = []
        links: list[Op] = []
        for rel in state.scope.edges:
            current = [e for e in entity.edges if e.rel == rel]
            used: set[EdgeId] = set()
            for _, target, qualifiers in (w for w in wanted if w[0] == rel):
                match = next(
                    (
                        e
                        for e in current
                        if e.edge_id not in used
                        and e.target == target
                        and strict_equal(dict(e.qualifiers), qualifiers)
                    ),
                    None,
                )
                if match is None:
                    links.append(LinkOp(id=entity.id, edge=EdgeSpec(rel, target, qualifiers)))
                else:
                    used.add(match.edge_id)
            unlinks.extend(
                UnlinkOp(id=entity.id, edge_id=e.edge_id) for e in current if e.edge_id not in used
            )
        state.edge_ops.extend([*unlinks, *links])
        return bool(unlinks or links)

    def retire(self, state: _Scope, keep: Collection[str]) -> None:
        """Retire the scope entities no item names, under the guards."""
        scope = state.scope
        names = {f.key for f in state.items}
        live = [r for r in self.members(state) if not self.is_retired(r, scope, state.path)]
        doomed = sorted(
            (str(r.attrs[scope.key]), r)
            for r in live
            if r.attrs[scope.key] not in names
            and r.attrs[scope.key] not in keep
            and r.id not in state.moved_ids
        )
        skip = len(self.source.name) + 1
        keys = [key[skip:] for key, _ in doomed]
        if not scope.items and doomed and not scope.allow_empty:
            raise _refuse(
                "reconcile.source_empty",
                f"{state.path} is complete and has no items, but {len(live)} {scope.type} entities"
                f" are under {state.prefix!r}: an empty source is not 'everything is gone'; send"
                " no entry for a source that failed, or set allow_empty to accept it",
                path=state.path,
                type=scope.type,
                under=scope.under,
                attribute=scope.key,
                prefix=state.prefix,
                current=len(live),
                field="allow_empty",
                value=True,
            )
        limit = max(MIN_RETIRE_LIMIT, len(live) // RETIRE_SHARE)
        if len(doomed) > limit and (scope.allow_retire is None or scope.allow_retire < len(doomed)):
            raise _refuse(
                "reconcile.retire_limit",
                f"{state.path} would retire {len(doomed)} of {len(live)} {scope.type} entities"
                f" under {state.prefix!r}, above the limit of {limit}; set allow_retire to"
                f" {len(doomed)} to accept it",
                path=state.path,
                type=scope.type,
                under=scope.under,
                attribute=scope.key,
                prefix=state.prefix,
                count=len(doomed),
                limit=limit,
                current=len(live),
                keys=keys[:MAX_LISTED_KEYS],
                field="allow_retire",
                value=len(doomed),
            )
        retire = scope.retire
        for _, revision in doomed:
            if retire.transition is None:
                state.retires.append(RetractOp(id=revision.id))
            else:
                state.retires.append(
                    TransitionOp(id=revision.id, to=retire.transition, attr=retire.attr)
                )
        state.summary["retired"] = len(doomed)
        state.retired_keys.extend(keys)

    # ---- lifecycles ----

    def is_retired(self, entity: Revision, scope: _Scope | ReconcileScope, path: str) -> bool:
        """Whether the entity is in the state a transition retire puts entities in."""
        retire = scope.scope.retire if isinstance(scope, _Scope) else scope.retire
        if retire.transition is None:
            return False
        attr, _ = self.lifecycle(entity.type, retire, path)
        return entity.attrs.get(attr) == retire.transition

    def lifecycle(
        self, type_curie: str, retire: RetireSpec, path: str
    ) -> tuple[str, frozenset[str]]:
        """The lifecycle attribute a transition retire (or revive) moves on ``type_curie`` and
        its states; ``changeset.malformed`` when the type has none or lacks a named state."""
        found = self.lifecycles.get((type_curie, retire))
        if found is not None:
            return found
        where = f"{path}.retire"

        def malformed(message: str) -> GraphCliError:
            return _refuse("changeset.malformed", f"{where}: {message}", path=where)

        cls = self.model.classes.get(type_curie)
        if cls is None:
            raise self.unknown("type", type_curie, where)
        attr = retire.attr or self.model.lifecycle_attribute(type_curie)
        attribute = None if attr is None else cls.attributes.get(attr)
        if attr is None or attribute is None or attribute.kind != "lifecycle":
            raise malformed(
                f"{type_curie} has no lifecycle attribute to transition"
                + ("" if retire.attr is None else f" named {retire.attr!r}")
                + "; name the attribute in retire.attr"
            )
        states = frozenset(self.model.lifecycles[attribute.type].state_names())
        for state in (retire.transition, retire.revive):
            if state is not None and state not in states:
                raise malformed(f"{state!r} is not a state of the lifecycle of {attr!r}")
        self.lifecycles[(type_curie, retire)] = (attr, states)
        return attr, states


def _range(low: int | None, high: int | None) -> str:
    if low is not None and high is not None:
        return f"{low} to {high}"
    return f"at least {low}" if low is not None else f"at most {high}"
