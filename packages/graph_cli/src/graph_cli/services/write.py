"""The single write pipeline (contracts B3, section 4 W.1-W.16; plan W7).

``WriteService.apply`` is the only caller of ``WriteSession.commit`` in
graph_cli (B3.1). Steps:

- W.1 limits (the document itself is parsed by the caller with
  ``kernel.changeset``); W.2 graph state ``active``; W.3 the pinned model;
  W.4 references on a lock-free snapshot; W.5 one decision per
  (action, ``graph:<id>/type:<curie>``) pair; a denial is appended to the
  installation stream (not in dry-run) and raised, as ``graph.not_found``
  when the actor cannot see the graph (B6.15).
- W.6 the graph lock; the manifest is re-read (state and pin re-checked, a
  changed pin reloads the model) and references are resolved again on the
  locked snapshot. W.7 idempotency (same key and hash replays the original
  result from the tx record; another hash is
  ``conflict.idempotency_key_reuse``). W.8 ``expect_seq``/``expect_rev``.
- W.9 ``recorded_at = max(now, head.recorded_at)`` (B4.3), tx and event ids;
  W.10 normalize; W.11 validate: error findings are ``write.invalid`` with
  ``details.findings``, warning findings ride along in the result.
- W.11b the blobs the request staged (``WriteRequest.blobs``) and the tx
  references are published under the lock; the tx lists every ``blob``
  value of its revisions (``tx.blobs``). A ``blob`` value must name a stored
  or staged blob (``data.referential.blob_missing``).
- W.12 the intent (with ``before`` hashes and the published blobs' hash and
  size), W.13 the commit; storage warnings are passed on.

Refusals (B3.14): ``invalid`` and ``conflict`` go to the graph stream under
the lock; if that append fails the original error is raised with warning
``audit.refusal_not_recorded``. Usage errors and dry-runs are not audited.

Dry-run (B3.10, W.d) runs W.1-W.4 and W.8-W.11 on a lock-free snapshot with
placeholder ids that never leave the service: created entities report no
id and no alias, finding targets name them by ``local``. Its blobs are
hashed only, never staged on disk; they count as present. A refused real
write names its creates the same way: their minted ids are never committed.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final
from uuid import UUID

from graph_cli.access_control.authorize import authorize
from graph_cli.audit import events
from graph_cli.audit.events import RefusalStatus
from graph_cli.kernel.actions import Action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthorizedTx, AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    MAX_BYTES,
    MAX_OPS,
    ChangeSet,
    CreateOp,
    Op,
    RetractOp,
    SupersedeOp,
    changeset_hash,
    changeset_to_json,
    parse_changeset,
)
from graph_cli.kernel.clock import Clock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import (
    AliasRef,
    EntityId,
    EntityRef,
    EventId,
    GraphId,
    IdProvider,
    LocalRef,
    TermRef,
    TxId,
    parse_id,
)
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.audit import AuditSink
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry, graph_not_found
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import (
    GraphStoreRead,
    GraphStoreWrite,
    Head,
    Snapshot,
    StagedBlob,
    WriteSession,
)
from graph_cli.kernel.records import EnvInfo, RequestMeta, TxRecord
from graph_cli.ontology_layer.data_validator import blob_values, validate_write
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.models import ModelCache
from graph_cli.services.read import denial_error
from graph_cli.services.refs import NO_BINDINGS, Resolution, resolve_refs
from graph_cli.temporal_knowledge.normalize import normalize
from graph_cli.temporal_knowledge.working import WorkingState

DEFAULT_LOCK_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class WriteConfig:
    max_ops: int = MAX_OPS
    max_bytes: int = MAX_BYTES
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class WriteRequest:
    actor: ActorContext
    graph: str  # name or id
    changeset: ChangeSet
    dry_run: bool
    request: RequestMeta
    bindings: Mapping[str, EntityId] = field(default=NO_BINDINGS)  # earlier documents (B3.9)
    allow_abbrev: bool = False  # references from CLI arguments (B1.9)
    blobs: tuple[StagedBlob, ...] = ()  # staged by the caller, who discards them afterwards


@dataclass(frozen=True, slots=True)
class EntityResult:
    id: EntityId | None  # None for a dry-run create
    alias: str | None
    rev: int
    type: TermRef
    ops: tuple[str, ...]
    local: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": None if self.id is None else str(self.id),
            "alias": self.alias,
            "rev": self.rev,
            "type": self.type,
            "ops": list(self.ops),
            "local": self.local,
        }


@dataclass(frozen=True, slots=True)
class WriteResult:
    graph_id: GraphId
    graph_name: str
    tx_id: TxId | None
    seq: int | None
    event_id: EventId | None
    dry_run: bool
    idempotent_replay: bool
    entities: tuple[EntityResult, ...]
    warnings: tuple[WarningRecord, ...] = ()
    findings: tuple[Finding, ...] = ()  # warning findings (additive to B3)
    bindings: Mapping[str, EntityId] = field(default=NO_BINDINGS)  # locals bound by this tx

    def to_json(self) -> dict[str, JsonValue]:
        single = self.entities[0] if len(self.entities) == 1 else None
        return {
            "tx_id": None if self.tx_id is None else str(self.tx_id),
            "seq": self.seq,
            "event_id": None if self.event_id is None else str(self.event_id),
            "dry_run": self.dry_run,
            "idempotent_replay": self.idempotent_replay,
            "id": None if single is None or single.id is None else str(single.id),
            "version": None if single is None else single.rev,
            "entities": [e.to_json() for e in self.entities],
            "findings": [f.to_json() for f in self.findings],
        }


@dataclass(frozen=True, slots=True)
class BatchResult:
    """B3.9: one result per committed document; processing stopped at ``error``."""

    results: tuple[WriteResult, ...]
    error: GraphCliError | None = None
    failed_index: int | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    entry: GraphEntry
    model: CompiledModel
    read: GraphStoreRead
    write: GraphStoreWrite
    snapshot: Snapshot
    resolution: Resolution
    decisions: tuple[Decision, ...]


class _Placeholders:
    """Dry-run ids: unique, never minted, never output (W.d)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.issued: set[UUID] = set()

    def __call__(self) -> UUID:
        value = UUID(int=next(self._counter))
        self.issued.add(value)
        return value


def _check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
    if entry.state == "readonly":
        raise GraphCliError(
            "graph.readonly",
            f"graph {entry.name} is read-only",
            details={"graph_id": str(entry.id)},
        )


def _subject_refs(op: Op) -> list[EntityRef]:
    match op:
        case CreateOp():
            return []
        case SupersedeOp():
            return [op.by, *op.targets]
        case RetractOp():
            return [op.id] if op.by is None else [op.id, op.by]
    return [op.id]


def _entity_of(ref: EntityRef, refs: Mapping[str, EntityId]) -> EntityId | None:
    """The existing entity a reference names; ``None`` for a create of this document."""
    match ref:
        case LocalRef(name):
            return refs.get(f"@{name}")
        case AliasRef(alias):
            return refs[alias]
    assert isinstance(ref, UUID)
    return ref


class WriteService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        audit: AuditSink,
        policy: PolicySource,
        clock: Clock,
        ids: IdProvider,
        env: EnvInfo,
        config: WriteConfig | None = None,
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._env = env
        self.config = config or WriteConfig()
        self._models = ModelCache(catalog)

    # ---- public --------------------------------------------------------

    def parse(self, document: JsonValue) -> ChangeSet:
        """W.1 for one parsed document, with this service's op limit."""
        return parse_changeset(document, max_ops=self.config.max_ops)

    def apply(self, request: WriteRequest) -> WriteResult:
        if request.dry_run:
            return self._dry_run(request, None, _Placeholders(), 0)[0]
        return self._apply(request)

    def apply_batch(self, requests: Sequence[WriteRequest]) -> BatchResult:
        """B3.9: one tx per document in order, locals carried, stop at the first failure.

        A dry-run batch evaluates each document on the working state of the
        documents before it, so later documents see earlier creates.
        """
        bindings: dict[str, EntityId] = {}
        results: list[WriteResult] = []
        base: Snapshot | None = None
        placeholders = _Placeholders()
        for index, request in enumerate(requests):
            current = replace(request, bindings={**request.bindings, **bindings})
            try:
                if current.dry_run:
                    result, working = self._dry_run(current, base, placeholders, index)
                    base = working
                    bindings.update(
                        {n: e for n, e in working.locals.items() if e in working.created}
                    )
                else:
                    result = self._apply(current)
                    bindings.update(result.bindings)
            except GraphCliError as exc:
                return BatchResult(tuple(results), exc, index)
            results.append(result)
        return BatchResult(tuple(results))

    # ---- W.1-W.5 -------------------------------------------------------

    def _prepare(self, request: WriteRequest, snapshot: Snapshot | None) -> _Prepared:
        if len(request.changeset.ops) > self.config.max_ops:
            raise GraphCliError(
                "changeset.too_large",
                f"the change set has {len(request.changeset.ops)} ops",
                details={
                    "limit_ops": self.config.max_ops,
                    "actual_ops": len(request.changeset.ops),
                },
            )
        entry = self._catalog.resolve(request.graph)
        _check_state(entry)
        model = self._models.load(entry)
        read, write = self._catalog.store_for(entry.id)
        snapshot = snapshot if snapshot is not None else read.snapshot()
        resolution = self._resolve(request, snapshot)
        decisions = self._authorize(request, entry, resolution)
        return _Prepared(entry, model, read, write, snapshot, resolution, decisions)

    def _resolve(self, request: WriteRequest, snapshot: Snapshot) -> Resolution:
        return resolve_refs(
            request.changeset,
            snapshot,
            bindings=request.bindings,
            allow_abbrev=request.allow_abbrev,
        )

    def _authorize(
        self, request: WriteRequest, entry: GraphEntry, resolution: Resolution
    ) -> tuple[Decision, ...]:
        cs = request.changeset
        policy = self._policy.current(entry.id)
        ctx = AuthzContext(
            op_count=len(cs.ops), has_reason=cs.reason is not None, dry_run=request.dry_run
        )
        pairs = sorted({(a, f"graph:{entry.id}/type:{t}") for a, t in resolution.pairs})
        decisions = tuple(authorize(request.actor, a, r, ctx, policy) for a, r in pairs)
        denied = [d for d in decisions if d.effect == "deny"]
        if not denied:
            return decisions
        first = denied[0]
        assert first.code is not None
        visible = authorize(request.actor, "read.list", f"graph:{entry.id}", ctx, policy)
        error = (
            denial_error(first) if visible.effect == "allow" else graph_not_found(request.graph)
        )  # B6.15: a graph the actor cannot see does not exist for it
        if request.dry_run:  # B3.10: a dry-run denial is not audited
            raise error
        event = events.refusal(
            status="denied",
            code=first.code,
            actor=request.actor,
            decisions=decisions,
            request=request.request,
            ts=self._clock.now(),
            event_id=EventId(self._ids.new_id()),
            env=self._env,
            graph=entry,
            reason=cs.reason,
            target={"types": sorted({t for _, t in resolution.pairs})},
        )
        try:
            self._audit.append_installation(event)
        except GraphCliError as exc:
            raise GraphCliError(
                error.code, error.message, details=error.details, warnings=(_not_recorded(exc),)
            ) from exc
        raise error

    # ---- W.6-W.16 ------------------------------------------------------

    def _apply(self, request: WriteRequest) -> WriteResult:
        prepared = self._prepare(request, None)
        with prepared.write.session(timeout_s=self.config.lock_timeout_s) as session:
            entry = self._catalog.resolve(str(prepared.entry.id))  # W.6: re-read the manifest
            _check_state(entry)
            model = prepared.model if entry.pin == prepared.entry.pin else self._models.load(entry)
            base = session.snapshot()
            head = session.head()
            resolution = self._resolve(request, base)
            decisions = prepared.decisions
            self._policy.refresh()  # a policy applied meanwhile decides (B6.14)
            if resolution.pairs != prepared.resolution.pairs or _stale(
                decisions, self._policy.current(entry.id).hash
            ):
                decisions = self._authorize(request, entry, resolution)
            refusal = _Refusal(self, session, request, entry, decisions, head)
            cs = resolution.changeset
            digest = changeset_hash(cs)
            if cs.idempotency_key is not None:  # W.7
                principal = request.actor.principal.id or ""
                previous = session.lookup_idempotency(cs.idempotency_key, principal)
                if previous is not None:
                    if previous.changeset_hash == digest:
                        return _replay(previous, entry, resolution.warnings)
                    raise refusal(
                        GraphCliError(
                            "conflict.idempotency_key_reuse",
                            f"idempotency key {cs.idempotency_key!r} was used for other content",
                            details={"idempotency_key": cs.idempotency_key, "seq": previous.seq},
                        ),
                        types=resolution.pairs,
                    )
            conflict = _conflict(cs, base, head.seq, resolution.refs)  # W.8
            if conflict is not None:
                raise refusal(conflict, types=resolution.pairs)
            recorded_at = _recorded_at(self._clock, head)  # W.9
            tx_id, event_id = TxId(self._ids.new_id()), EventId(self._ids.new_id())
            working = normalize(  # W.10
                cs.ops,
                base,
                model,
                refs=resolution.refs,
                new_id=self._ids.new_id,
                recorded_at=recorded_at,
                tx_id=tx_id,
                tx_seq=head.seq + 1,
                pin_version=entry.pin.version,
                alias_counters=session.alias_counters(),
                alias_prefixes=entry.alias_prefixes,
            )
            stored = _blob_check(request, prepared.read)
            findings = _validate(model, base, working, stored)  # W.11
            if any(f.severity is Severity.ERROR for f in findings):
                # ids minted for creates are never committed: name them by local
                findings = [_scrub(f, working, working.created) for f in findings]
                raise refusal(
                    _invalid(findings), types=resolution.pairs, findings=findings, working=working
                )
            tx = AuthorizedTx(
                graph_id=entry.id,
                tx_id=tx_id,
                event_id=event_id,
                seq=head.seq + 1,
                recorded_at=recorded_at,
                actor=request.actor,
                decisions=decisions,
                changeset=changeset_to_json(cs),
                changeset_hash=digest,
                resolution=working.resolution,
                revisions=tuple(working.revisions.values()),
                alias_allocations=working.alias_allocations,
                ontology=entry.pin,
                reason=cs.reason,
                idempotency_key=cs.idempotency_key,
                request=request.request,
                blobs=_tx_blobs(model, working),
            )
            listed = frozenset(tx.blobs)
            published = [
                session.publish_blob(b) for b in request.blobs if b.hash in listed
            ]  # W.11b
            before = {}
            for revision in tx.revisions:
                previous_rev = base.get(revision.id)
                if previous_rev is not None:
                    before[revision.id] = previous_rev.content_hash
            intent = events.write_intent(
                tx, graph=entry, env=self._env, before=before, blobs=published
            )
            receipt = session.append_audit(intent)  # W.12
            committed = session.commit(tx, receipt)  # W.13
        return _result(
            entry,
            working,
            tx_id=tx_id,
            seq=committed.seq,
            event_id=event_id,
            dry_run=False,
            warnings=(*resolution.warnings, *committed.warnings),
            findings=findings,
        )

    def _dry_run(
        self,
        request: WriteRequest,
        base: Snapshot | None,
        placeholders: _Placeholders,
        offset: int,
    ) -> tuple[WriteResult, WorkingState]:
        prepared = self._prepare(request, base)
        snapshot = prepared.snapshot
        resolution = prepared.resolution
        cs = resolution.changeset
        conflict = _conflict(cs, snapshot, snapshot.seq + offset, resolution.refs)
        if conflict is not None:
            raise conflict
        working = normalize(
            cs.ops,
            snapshot,
            prepared.model,
            refs=resolution.refs,
            new_id=placeholders,
            recorded_at=self._clock.now(),
            tx_id=TxId(placeholders()),
            tx_seq=snapshot.seq + offset + 1,
            pin_version=prepared.entry.pin.version,
            alias_counters={},
            alias_prefixes=prepared.entry.alias_prefixes,
            allocate_aliases=False,
        )
        hidden = placeholders.issued
        stored = _blob_check(request, prepared.read)
        findings = [
            _scrub(f, working, hidden) for f in _validate(prepared.model, snapshot, working, stored)
        ]
        if any(f.severity is Severity.ERROR for f in findings):
            raise _invalid(findings)
        result = _result(
            prepared.entry,
            working,
            tx_id=None,
            seq=None,
            event_id=None,
            dry_run=True,
            warnings=resolution.warnings,
            findings=findings,
            hidden=hidden,
        )
        return result, working

    def _append_refusal(
        self,
        session: WriteSession,
        request: WriteRequest,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        head: Head,
        error: GraphCliError,
        *,
        types: Sequence[tuple[Action, TermRef]],
        findings: Sequence[Finding],
        entities: Sequence[EntityId],
    ) -> GraphCliError:
        status: RefusalStatus = "invalid" if error.code == "write.invalid" else "conflict"
        target: dict[str, list[str]] = {"types": sorted({t for _, t in types})}
        if entities:
            target["entities"] = sorted(str(e) for e in entities)
        event = events.refusal(
            status=status,
            code=error.code,
            actor=request.actor,
            decisions=decisions,
            request=request.request,
            ts=_recorded_at(self._clock, head),
            event_id=EventId(self._ids.new_id()),
            env=self._env,
            graph=entry,
            findings=[f for f in findings if f.severity is Severity.ERROR],
            reason=request.changeset.reason,
            target=target,
        )
        try:
            session.append_audit(event)
        except GraphCliError as exc:
            return GraphCliError(
                error.code, error.message, details=error.details, warnings=(_not_recorded(exc),)
            )
        return error


@dataclass
class _Refusal:
    """Raise-ready refusal errors for one locked request (B3.14)."""

    service: WriteService
    session: WriteSession
    request: WriteRequest
    entry: GraphEntry
    decisions: tuple[Decision, ...]
    head: Head

    def __call__(
        self,
        error: GraphCliError,
        *,
        types: Sequence[tuple[Action, TermRef]],
        findings: Sequence[Finding] = (),
        working: WorkingState | None = None,
    ) -> GraphCliError:
        entities = (
            () if working is None else [e for e in working.revisions if e not in working.created]
        )
        return self.service._append_refusal(
            self.session,
            self.request,
            self.entry,
            self.decisions,
            self.head,
            error,
            types=types,
            findings=findings,
            entities=entities,
        )


def _not_recorded(exc: GraphCliError) -> WarningRecord:
    return WarningRecord(
        "audit.refusal_not_recorded", "the refusal could not be audited", {"cause": exc.code}
    )


def _recorded_at(clock: Clock, head: Head) -> datetime:
    now = clock.now()
    return now if head.recorded_at is None else max(now, head.recorded_at)


def _conflict(
    cs: ChangeSet, snapshot: Snapshot, seq: int, refs: Mapping[str, EntityId]
) -> GraphCliError | None:
    """W.8 (B3.11): document ``expect_seq``, then per-op ``expect_rev``."""
    if cs.expect_seq is not None and cs.expect_seq != seq:
        return GraphCliError(
            "conflict.seq_mismatch",
            f"the graph is at seq {seq}, expected {cs.expect_seq}",
            details={"expected": cs.expect_seq, "actual": seq},
        )
    for index, op in enumerate(cs.ops):
        expect = getattr(op, "expect_rev", None)
        if expect is None:
            continue
        entity = _entity_of(op.id, refs)  # type: ignore[union-attr]
        current = None if entity is None else snapshot.get(entity)
        actual = None if current is None else current.rev
        if actual != expect:
            return GraphCliError(
                "conflict.version_mismatch",
                f"ops[{index}]: the entity is at rev {actual}, expected {expect}",
                details={
                    "path": f"ops[{index}].expect_rev",
                    "id": None if entity is None else str(entity),
                    "expected": expect,
                    "actual": actual,
                },
            )
    return None


def _validate(
    model: CompiledModel,
    base: Snapshot,
    working: WorkingState,
    blob_exists: Callable[[str], bool],
) -> list[Finding]:
    found = validate_write(
        model,
        base,
        working,
        touched=tuple(working.revisions),
        effects=working.effects,
        alias_allocations=working.alias_allocations,
        local_names={entity: name for name, entity in working.locals.items()},
        blob_exists=blob_exists,
    )
    return sort_findings([*working.findings, *found])


def _blob_check(request: WriteRequest, read: GraphStoreRead) -> Callable[[str], bool]:
    """A blob is present when the request stages it or the store holds it."""
    staged = frozenset(b.hash for b in request.blobs)
    return lambda digest: digest in staged or read.blob(digest) is not None


def _tx_blobs(model: CompiledModel, working: WorkingState) -> tuple[str, ...]:
    """Every ``blob`` value of the tx's revisions, sorted and unique (tx ``blobs``)."""
    found = {h for r in working.revisions.values() for h in blob_values(model, r)}
    return tuple(sorted(found))


def _invalid(findings: Sequence[Finding]) -> GraphCliError:
    errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    return GraphCliError(
        "write.invalid",
        f"the write violates the ontology ({errors} error finding{'s' if errors != 1 else ''})",
        details={"findings": [f.to_json() for f in findings]},
    )


def _scrub(finding: Finding, working: WorkingState, hidden: Set[UUID]) -> Finding:
    """Dry-run: replace placeholder ids by local names; entities created in the request
    (or an earlier document of a dry-run batch) carry no ``id``."""
    labels = {}
    for placeholder in hidden:
        local = working.local_of(EntityId(placeholder))
        labels[str(placeholder)] = f"@{local}" if local else "(new entity)"

    def clean(text: str) -> str:
        for placeholder, label in labels.items():
            text = text.replace(placeholder, label)
        return text

    target = dict(finding.target)
    if str(target.get("id")) in labels:
        del target["id"]
    actual = finding.actual
    if isinstance(actual, list):
        actual = [clean(a) if isinstance(a, str) else a for a in actual]
    elif isinstance(actual, str):
        actual = clean(actual)
    return replace(finding, target=target, message=clean(finding.message), actual=actual)


def _op_names(working: WorkingState) -> dict[EntityId, list[str]]:
    names: dict[EntityId, list[str]] = {e: [] for e in working.revisions}
    for effect in working.effects:
        if not effect.applied:
            continue
        for entity in (effect.entity, *effect.targets):
            if entity in names and effect.op not in names[entity]:
                names[entity].append(effect.op)
    return names


def _result(
    entry: GraphEntry,
    working: WorkingState,
    *,
    tx_id: TxId | None,
    seq: int | None,
    event_id: EventId | None,
    dry_run: bool,
    warnings: Sequence[WarningRecord],
    findings: Sequence[Finding],
    hidden: Set[UUID] = frozenset(),
) -> WriteResult:
    names = _op_names(working)
    entities = tuple(
        EntityResult(
            id=None if entity in hidden else entity,
            alias=revision.alias,
            rev=revision.rev,
            type=revision.type,
            ops=tuple(names[entity]),
            local=working.local_of(entity),
        )
        for entity, revision in working.revisions.items()
    )
    bound = (
        {}
        if dry_run
        else {name: entity for name, entity in working.locals.items() if entity in working.created}
    )
    return WriteResult(
        graph_id=entry.id,
        graph_name=entry.name,
        tx_id=tx_id,
        seq=seq,
        event_id=event_id,
        dry_run=dry_run,
        idempotent_replay=False,
        entities=entities,
        warnings=tuple(warnings),
        findings=tuple(f for f in findings if f.severity is not Severity.ERROR),
        bindings=bound,
    )


def _replay(tx: TxRecord, entry: GraphEntry, warnings: Sequence[WarningRecord]) -> WriteResult:
    """The original result of an idempotent retry, rebuilt from its tx record (B3.12)."""
    try:
        cs = parse_changeset(tx.changeset, max_ops=sys.maxsize)
        locals_raw = as_object(tx.resolution.get("locals", {}), "locals")
        refs_raw = as_object(tx.resolution.get("refs", {}), "refs")
        locals_map = {n: EntityId(parse_id(str(v))) for n, v in locals_raw.items()}
        refs_map = {k: EntityId(parse_id(str(v))) for k, v in refs_raw.items()}
    except (GraphCliError, JsonShapeError) as exc:
        raise GraphCliError("internal.error", f"tx {tx.seq} cannot be replayed: {exc}") from exc
    created: Iterator[EntityId] = iter(r.id for r in tx.revisions if r.rev == 1)
    names: dict[EntityId, list[str]] = {r.id: [] for r in tx.revisions}

    def note(entity: EntityId | None, op: str) -> None:
        if entity is not None and entity in names and op not in names[entity]:
            names[entity].append(op)

    for op in cs.ops:
        if isinstance(op, CreateOp):
            note(next(created), op.OP)
        for ref in _subject_refs(op):
            if isinstance(ref, LocalRef) and ref.name in locals_map:
                note(locals_map[ref.name], op.OP)
            else:
                note(_entity_of(ref, refs_map), op.OP)
    local_names = {e: n for n, e in locals_map.items()}
    local_names.update({e: k[1:] for k, e in refs_map.items() if k.startswith("@")})
    entities = tuple(
        EntityResult(r.id, r.alias, r.rev, r.type, tuple(names[r.id]), local_names.get(r.id))
        for r in tx.revisions
    )
    return WriteResult(
        graph_id=entry.id,
        graph_name=entry.name,
        tx_id=tx.tx_id,
        seq=tx.seq,
        event_id=tx.event_id,
        dry_run=False,
        idempotent_replay=True,
        entities=entities,
        warnings=tuple(warnings),
        bindings=locals_map,
    )


def _stale(decisions: Sequence[Decision], policy_hash: str) -> bool:
    return any(d.policy_hash != policy_hash for d in decisions)
