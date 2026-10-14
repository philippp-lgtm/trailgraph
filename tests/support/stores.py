"""Backend factories and a write driver for the storage contract suite (C tests).

Every C test runs against each backend in ``BACKENDS`` and each toy
ontology. Tests talk to the ports only; the factory is the single place that
knows how a backend is built.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.ports.audit import AuditSink, graph_stream
from graph_cli.kernel.ports.storage import CommitResult, Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.memory.audit_log import InMemoryAuditLog
from graph_cli.storage.memory.store import InMemoryStore
from support.clock import StepClock
from support.drafts import intent
from support.ids import SeededIdProvider
from support.memlog import model
from support.txbuilder import FuncState, TxBuilder, TxDraft

ALL_STATUSES = frozenset({"active", "superseded", "retracted"})
BACKENDS = ("memory",)

Store = InMemoryStore


class FaultSwitch:
    """A fault hook that raises ``OSError`` at the armed points."""

    def __init__(self) -> None:
        self.armed: set[CrashPoint] = set()
        self.seen: list[CrashPoint] = []

    def __call__(self, point: CrashPoint) -> None:
        self.seen.append(point)
        if point in self.armed:
            raise OSError(f"injected fault at {point}")


def snapshot_state(snapshot: Snapshot) -> FuncState:
    def ids() -> list[EntityId]:
        page = snapshot.query(Query(record_status=ALL_STATUSES, limit=10**9))
        return [r.id for r in page.items]

    return FuncState(snapshot.get, ids)


@dataclass
class StoreEnv:
    backend: str
    ontology: str
    root: Path
    seed: int = 0
    faults: FaultSwitch = field(default_factory=FaultSwitch)

    def __post_init__(self) -> None:
        self.clock = StepClock()
        self.ids = SeededIdProvider(self.clock, self.seed)
        self.graph_id = GraphId(self.ids.new_id())
        self.stream = graph_stream(str(self.graph_id))
        self.builder = TxBuilder(
            model(self.ontology),
            graph_id=self.graph_id,
            ids=self.ids,
            clock=self.clock,
            seed=self.seed,
        )
        self.store, self.audit = make_backend(self.backend, self.graph_id, self.root, self.faults)

    def write(
        self,
        edit: Callable[[TxDraft], object],
        *,
        idempotency_key: str | None = None,
        principal: str = "test:1",
        reason: str | None = None,
    ) -> tuple[AuthorizedTx, CommitResult]:
        """One session: build a tx from the session snapshot, append its intent, commit."""
        with self.store.session(timeout_s=5) as session:
            draft = self.builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            edit(draft)
            tx = draft.build(idempotency_key=idempotency_key, principal=principal, reason=reason)
            receipt = session.append_audit(intent(tx))
            return tx, session.commit(tx, receipt)

    def random(self, count: int) -> None:
        for _ in range(count):
            self.write(self.builder.random_tx)


def make_backend(
    backend: str, graph_id: GraphId, root: Path, faults: FaultSwitch
) -> tuple[Store, AuditSink]:
    if backend == "memory":
        audit = InMemoryAuditLog(faults=faults)
        return InMemoryStore(graph_id, audit=audit, faults=faults), audit
    raise AssertionError(f"unknown backend {backend!r}")
