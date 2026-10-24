"""Storage ports: read, write session and maintenance (contracts section 2, B4, B13).

Payloads are kernel types only; no paths, SQL or backend types cross the port.
The one exception is ``blob_local_path`` behind the capability of the same
name (B13.2): a local backend hands out the read-only path of a stored file.
``GraphStoreMaintenance`` implementations are imported only by graph_cli_admin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import GENESIS_HASH, JsonValue
from graph_cli.kernel.errors import EMPTY, WarningRecord
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import EntityId, GraphId, TermRef, TxId
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt
from graph_cli.kernel.ports.catalog import ManifestData, SkillSlot
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.records import BlobInfo, EdgeRecord, Revision, RevisionMeta, TxRecord

CAPABILITY_SNAPSHOT_AT_SEQ = "snapshot_at_seq"
CAPABILITY_BLOB_LOCAL_PATH = "blob_local_path"


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

    def tx(self, seq: int) -> TxRecord | None:
        """The committed tx with this seq; ``None`` outside ``1..self.seq``."""
        ...

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


class StagedBlob(Protocol):
    """Bytes hashed and held by a backend before the lock; published by a write session.

    Opaque to callers: a synced staging file (FileStore) or bytes (memory).
    """

    @property
    def hash(self) -> str: ...

    @property
    def size(self) -> int: ...

    def discard(self) -> None:
        """Drop the staged bytes; idempotent, and a no-op after a publish."""
        ...


class GraphStoreRead(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    def head(self) -> Head: ...

    def snapshot(self, *, at_seq: int | None = None) -> Snapshot:
        """Lock-free and isolated (B4.10)."""
        ...

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]: ...

    def blob(self, digest: str) -> BlobInfo | None:
        """The stored blob with this hash, lock-free; ``None`` when absent."""
        ...

    def read_blob(self, digest: str) -> Iterator[bytes]:
        """The stored bytes in chunks; ``backend.corrupt`` when the blob is absent."""
        ...

    def blob_local_path(self, digest: str) -> Path | None:
        """Capability ``blob_local_path``: the read-only file of a stored blob."""
        ...


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

    def publish_blob(self, staged: StagedBlob) -> BlobInfo:
        """Store a staged blob under its hash, durable on return (W.11b).

        A blob already stored is kept as it is; the staged copy is discarded.
        """
        ...


class GraphStoreWrite(Protocol):
    def session(self, *, timeout_s: float) -> AbstractContextManager[WriteSession]: ...

    def stage_blob(self, chunks: Iterable[bytes], *, max_bytes: int) -> StagedBlob:
        """Hash and hold ``chunks`` without the lock; ``file.too_large`` beyond ``max_bytes``."""
        ...


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

    def read_policy(self) -> bytes | None:
        """The graph's installed policy file as stored; ``None`` without one (B6.14)."""
        ...

    def replace_policy(self, document: Mapping[str, JsonValue], receipt: AuditReceipt) -> None:
        """Install the graph's compiled policy atomically (W.e, B5.15)."""
        ...

    def recover(self) -> RecoveryReport: ...

    def verify(self) -> Sequence[Finding]:
        """``integrity.storage.*`` findings."""
        ...

    def reindex(self, receipt: AuditReceipt | None = None) -> None:
        """Rebuild every derived file from the tx log; ``receipt`` is this session's intent."""
        ...

    def blobs(self) -> Iterator[BlobInfo]:
        """Every stored blob, in hash order."""
        ...

    def orphan_blobs(self) -> Sequence[BlobInfo]:
        """Stored blobs no tx names; ``backend.corrupt`` when the tx log is damaged."""
        ...

    def staging(self) -> Iterator[tuple[str, datetime]]:
        """Blob staging leftovers: ``(name, last modified)``."""
        ...

    def verify_blobs(self, *, rehash: bool) -> Sequence[Finding]:
        """``integrity.storage.blob_*`` findings; ``rehash`` reads every stored blob."""
        ...

    def remove_blobs(
        self, hashes: Sequence[str], staging: Sequence[str], receipt: AuditReceipt
    ) -> None:
        """Delete unreferenced blobs and staging leftovers; ``receipt`` is the intent (W.e)."""
        ...


class RegistrySession(Protocol):
    """Holds the installation registry lock (W.a, W.f)."""

    def manifests(self) -> Sequence[ManifestData]:
        """Every graph's manifest, archived ones included."""
        ...

    def read_tokens(self) -> bytes | None:
        """The token file of the server API as stored, under the registry lock (B14.11)."""
        ...

    def replace_tokens(self, document: Mapping[str, JsonValue], receipt: AuditReceipt) -> None:
        """Replace the token file; ``receipt`` is its installation event (B14.11)."""
        ...

    def read_installation_policy(self) -> bytes | None:
        """The installed installation policy as stored; ``None`` without one (B6.14)."""
        ...

    def replace_installation_policy(
        self, document: Mapping[str, JsonValue], receipt: AuditReceipt
    ) -> None:
        """Install the compiled installation policy atomically after its event (B5.15)."""
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
