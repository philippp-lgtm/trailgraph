"""Test-only builder of ``AuthorizedTx`` objects: a deliberate test double of normalize.

Storage tests must not depend on services, so this builds structurally
consistent txs straight from a compiled fixture model: next rev, ``prev_rev``,
``content_hash``, edge ids, aliases from ``display_prefix`` plus the alias
counters. It picks concrete classes, fills required attributes with dummy
values per base type and links relations whose domain and range match, so
the data looks like each fixture without naming any of its terms. It does
not validate anything (contracts B4, B13.4; plan Q3).
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from graph_cli.kernel.actor import ABSENT, ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.changeset import OP_ACTIONS
from graph_cli.kernel.clock import Clock
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, GraphId, IdProvider, TermRef, TxId
from graph_cli.kernel.ports.storage import Head
from graph_cli.kernel.records import (
    EdgeRecord,
    OntologyPin,
    RecordStatus,
    RequestMeta,
    Revision,
    content_hash,
    content_projection,
)
from graph_cli.kernel.sealing import KeyRing, keyed_hash
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel

SUPERSEDES = TermRef("core:supersedes")
RETRACTS = TermRef("core:retracts")
EXTERNAL_GRAPH = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
EXTERNAL_ENTITY = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


def allow_ops(graph_id: GraphId, ops: Sequence[Mapping[str, JsonValue]]) -> tuple[Decision, ...]:
    """One allow decision per op action: what services.write attaches (B6.10)."""
    actions = sorted({OP_ACTIONS[str(op["op"])] for op in ops})
    resource = f"graph:{graph_id}"
    policy = hash_value({"policy": "test"})
    return tuple(Decision("allow", a, resource, "test.allow", policy) for a in actions)


def actor(principal: str = "test:1") -> ActorContext:
    return ActorContext(
        principal=Facet(Assurance.VERIFIED, id=principal, name="tester", source="test"),
        agent=ABSENT,
        session=ABSENT,
        device=ABSENT,
        project=ABSENT,
        client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
        pid=None,
    )


class State(Protocol):
    """What the builder reads: the current revision of an entity and the known ids."""

    def get(self, entity: EntityId) -> Revision | None: ...

    def ids(self) -> Sequence[EntityId]: ...


@dataclass(frozen=True)
class FuncState:
    getter: Callable[[EntityId], Revision | None]
    lister: Callable[[], Sequence[EntityId]]

    def get(self, entity: EntityId) -> Revision | None:
        return self.getter(entity)

    def ids(self) -> Sequence[EntityId]:
        return self.lister()


@dataclass
class _Work:
    id: EntityId
    type: TermRef
    record_status: RecordStatus
    attrs: dict[str, JsonValue]
    edges: list[EdgeRecord]
    alias: str | None
    previous: Revision | None
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass
class TxDraft:
    builder: "TxBuilder"
    state: State
    head: Head
    counters: dict[str, int]
    work: dict[EntityId, _Work] = field(default_factory=dict)
    allocations: list[tuple[EntityId, str]] = field(default_factory=list)
    ops: list[dict[str, JsonValue]] = field(default_factory=list)

    # ---- reading -------------------------------------------------------

    def current(self, entity: EntityId) -> _Work | None:
        if entity in self.work:
            return self.work[entity]
        revision = self.state.get(entity)
        if revision is None:
            return None
        return _Work(
            id=revision.id,
            type=revision.type,
            record_status=revision.record_status,
            attrs=dict(revision.attrs),
            edges=list(revision.edges),
            alias=revision.alias,
            previous=revision,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
        )

    def _touch(self, entity: EntityId) -> _Work:
        work = self.current(entity)
        assert work is not None, f"unknown entity {entity}"
        self.work[entity] = work
        return work

    def all_ids(self) -> list[EntityId]:
        return sorted({*self.state.ids(), *self.work})

    def active_ids(self) -> list[EntityId]:
        found = []
        for entity in self.all_ids():
            work = self.current(entity)
            if work is not None and work.record_status == "active":
                found.append(entity)
        return found

    # ---- operations ----------------------------------------------------

    def create(
        self,
        cls: str | None = None,
        *,
        attrs: Mapping[str, JsonValue] | None = None,
        edges: Sequence[tuple[str, EntityId]] = (),
        alias: str | Literal["auto"] | None = "auto",
    ) -> EntityId:
        model = self.builder.model
        cls = cls or self.builder.rng.choice(self.builder.concrete_classes)
        entity = EntityId(self.builder.ids.new_id())
        values = self.builder.required_attrs(cls)
        values.update(attrs or {})
        work = _Work(entity, TermRef(cls), "active", values, [], None, None)
        self.work[entity] = work
        for rel, target in edges:
            work.edges.append(self.builder.edge(rel, target))
        if alias == "auto":
            prefix = self.builder.prefix_of(cls)
            alias = self._next_alias(prefix) if prefix else None
        if alias is not None:
            self._allocate(entity, alias)
        self.ops.append({"op": "create", "type": cls, "id": str(entity)})
        assert cls in model.classes
        return entity

    def update(
        self,
        entity: EntityId,
        set: Mapping[str, JsonValue] | None = None,
        unset: Sequence[str] = (),
    ) -> None:
        work = self._touch(entity)
        if set is None:
            name = self.builder.text_attribute(work.type)
            set = {name: f"updated {self.builder.next_number()}"} if name else {}
        work.attrs.update(set)
        for name in unset:
            work.attrs.pop(name, None)
        self.ops.append({"op": "update", "id": str(entity), "set": dict(set)})

    def link(
        self,
        entity: EntityId,
        rel: str | None = None,
        target: EntityId | None = None,
        *,
        qualifiers: Mapping[str, JsonValue] | None = None,
    ) -> EdgeId | None:
        work = self._touch(entity)
        if rel is None or target is None:
            choice = self.builder.pick_link(work.type, self, exclude=entity)
            if choice is None:
                return None
            rel, target = choice
        edge = self.builder.edge(rel, target, qualifiers)
        work.edges.append(edge)
        self.ops.append({"op": "link", "id": str(entity), "rel": rel, "target": str(target)})
        return edge.edge_id

    def unlink(self, entity: EntityId, edge_id: EdgeId | None = None) -> bool:
        current = self.current(entity)
        assert current is not None, f"unknown entity {entity}"
        candidates = [e for e in current.edges if edge_id in (None, e.edge_id)]
        if not candidates:
            return False
        work = self._touch(entity)
        edge = candidates[0] if edge_id else self.builder.rng.choice(candidates)
        work.edges.remove(edge)
        self.ops.append({"op": "unlink", "id": str(entity), "edge_id": str(edge.edge_id)})
        return True

    def supersede(self, by: EntityId, targets: Sequence[EntityId]) -> None:
        replacement = self._touch(by)
        for target in targets:
            replacement.edges.append(self.builder.edge(SUPERSEDES, target))
            self._touch(target).record_status = "superseded"
        self.ops.append({"op": "supersede", "by": str(by), "targets": [str(t) for t in targets]})

    def retract(self, entity: EntityId, by: EntityId | None = None) -> None:
        self._touch(entity).record_status = "retracted"
        if by is not None:
            self._touch(by).edges.append(self.builder.edge(RETRACTS, entity))
        self.ops.append({"op": "retract", "id": str(entity)})

    def set_alias(self, entity: EntityId, alias: str | None = None) -> str:
        work = self._touch(entity)
        if alias is None:
            prefix = self.builder.prefix_of(work.type) or "X"
            alias = self._next_alias(prefix)
        self._allocate(entity, alias)
        self.ops.append({"op": "set_alias", "id": str(entity), "alias": alias})
        return alias

    def _next_alias(self, prefix: str) -> str:
        number = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{number:04d}"

    def _allocate(self, entity: EntityId, alias: str) -> None:
        prefix, _, number = alias.rpartition("-")
        self.counters[prefix] = max(self.counters.get(prefix, 0), int(number))
        self.work[entity].alias = alias
        # One allocation per entity per tx: a second one in the same tx replaces the first.
        self.allocations = [(e, a) for e, a in self.allocations if e != entity]
        self.allocations.append((entity, alias))

    # ---- result --------------------------------------------------------

    def build(
        self,
        *,
        idempotency_key: str | None = None,
        reason: str | None = None,
        principal: str = "test:1",
    ) -> AuthorizedTx:
        builder = self.builder
        recorded_at = builder.clock.now()
        if self.head.recorded_at is not None:
            recorded_at = max(recorded_at, self.head.recorded_at)
        tx_id = TxId(builder.ids.new_id())
        event_id = EventId(builder.ids.new_id())
        seq = self.head.seq + 1
        revisions = tuple(
            self._revision(work, tx_id, seq, recorded_at)
            for work in sorted(self.work.values(), key=lambda w: w.id)
        )
        changeset: dict[str, JsonValue] = {"schema": "graph_cli.changeset/1", "ops": self.ops}
        if reason is not None:
            changeset["reason"] = reason
        if idempotency_key is not None:
            changeset["idempotency_key"] = idempotency_key
        return AuthorizedTx(
            graph_id=builder.graph_id,
            tx_id=tx_id,
            event_id=event_id,
            seq=seq,
            recorded_at=recorded_at,
            actor=actor(principal),
            decisions=allow_ops(builder.graph_id, self.ops),
            changeset=changeset,
            changeset_hash=keyed_hash(changeset, builder.tx_mac(tx_id)),
            resolution={"locals": {}, "refs": {}},
            revisions=revisions,
            alias_allocations=tuple(self.allocations),
            ontology=builder.pin,
            reason=reason,
            idempotency_key=idempotency_key,
            request=RequestMeta("apply", hash_value({"ops": len(self.ops)})),
        )

    def _revision(self, work: _Work, tx_id: TxId, seq: int, recorded_at: datetime) -> Revision:
        previous = work.previous
        edges = tuple(work.edges)
        projection = content_projection(
            type=work.type,
            record_status=work.record_status,
            alias=work.alias,
            attrs=work.attrs,
            edges=edges,
            valid_from=work.valid_from,
            valid_to=work.valid_to,
        )
        return Revision(
            id=work.id,
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
            tx_seq=seq,
            ontology_version=self.builder.model.bundle_version,
            content_hash=content_hash(projection, self.builder.entity_mac(work.id)),
        )


class TxBuilder:
    def __init__(
        self,
        model: CompiledModel,
        *,
        graph_id: GraphId,
        ids: IdProvider,
        clock: Clock,
        seed: int = 0,
        ring: KeyRing | None = None,
    ) -> None:
        self.model = model
        self.ring = ring  # a sealed store's keys: the hashes are keyed under them (T16)
        self.graph_id = graph_id
        self.ids = ids
        self.clock = clock
        self.rng = random.Random(seed)
        self.pin = OntologyPin(model.bundle_id, model.bundle_version, model.artifact_hash)
        self.concrete_classes = sorted(c for c, d in model.classes.items() if not d.abstract)
        self._number = 0

    def entity_mac(self, entity: EntityId) -> bytes | None:
        return None if self.ring is None else self.ring.mac_key(self.ring.entity_key(entity))

    def tx_mac(self, tx: TxId) -> bytes | None:
        return None if self.ring is None else self.ring.mac_key(self.ring.tx_key(tx))

    def begin(self, state: State, head: Head, alias_counters: Mapping[str, int]) -> TxDraft:
        return TxDraft(self, state, head, dict(alias_counters))

    def next_number(self) -> int:
        self._number += 1
        return self._number

    # ---- model lookups -------------------------------------------------

    def prefix_of(self, cls: str) -> str | None:
        definition = self.model.classes[cls]
        for name in (cls, *definition.ancestors):
            prefix = self.model.classes[name].display_prefix
            if prefix:
                return prefix
        return None

    def _base(self, attr: AttributeDef) -> str:
        if attr.kind == "datatype":
            return self.model.datatypes[attr.type].base
        return attr.type

    def dummy(self, attr: AttributeDef) -> JsonValue:
        n = self.next_number()
        if attr.kind == "enum":
            value: JsonValue = self.model.enums[attr.type].values[0].name
        elif attr.kind == "lifecycle":
            value = self.model.lifecycles[attr.type].initial[0]
        else:
            value = _base_dummy(self._base(attr), n)
        return [value] if attr.many else value

    def required_attrs(self, cls: str) -> dict[str, JsonValue]:
        attributes = self.model.classes[cls].attributes
        return {
            name: attr.default if attr.default is not None else self.dummy(attr)
            for name, attr in sorted(attributes.items())
            if attr.required or attr.kind == "lifecycle"
        }

    def text_attribute(self, cls: str) -> str | None:
        for name, attr in sorted(self.model.classes[cls].attributes.items()):
            kind_ok = attr.kind in ("base", "datatype") and not attr.many
            if kind_ok and self._base(attr) in ("string", "text"):
                return name
        return None

    def _is_a(self, cls: str, allowed: Sequence[str]) -> bool:
        return not allowed or any(c in allowed for c in (cls, *self.model.classes[cls].ancestors))

    def pick_link(
        self, cls: str, draft: TxDraft, *, exclude: EntityId
    ) -> tuple[str, EntityId] | None:
        relations = [
            name
            for name, rel in sorted(self.model.relations.items())
            if not name.startswith("core:") and self._is_a(cls, rel.domain)
        ]
        self.rng.shuffle(relations)
        targets = draft.all_ids()
        for name in relations:
            rng_range = self.model.relations[name].range
            candidates = []
            for entity in targets:
                work = draft.current(entity)
                if entity != exclude and work is not None and self._is_a(work.type, rng_range):
                    candidates.append(entity)
            if candidates:
                return name, self.rng.choice(candidates)
        return None

    def edge(
        self, rel: str, target: EntityId, qualifiers: Mapping[str, JsonValue] | None = None
    ) -> EdgeRecord:
        return EdgeRecord(EdgeId(self.ids.new_id()), TermRef(rel), target, dict(qualifiers or {}))

    # ---- random walk ---------------------------------------------------

    def random_tx(self, draft: TxDraft, *, max_ops: int = 3) -> None:
        """Add 1..max_ops random operations; the walk never edits non-active entities."""
        for _ in range(self.rng.randint(1, max_ops)):
            active = draft.active_ids()
            roll = self.rng.random()
            if not active or roll < 0.35:
                draft.create()
            elif roll < 0.6:
                draft.update(self.rng.choice(active))
            elif roll < 0.75:
                draft.link(self.rng.choice(active))
            elif roll < 0.82:
                draft.unlink(self.rng.choice(active))
            elif roll < 0.88:
                draft.set_alias(self.rng.choice(active))
            elif roll < 0.94 and len(active) >= 2:
                by, target = self.rng.sample(active, 2)
                draft.supersede(by, [target])
            else:
                draft.retract(self.rng.choice(active))
        if not draft.work:  # every tx needs at least one revision
            draft.create()


def _base_dummy(base: str, n: int) -> JsonValue:
    if base == "integer":
        return n
    if base == "decimal":
        return f"{n}.50"
    if base == "boolean":
        return n % 2 == 0
    if base == "date":
        return f"2026-01-{n % 28 + 1:02d}"
    if base == "datetime":
        return f"2026-01-01T00:00:{n % 60:02d}.000Z"
    if base == "duration":
        return f"P{n}D"
    if base == "iri":
        return f"https://example.org/{n}"
    if base == "json":
        return {"n": n}
    if base == "external_ref":
        return {"graph": EXTERNAL_GRAPH, "entity": EXTERNAL_ENTITY}
    return f"value {n}"
