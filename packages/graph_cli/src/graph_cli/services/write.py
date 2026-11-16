"""The single write pipeline (contracts B3, section 4 W.1-W.16; plan W7).

``WriteService.apply`` is the only caller of ``WriteSession.commit`` in
graph_cli (B3.1). Steps:

- W.1 limits (the document itself is parsed by the caller with
  ``kernel.changeset``); W.2 graph state ``active``; W.3 the pinned model;
  W.4 references on a lock-free snapshot; W.5 one decision per
  (action, ``graph:<id>/type:<curie>``) pair; a denial is appended to the
  installation stream (not in dry-run) and raised, as ``graph.not_found``
  when the actor cannot see the graph (B6.15).
- W.5b the blobs the request staged are handed to the store for promotion
  (``GraphStoreWrite.promote_blobs``) after W.1-W.5 and before the lock: a
  store that keeps stored files elsewhere moves the bytes there, so a request
  that is over a limit, names a missing entity or is denied never reaches it.
  Validation (W.11) runs under the lock, so an authorized request found
  invalid there has promoted its blobs; no tx references them.
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

Action plans (actions plan A5, ``graph do``): a request with a ``plan`` carries only
the document frame (reason and key) and the action with its bound parameters. W.1-W.4
run as above on the empty frame, so nothing of the action is expanded, checked or
authorized before the lock. After W.6 the service, on the locked snapshot: expands the
template (``action.nothing_to_do`` is held, not raised yet), resolves the expansion and
authorizes it as W.5 does (an empty expansion is authorized by the action's declared
needs), then W.7 matches the idempotency key on the request digest and the action
instead of the change set hash, then checks the preconditions (``action.precondition_failed``
is an audited ``invalid`` refusal), then raises the held ``nothing_to_do`` (not audited),
and continues with W.8 on the expanded change set, which is what the tx records. A dry-run
does the same on its lock-free snapshot, without audit and idempotency.

Terms (write allow lists plan W4, W7): besides its (action, type) decision an op is decided on
the attributes and relations it names below its type (``Resolution.fine``), but only for a
type some rule of the graph's policy names terms of (``fine_types``); every other type is
decided as before, so policies without term rules produce the same decisions, events and
records. A transition without ``attr`` is decided on the lifecycle attribute of its type in
the model of the decision (the prepared one before the lock, the locked one under it); the
set of terms is compared like the pairs for the re-authorization under the lock. An action
whose expansion is empty is authorized by the any-of rule of ``services.action_rights`` on
its derived needs.

Refusals (B3.14): ``invalid`` and ``conflict`` go to the graph stream under
the lock; if that append fails the original error is raised with warning
``audit.refusal_not_recorded``. Usage errors and dry-runs are not audited.

Dry-run (B3.10, W.d) runs W.1-W.4 and W.8-W.11 on a lock-free snapshot with
placeholder ids that never leave the service: created entities report no
id and no alias, finding targets name them by ``local``. Its blobs are
hashed only, never staged on disk; they count as present. A refused real
write names its creates the same way: their minted ids are never committed.

Type read limits (type read limits plan V6): when the graph is limited and the caller does not
see every type (``services.visibility``), two snapshots are kept per request. The raw one is the
truth: ``normalize`` builds the new revisions from it (an update keeps the entity's hidden
edges), the conflict check and the ``before`` hashes read it, and validation judges the post-state
of the whole graph. A ``VisibleSnapshot`` of it is what the caller reaches: references resolve
through it (a hidden entity is ``entity.not_found``), so do the action expansion, the action's
preconditions and the file lookups; an unlink matches only edges into entities the caller sees.
The authorization also decides ``read.get`` on every type of the request and records it. A
create of a type the caller does not see is answered as a create of a type the model lacks: the
decisions that answer are taken on a resource no rule can name exactly
(``authorize_unlisted_type``), so a type limit never shows in the refusal, and validation judges
the type unknown (``CallerView``). The refusal is audited with the real decisions. Findings
about entities the caller does not see are concealed by the validator, and the result names only
entities of visible types. A write of any other caller is unchanged, byte for byte.

The command's timings get the phases ``model``, ``snapshot``, ``resolve``,
``authorize``, ``promote`` (a request with staged blobs: their upload to an object
store), ``lock`` (waiting for and taking the graph lock, with the
recovery of an interrupted write), ``normalize``, ``validate``, ``audit``
(the intent or a refusal) and ``commit``.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final
from uuid import UUID

from graph_cli.access_control.authorize import authorize, authorize_unlisted_type
from graph_cli.access_control.terms import term_resource
from graph_cli.audit import events
from graph_cli.audit.events import RefusalStatus
from graph_cli.kernel.actions import Action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthorizedTx, AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    MAX_BYTES,
    MAX_OPS,
    OP_ACTIONS,
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
from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code
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
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import EnvInfo, RequestMeta, TxRecord
from graph_cli.kernel.timing import Timings
from graph_cli.kinetic.action_entries import ActionEntry, find_action
from graph_cli.kinetic.action_needs import action_needs
from graph_cli.kinetic.expand import check_preconditions, expand
from graph_cli.ontology_layer.data_validator import CallerView, blob_values, validate_write
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value
from graph_cli.services import migration_state
from graph_cli.services.action_rights import FineTypes, need_decisions
from graph_cli.services.models import ModelCache
from graph_cli.services.read import denial_error
from graph_cli.services.refs import NO_BINDINGS, FineTerm, Resolution, resolve_refs
from graph_cli.services.visibility import Visibility, VisibleSnapshot
from graph_cli.temporal_knowledge.normalize import normalize
from graph_cli.temporal_knowledge.working import WorkingState

DEFAULT_LOCK_TIMEOUT_S: Final = 10.0
READ_GET: Final = Action("read.get")


@dataclass(frozen=True, slots=True)
class WriteConfig:
    max_ops: int = MAX_OPS
    max_bytes: int = MAX_BYTES
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """A published action and its parameters as ``kinetic.params`` binds them, ``ref`` values
    already resolved to full entity ids. Expanded by the service on the snapshot it judges."""

    entry: ActionEntry
    bound: Mapping[str, Value]


@dataclass(frozen=True, slots=True)
class WriteRequest:
    actor: ActorContext
    graph: str  # name or id
    changeset: ChangeSet  # with a ``plan``: the document frame, no ops
    dry_run: bool
    request: RequestMeta
    bindings: Mapping[str, EntityId] = field(default=NO_BINDINGS)  # earlier documents (B3.9)
    allow_abbrev: bool = False  # references from CLI arguments (B1.9)
    blobs: tuple[StagedBlob, ...] = ()  # staged by the caller, who discards them afterwards
    plan: ActionPlan | None = None  # an action to expand instead of ``changeset.ops``


@dataclass(frozen=True, slots=True)
class EntityResult:
    id: EntityId | None  # None for a dry-run create
    alias: str | None
    rev: int
    type: TermRef
    ops: tuple[str, ...]
    local: str | None
    changed: bool = True  # False: same content as before, no new revision

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": None if self.id is None else str(self.id),
            "alias": self.alias,
            "rev": self.rev,
            "type": self.type,
            "ops": list(self.ops),
            "local": self.local,
            "changed": self.changed,
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

    def to_json(self, *, summary: bool = False) -> dict[str, JsonValue]:
        """``summary`` leaves out the per-entity list (``entities: null``), not its count."""
        single = self.entities[0] if len(self.entities) == 1 else None
        return {
            "tx_id": None if self.tx_id is None else str(self.tx_id),
            "seq": self.seq,
            "event_id": None if self.event_id is None else str(self.event_id),
            "dry_run": self.dry_run,
            "idempotent_replay": self.idempotent_replay,
            "id": None if single is None or single.id is None else str(single.id),
            "version": None if single is None else single.rev,
            "entity_count": len(self.entities),
            "entities": None if summary else [e.to_json() for e in self.entities],
            "findings": [f.to_json() for f in self.findings],
        }


@dataclass(frozen=True, slots=True)
class BatchResult:
    """B3.9: one result per committed document; processing stopped at ``error``."""

    results: tuple[WriteResult, ...]
    error: GraphCliError | None = None
    failed_index: int | None = None


@dataclass(frozen=True, slots=True)
class _Wanted:
    """What a write is authorized on: the (action, type) pairs (B6.9) and the terms below the
    types (W4), before the policy says which types have term rules."""

    pairs: tuple[tuple[Action, TermRef], ...]
    fine: tuple[FineTerm, ...] = ()


@dataclass(frozen=True, slots=True)
class _Prepared:
    entry: GraphEntry
    model: CompiledModel
    read: GraphStoreRead
    write: GraphStoreWrite
    snapshot: Snapshot
    resolution: Resolution
    wanted: _Wanted
    decisions: tuple[Decision, ...]
    view: ReadView  # what the caller reads of the graph (``kernel.read_view``)


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
        timings: Timings | None = None,
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._env = env
        self.config = config or WriteConfig()
        self.timings = timings if timings is not None else Timings()
        self._models = ModelCache(catalog, self.timings)
        self._fine = FineTypes()
        self._visibility = Visibility()

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
        if any(request.plan is not None for request in requests):
            raise GraphCliError("internal.error", "action plans are applied one at a time")
        bindings: dict[str, EntityId] = {}
        results: list[WriteResult] = []
        base: Snapshot | None = None
        placeholders = _Placeholders()
        commits = 0  # dry-run documents before this one that would commit a tx
        for index, request in enumerate(requests):
            current = replace(request, bindings={**request.bindings, **bindings})
            try:
                if current.dry_run:
                    result, working = self._dry_run(current, base, placeholders, commits)
                    base = working
                    commits += 1 if working.revisions else 0
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
        if snapshot is None:
            with self.timings.phase("snapshot"):
                snapshot = read.snapshot()
        migration_state.refuse_write(entry)
        view = self._read_view(request, entry, model)
        resolution = self._resolve(  # a plan: the empty frame, no pairs
            request, _seen_by(snapshot, view)
        )
        wanted = _wanted(resolution, model)
        decisions = self._authorize(request, entry, wanted, model, view)
        return _Prepared(entry, model, read, write, snapshot, resolution, wanted, decisions, view)

    def _read_view(
        self, request: WriteRequest, entry: GraphEntry, model: CompiledModel
    ) -> ReadView:
        """What the caller reads of the graph under the current policy. The write path judges
        by its type half only (type read limits plan V6)."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            return self._visibility.of(request.actor, entry, policy, model)

    def _resolve(self, request: WriteRequest, snapshot: Snapshot) -> Resolution:
        with self.timings.phase("resolve"):
            return resolve_refs(
                request.changeset,
                snapshot,
                bindings=request.bindings,
                allow_abbrev=request.allow_abbrev,
            )

    def _plan_wanted(
        self,
        request: WriteRequest,
        entry: GraphEntry,
        plan: ActionPlan,
        model: CompiledModel,
        view: ReadView,
    ) -> _Wanted:
        """What an action whose expansion is empty is authorized on: for each need of the
        action on ``model`` the option the any-of rule picks (``services.action_rights``).

        A caller who does not see every type meets a need only through a type it sees. An
        action with a need it cannot meet is hidden from it (``ActionService`` answers
        ``action.not_found``); should one get here anyway (the policy changed meanwhile), the
        answer is the one for an action that does not exist, never an authorization of
        nothing."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            fine = self._fine.of(policy, entry.id)
            ctx = _context(request)
            pairs: set[tuple[Action, TermRef]] = set()
            terms: set[FineTerm] = set()
            derived = action_needs(plan.entry, model)
            for need in derived:
                found = need_decisions(request.actor, entry.id, need, ctx, policy, fine, view.types)
                if view.types_limited and not found.met and not found.decisions:
                    find_action((), plan.entry.name)  # raises action.not_found
                if found.type is None:
                    continue
                pairs.add((Action(need.action), TermRef(found.type)))
                terms.update(
                    (Action(a), TermRef(found.type), kind, term) for a, kind, term in found.terms
                )
            covered = {(n.action, n.type) for n in derived}
            for published in plan.entry.needs:  # a published need the derivation did not see
                if (published.action, published.type) not in covered:
                    pairs.add((Action(published.action), TermRef(published.type)))
        return _Wanted(tuple(sorted(pairs)), tuple(sorted(terms)))

    def _authorize(
        self,
        request: WriteRequest,
        entry: GraphEntry,
        wanted: _Wanted,
        model: CompiledModel | None = None,
        view: ReadView = UNLIMITED,
    ) -> tuple[Decision, ...]:
        """The decisions of W.5 for ``wanted``; ``view`` (what the caller reads, with the
        ``model`` its types are types of) matters only for a caller under a type read limit."""
        pairs = wanted.pairs
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            ctx = _context(request)
            fine = self._fine.of(policy, entry.id)
            # A type the model has but the caller's view lacks answers as a type the model
            # lacks (V6): its decisions are taken on a resource no rule names, below.
            unseen: frozenset[str] = frozenset()
            if view.types_limited:
                assert model is not None
                limited = self._visibility.model(entry, model, view)
                unseen = frozenset(
                    t for _, t in pairs if t in model.classes and t not in limited.classes
                )
            resources = {(a, f"graph:{entry.id}/type:{t}") for a, t in pairs}
            for a, t, kind, term in wanted.fine:
                resource = term_resource(entry.id, t, kind, term) if t in fine else None
                if resource is not None and t not in unseen:
                    resources.add((a, resource))
            recorded = {
                (a, r): authorize(request.actor, a, r, ctx, policy) for a, r in sorted(resources)
            }
            # What the caller is answered by: the decision, or for an unseen type the one on
            # a resource no rule names, whatever the rules say of the type itself.
            answering = {
                key: (
                    authorize_unlisted_type(
                        request.actor, key[0], entry.id, _type_of(key[1]), ctx, policy
                    )
                    if _type_of(key[1]) in unseen
                    else d
                )
                for key, d in recorded.items()
            }
            if view.types_limited:  # V1: the read decision of every type the request names
                assert model is not None
                for t in sorted({t for _, t in pairs if t in model.classes}):
                    if t in unseen or not model.classes[t].abstract:
                        resource = f"graph:{entry.id}/type:{t}"
                        recorded[(READ_GET, resource)] = authorize(
                            request.actor, READ_GET, resource, ctx, policy
                        )
            decisions = tuple(recorded[key] for key in sorted(recorded))
        denied = [d for _, d in sorted(answering.items()) if d.effect == "deny"]
        if not denied:
            return decisions
        first = denied[0]
        assert first.code is not None
        visible_graph = authorize(request.actor, "read.list", f"graph:{entry.id}", ctx, policy)
        error = (
            denial_error(first)
            if visible_graph.effect == "allow"
            else graph_not_found(request.graph)
        )  # B6.15: a graph the actor cannot see does not exist for it
        if request.dry_run:  # B3.10: a dry-run denial is not audited
            raise error
        # The event names the decisions taken; the one that answered the caller is among them
        # (it was taken on a resource no rule names when the type is hidden from the caller).
        named = decisions
        if not any(d.effect == "deny" and d.code == first.code for d in decisions):
            named = (*decisions, first)
        raise self._append_denial(request, entry, named, error, {t for _, t in pairs})

    def _append_denial(
        self,
        request: WriteRequest,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        error: GraphCliError,
        types: Set[str],
    ) -> GraphCliError:
        """Audit a denied request on the installation stream; ``error`` with the warning
        ``audit.refusal_not_recorded`` when that fails."""
        event = events.refusal(
            status="denied",
            code=_denial_code(decisions, error),
            actor=request.actor,
            decisions=decisions,
            request=request.request,
            ts=self._clock.now(),
            event_id=EventId(self._ids.new_id()),
            env=self._env,
            graph=entry,
            reason=request.changeset.reason,
            target={"types": sorted(types)},
        )
        try:
            with self.timings.phase("audit"):
                self._audit.append_installation(event)
        except GraphCliError as exc:
            return GraphCliError(
                error.code, error.message, details=error.details, warnings=(_not_recorded(exc),)
            )
        return error

    # ---- W.6-W.16 ------------------------------------------------------

    def _apply(self, request: WriteRequest) -> WriteResult:
        prepared = self._prepare(request, None)
        if request.blobs:  # W.5b
            with self.timings.phase("promote"):
                prepared.write.promote_blobs(request.blobs)
        with ExitStack() as stack:
            with self.timings.phase("lock"):
                session = stack.enter_context(
                    prepared.write.session(timeout_s=self.config.lock_timeout_s)
                )
            entry = self._catalog.resolve(str(prepared.entry.id))  # W.6: re-read the manifest
            _check_state(entry)
            model = prepared.model if entry.pin == prepared.entry.pin else self._models.load(entry)
            with self.timings.phase("snapshot"):
                base = session.snapshot()
                head = session.head()
            # a release that crashed after its migration tx while this write waited for the lock
            migration_state.refuse_write(entry)
            self._policy.refresh()  # a policy applied meanwhile decides (B6.14)
            view = self._read_view(request, entry, model)
            seen = _seen_by(base, view)  # what the caller reaches; ``base`` stays the truth
            limited = self._visibility.model(entry, model, view) if view.types_limited else None
            plan, held = request.plan, None
            if plan is not None:  # the action meets the state it will change
                request, held = self._expand(request, seen)
            resolution = self._resolve(request, seen)
            if plan is not None and held is not None:
                wanted = self._plan_wanted(request, entry, plan, model, view)
            else:
                wanted = _wanted(resolution, model)
            pairs = wanted.pairs
            decisions = prepared.decisions
            if (
                plan is not None
                or wanted != prepared.wanted
                or _stale(decisions, self._policy.current(entry.id).hash)
            ):
                decisions = self._authorize(request, entry, wanted, model, view)
            refusal = _Refusal(self, session, request, entry, decisions, head)
            cs = resolution.changeset
            digest = changeset_hash(cs)
            if cs.idempotency_key is not None:  # W.7
                principal = request.actor.principal.id or ""
                previous = session.lookup_idempotency(cs.idempotency_key, principal)
                if previous is not None:
                    if _same_request(request, previous, digest):
                        return _replay(previous, entry, resolution.warnings, view)
                    raise refusal(
                        GraphCliError(
                            "conflict.idempotency_key_reuse",
                            f"idempotency key {cs.idempotency_key!r} was used for other content",
                            details={"idempotency_key": cs.idempotency_key, "seq": previous.seq},
                        ),
                        types=pairs,
                    )
            if plan is not None:
                self._check_plan(plan, seen, model, refusal, pairs)
                if held is not None:  # nothing would change: no tx, no audit event
                    raise held
            conflict = _conflict(cs, base, head.seq, resolution.refs)  # W.8
            if conflict is not None:
                raise refusal(conflict, types=pairs)
            recorded_at = _recorded_at(self._clock, head)  # W.9
            tx_id, event_id = TxId(self._ids.new_id()), EventId(self._ids.new_id())
            with self.timings.phase("normalize"):
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
                    default_language=entry.language,
                    sees=_sees_in(seen),
                )
            with self.timings.phase("validate"):
                stored = _blob_check(request, prepared.read)
                findings = _validate(  # W.11
                    model, base, working, stored, _caller_view(limited, view, working)
                )
            if any(f.severity is Severity.ERROR for f in findings):
                # ids minted for creates are never committed: name them by local
                findings = [_scrub(f, working, working.created) for f in findings]
                raise refusal(_invalid(findings), types=pairs, findings=findings, working=working)
            if not working.revisions:  # nothing changes: no tx, no audit event
                return _result(
                    entry,
                    working,
                    tx_id=None,
                    seq=None,
                    event_id=None,
                    dry_run=False,
                    warnings=resolution.warnings,
                    findings=findings,
                    view=view,
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
            with self.timings.phase("audit"):
                receipt = session.append_audit(intent)  # W.12
            with self.timings.phase("commit"):
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
            view=view,
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
        view = prepared.view
        seen = _seen_by(snapshot, view)
        limited = (
            self._visibility.model(prepared.entry, prepared.model, view)
            if view.types_limited
            else None
        )
        plan = request.plan
        if plan is not None:  # as under the lock, on the lock-free snapshot, unaudited
            request, held = self._expand(request, seen)
            resolution = self._resolve(request, seen)
            if held is not None:
                wanted = self._plan_wanted(request, prepared.entry, plan, prepared.model, view)
            else:
                wanted = _wanted(resolution, prepared.model)
            pairs = wanted.pairs
            self._authorize(request, prepared.entry, wanted, prepared.model, view)
            self._check_plan(plan, seen, prepared.model, None, pairs)
            if held is not None:
                raise held
        cs = resolution.changeset
        conflict = _conflict(cs, snapshot, snapshot.seq + offset, resolution.refs)
        if conflict is not None:
            raise conflict
        with self.timings.phase("normalize"):
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
                default_language=prepared.entry.language,
                sees=_sees_in(seen),
            )
        hidden = placeholders.issued
        with self.timings.phase("validate"):
            stored = _blob_check(request, prepared.read)
            findings = _validate(
                prepared.model, snapshot, working, stored, _caller_view(limited, view, working)
            )
        findings = [_scrub(f, working, hidden) for f in findings]
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
            view=view,
        )
        return result, working

    def _expand(
        self, request: WriteRequest, snapshot: Snapshot
    ) -> tuple[WriteRequest, GraphCliError | None]:
        """The request with the plan's expansion as its change set, or, when the action has
        nothing to do in this state, the request as it was and the ``action.nothing_to_do``
        to raise once the idempotency key had its say."""
        plan = request.plan
        assert plan is not None
        frame = request.changeset
        try:
            expanded = expand(
                plan.entry,
                plan.bound,
                snapshot,
                reason=frame.reason,
                idempotency_key=frame.idempotency_key,
            )
        except GraphCliError as exc:
            if exc.code != "action.nothing_to_do":
                raise
            return request, exc
        if len(expanded.ops) > self.config.max_ops:
            raise GraphCliError(
                "changeset.too_large",
                f"the action expands to {len(expanded.ops)} ops",
                details={"limit_ops": self.config.max_ops, "actual_ops": len(expanded.ops)},
            )
        return replace(request, changeset=expanded), None

    def _check_plan(
        self,
        plan: ActionPlan,
        snapshot: Snapshot,
        model: CompiledModel,
        refusal: _Refusal | None,
        pairs: Sequence[tuple[Action, TermRef]],
    ) -> None:
        """The preconditions on ``snapshot``; a failure is an audited refusal when the
        locked write has a ``refusal`` to raise (``invalid``, naming the entities)."""
        try:
            check_preconditions(plan.entry, plan.bound, snapshot, model)
        except GraphCliError as exc:
            if refusal is None or exc.code != "action.precondition_failed":
                raise
            failed = exc.details.get("failed")
            assert isinstance(failed, list)
            entities = [
                EntityId(parse_id(str(item["entity"])))
                for item in failed
                if isinstance(item, Mapping)
            ]
            raise refusal(exc, types=pairs, entities=entities) from None

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
        if any(d.effect == "deny" for d in decisions):
            # Decisions that deny were taken on a type the caller cannot see, and the answer
            # followed from the rest of the request: the request is a denied one (the event of a
            # refusal after authorization carries no denial).
            return self._append_denial(request, entry, decisions, error, {t for _, t in types})
        spec = lookup_error_code(error.code)
        invalid = spec is not None and spec.category is ErrorCategory.INVALID
        status: RefusalStatus = "invalid" if invalid else "conflict"
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
            with self.timings.phase("audit"):
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
        entities: Sequence[EntityId] = (),
    ) -> GraphCliError:
        if working is not None:
            entities = [e for e in working.revisions if e not in working.created]
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


def _denial_code(decisions: Sequence[Decision], error: GraphCliError) -> str:
    """The code of a ``denied`` event whose answer is ``error``: the error's own code when a
    decision denied with it, else the first denial's (the event needs one of its decisions)."""
    codes = [d.code for d in decisions if d.effect == "deny" and d.code is not None]
    return error.code if error.code in codes else codes[0]


def _type_of(resource: str) -> str:
    """The type of a ``graph:<id>/type:<curie>`` resource (these are the only ones decided
    for an op's type; a term resource below it is cut off)."""
    return resource.split("/type:", 1)[1].split("/", 1)[0]


def _seen_by(snapshot: Snapshot, view: ReadView) -> Snapshot:
    """What the caller reaches of ``snapshot``: all of it, or the entities of the types it
    sees (the type half of ``view``)."""
    return VisibleSnapshot(snapshot, view.types_only()) if view.types_limited else snapshot


def _sees_in(seen: Snapshot) -> Callable[[EntityId], bool] | None:
    """The predicate of a limited view (``None``: nothing is limited)."""
    return seen.sees if isinstance(seen, VisibleSnapshot) else None


def _caller_view(
    limited: CompiledModel | None, view: ReadView, working: WorkingState
) -> CallerView | None:
    """What the validator may tell the caller (``None``: everything). The caller sees the
    entities its request creates, whatever their type: the type is judged on its own."""
    if not view.types_limited or limited is None:
        return None
    memo: dict[EntityId, bool] = {}

    def sees(entity: EntityId) -> bool:
        if entity in working.created:
            return True
        if entity not in memo:
            found = working.get(entity)
            memo[entity] = found is not None and view.shows_type(found.type)
        return memo[entity]

    return CallerView(limited, view, sees)


def _context(request: WriteRequest) -> AuthzContext:
    cs = request.changeset
    return AuthzContext(
        op_count=len(cs.ops), has_reason=cs.reason is not None, dry_run=request.dry_run
    )


def _wanted(resolution: Resolution, model: CompiledModel) -> _Wanted:
    """The pairs and terms of a resolved change set; a transition without ``attr`` is decided
    on the lifecycle attribute ``model`` gives its type (none: no term)."""
    fine = set(resolution.fine)
    for type_curie in resolution.bare_transitions:
        attr = model.lifecycle_attribute(type_curie)
        if attr is not None:
            fine.add((OP_ACTIONS["transition"], type_curie, "attr", attr))
    return _Wanted(resolution.pairs, tuple(sorted(fine)))


def _same_request(request: WriteRequest, previous: TxRecord, digest: str) -> bool:
    """Whether a retry with the same idempotency key asks for what ``previous`` recorded: an
    action matches on the request digest and the action (its expansion depends on the state),
    a change set on its hash."""
    if request.plan is None:
        return previous.changeset_hash == digest
    return (
        previous.request.args_digest == request.request.args_digest
        and previous.request.action == request.request.action
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
    view: CallerView | None,
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
        view=view,
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

    def scrub(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, Mapping):
            return {k: scrub(v) for k, v in value.items()}
        return value

    actual = scrub(finding.actual)
    return replace(finding, target=target, message=clean(finding.message), actual=actual)


def _op_names(working: WorkingState) -> dict[EntityId, list[str]]:
    names: dict[EntityId, list[str]] = {e: [] for e in (*working.revisions, *working.unchanged)}
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
    view: ReadView = UNLIMITED,
) -> WriteResult:
    """The result of a write. A caller who does not see every type is told about entities of
    the types it sees only: whatever else the request changed stays in the tx."""
    names = _op_names(working)
    if view.types_limited:
        working = replace(
            working,
            revisions={e: r for e, r in working.revisions.items() if view.shows_type(r.type)},
            unchanged={e: r for e, r in working.unchanged.items() if view.shows_type(r.type)},
        )
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
    ) + tuple(
        EntityResult(
            id=entity,
            alias=revision.alias,
            rev=revision.rev,
            type=revision.type,
            ops=tuple(names[entity]),
            local=working.local_of(entity),
            changed=False,
        )
        for entity, revision in working.unchanged.items()
    )
    if working.unchanged:
        warnings = [*warnings, _unchanged(working)]
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


def _unchanged(working: WorkingState) -> WarningRecord:
    revisions = list(working.unchanged.values())
    names = ", ".join(r.alias or str(r.id) for r in revisions[:5])
    more = f" and {len(revisions) - 5} more" if len(revisions) > 5 else ""
    nothing = "" if working.revisions else "; nothing was written"
    return WarningRecord(
        "entity.unchanged",
        f"already as given, no new revision: {names}{more}{nothing}",
        {"ids": [str(r.id) for r in revisions], "count": len(revisions)},
    )


def _replay(
    tx: TxRecord,
    entry: GraphEntry,
    warnings: Sequence[WarningRecord],
    view: ReadView,
) -> WriteResult:
    """The original result of an idempotent retry, rebuilt from its tx record (B3.12); for a
    caller who does not see every type, of the entities of the types it sees."""
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
        if view.shows_type(r.type)
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
