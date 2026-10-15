"""Storage ports: read, write session and maintenance (contracts section 2, B4, B13).

Payloads are kernel types only; no paths, SQL or backend types cross the port.
``GraphStoreMaintenance`` implementations are imported only by graph_cli_admin.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import GENESIS_HASH, JsonValue
from graph_cli.kernel.errors import EMPTY, WarningRecord
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import EntityId, GraphId, TermRef, TxId
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt
from graph_cli.kernel.ports.catalog import ManifestData, SkillSlot
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.records import EdgeRecord, Revision, RevisionMeta, TxRecord

CAPABILITY_SNAPSHOT_AT_SEQ = "snapshot_at_seq"


@dataclass(frozen=True, slots=True)
class Head:
    seq: int  # 0 = empty graph
    tx_id: TxId | None
    hash: str  # genesis hash when seq == 0
    recorded_at: datetime | None


EMPTY_HEAD = Head(seq=0, tx_id=None, hash=GENESIS_HASH, recorded_at=None)


@dataclass(frozen=True, slots=True)
class CommitResult:
    seq: int
    tx_id: TxId
    hash: str
    warnings: tuple[WarningRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered: bool
    details: Mapping[str, JsonValue] = field(default=EMPTY)


class Snapshot(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        """Notices about the read itself, e.g. ``storage.recovery_pending``."""
        ...

    def get(self, entity: EntityId) -> Revision | None: ...

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]: ...

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None: ...

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]: ...

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]: ...

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        """``(id, is_current_alias)``."""
        ...

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]: ...

    def query(self, q: Query) -> QueryPage:
        """``q.as_of`` must be None; services enforce it."""
        ...

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]: ...


class GraphStoreRead(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    def head(self) -> Head: ...

    def snapshot(self, *, at_seq: int | None = None) -> Snapshot:
        """Lock-free and isolated (B4.10)."""
        ...

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]: ...


class WriteSession(Protocol):
    """Exists only while the graph lock is held."""

    def snapshot(self) -> Snapshot: ...

    def head(self) -> Head: ...

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None: ...

    def alias_counters(self) -> Mapping[str, int]:
        """Per alias prefix, the highest number allocated so far (B1.8, B8.14)."""
        ...

    def append_audit(self, event: AuditEventDraft) -> AuditReceipt:
        """Durable on return."""
        ...

    def commit(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult: ...


class GraphStoreWrite(Protocol):
    def session(self, *, timeout_s: float) -> AbstractContextManager[WriteSession]: ...


class MaintenanceSession(WriteSession, Protocol):
    """A write session plus the maintenance effects (W.e); no recovery on open."""

    def manifest(self) -> ManifestData:
        """The manifest as it is now, read under the graph lock."""
        ...

    def release_hashes(self) -> Mapping[str, str]:
        """Installed release versions and the C.2 hash of their ``compiled.json``."""
        ...

    def read_release_file(self, version: str, name: str) -> bytes: ...

    def replace_manifest(self, manifest: ManifestData, receipt: AuditReceipt) -> None:
        """Atomic manifest switch; ``receipt`` is this session's intent (B5.7)."""
        ...

    def install_release(
        self, version: str, files: Mapping[str, bytes], receipt: AuditReceipt
    ) -> None:
        """Publish ``ontology/<version>/`` atomically; it never changes afterwards (B7.4)."""
        ...

    def read_skill(self) -> SkillSlot | None:
        """The graph's published skill as it is now (D15 Q2)."""
        ...

    def replace_skill(self, slot: SkillSlot, receipt: AuditReceipt) -> None:
        """Publish the graph skill atomically, replacing the previous one (D15 Q2)."""
        ...

    def recover(self) -> RecoveryReport: ...

    def verify(self) -> Sequence[Finding]:
        """``integrity.storage.*`` findings."""
        ...

    def reindex(self) -> None: ...


class RegistrySession(Protocol):
    """Holds the installation registry lock (W.a, W.f)."""

    def manifests(self) -> Sequence[ManifestData]:
        """Every graph's manifest, archived ones included."""
        ...

    def create_graph(
        self,
        manifest: ManifestData,
        genesis: AuditEventDraft,
        *,
        version: str,
        files: Mapping[str, bytes],
        announce: Callable[[AuditReceipt], AuditReceipt],
    ) -> AuditReceipt:
        """W.f: stage the graph with its genesis event and first release, call
        ``announce`` with the genesis receipt (it appends the installation event
        with the anchor), then publish the graph. Returns the genesis receipt."""
        ...


class GraphStoreMaintenance(Protocol):
    def graph_ids(self) -> Sequence[GraphId]:
        """Every published graph, in id order."""
        ...

    def registry(self, *, timeout_s: float) -> AbstractContextManager[RegistrySession]: ...

    def maintenance_session(
        self, graph: GraphId, *, timeout_s: float
    ) -> AbstractContextManager[MaintenanceSession]: ...
