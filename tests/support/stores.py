"""Backend factories and a write driver for the storage contract suite (C tests).

Every C test runs against each backend in ``BACKENDS`` and each toy
ontology. Tests talk to the ports only; the factory is the single place that
knows how a backend is built.
"""

import fcntl
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditSink, graph_stream
from graph_cli.kernel.ports.storage import CommitResult, Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.layout import init_graph_dir
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.memory.audit_log import InMemoryAuditLog
from graph_cli.storage.memory.store import InMemoryStore
from support import audit_events
from support.clock import StepClock
from support.drafts import intent
from support.ids import SeededIdProvider
from support.memlog import model
from support.txbuilder import FuncState, TxBuilder, TxDraft

ALL_STATUSES = frozenset({"active", "superseded", "retracted"})
BACKENDS = ("memory", "files")

Store = InMemoryStore | FileStore


def fd_path(fd: int) -> str:
    """The path of an open descriptor (darwin: F_GETPATH, Linux: /proc)."""
    if sys.platform == "darwin":
        raw = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH
        return os.fsdecode(raw.rstrip(b"\0"))
    return os.readlink(f"/proc/self/fd/{fd}")


class RecordingSyncer:
    """A no-op syncer that records what would have been synced, in order (plan D2)."""

    def __init__(self, *, record: bool = False) -> None:
        self.record = record
        self.calls: list[tuple[str, str]] = []

    def sync_file(self, fd: int) -> None:
        if self.record:
            self.calls.append(("file", fd_path(fd)))

    def sync_dir(self, path: Path) -> None:
        if self.record:
            self.calls.append(("dir", str(path)))


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
    real_audit: bool = False  # graph_cli.audit events instead of minimal drafts

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
        self.syncer = RecordingSyncer()
        self.store, self.audit = make_backend(
            self.backend, self.graph_id, self.root, self.faults, self.syncer
        )

    def audit_head(self) -> tuple[int, str]:
        """Head of this graph's audit stream."""
        return self.audit.head(self.stream)

    def audit_events(self) -> Iterator[Mapping[str, JsonValue]]:
        return self.audit.read(self.stream)

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
            receipt = session.append_audit(self.intent(tx, session.snapshot()))
            return tx, session.commit(tx, receipt)

    def write_blobs(
        self, edit: Callable[[TxDraft], object], contents: Sequence[bytes]
    ) -> tuple[AuthorizedTx, CommitResult]:
        """Like ``write``, with ``contents`` staged before the lock, published under it and
        listed in the tx (W.11b)."""
        staged = [self.store.stage_blob([data], max_bytes=len(data)) for data in contents]
        with self.store.session(timeout_s=5) as session:
            draft = self.builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            edit(draft)
            for blob in staged:
                session.publish_blob(blob)
            tx = replace(draft.build(), blobs=tuple(sorted({b.hash for b in staged})))
            receipt = session.append_audit(self.intent(tx, session.snapshot()))
            return tx, session.commit(tx, receipt)

    def intent(self, tx: AuthorizedTx, snapshot: Snapshot) -> AuditEventDraft:
        """The intent this env appends for ``tx``; ``snapshot`` is the pre-state."""
        if not self.real_audit:
            return intent(tx)
        return audit_events.intent(tx, before=audit_events.before_hashes(snapshot, tx))

    def random(self, count: int) -> None:
        for _ in range(count):
            self.write(self.builder.random_tx)


def make_backend(
    backend: str, graph_id: GraphId, root: Path, faults: FaultSwitch, syncer: RecordingSyncer
) -> tuple[Store, AuditSink]:
    if backend == "memory":
        audit = InMemoryAuditLog(faults=faults)
        return InMemoryStore(graph_id, audit=audit, faults=faults), audit
    if backend == "files":
        graph_dir = root / "graphs" / str(graph_id)
        init_graph_dir(graph_dir, syncer)
        store = FileStore(graph_dir, graph_id, syncer=syncer, faults=faults)
        return store, FileAuditLog(root, syncer=syncer, faults=faults)
    raise AssertionError(f"unknown backend {backend!r}")
