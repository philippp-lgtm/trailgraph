"""FileStore paths (contracts section 5, L.1-L.3). Private to the adapter."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.ids import EntityId
from graph_cli.storage.files.durable import FILE_MODE, Syncer, ensure_dir

SEGMENT_DIGITS: Final = 12
TX_SEGMENT_PREFIX: Final = ""
AUDIT_SEGMENT_PREFIX: Final = "events-"
DEFAULT_SEGMENT_BYTES: Final = 64 * 1024 * 1024  # B4 open point: roll at 64 MiB
REVISION_FILE_RE: Final = re.compile(r"^r([0-9]{6,})\.json$")
STAGING_PREFIX: Final = ".tmp-"  # graphs/.tmp-<id>, ontology/.tmp-<version>; readers ignore them


def segment_name(prefix: str, first_seq: int) -> str:
    return f"{prefix}{first_seq:0{SEGMENT_DIGITS}d}.jsonl"


def parse_segment_name(prefix: str, name: str) -> int | None:
    """The first seq of a segment file, or ``None`` for anything else (temp files, strays)."""
    match = re.fullmatch(rf"{re.escape(prefix)}([0-9]{{{SEGMENT_DIGITS},}})\.jsonl", name)
    return int(match.group(1)) if match else None


def revision_file_name(rev: int) -> str:
    return f"r{rev:06d}.json"


@dataclass(frozen=True, slots=True)
class GraphLayout:
    """``<data_root>/graphs/<graph_id>/`` and everything below it."""

    root: Path

    @property
    def lock(self) -> Path:
        return self.root / ".lock"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.toml"

    @property
    def ontology(self) -> Path:
        return self.root / "ontology"

    def release_dir(self, version: str) -> Path:
        return self.ontology / version

    @property
    def policy(self) -> Path:
        """The graph's own installed policy (B6.14)."""
        return self.root / "policy.json"

    @property
    def skill(self) -> Path:
        """The published graph skill (D15 Q2): ``source.json`` and ``files/<hash hex>/``."""
        return self.root / "skill"

    @property
    def skill_source(self) -> Path:
        return self.skill / "source.json"

    def skill_files(self, digest: str) -> Path:
        return self.skill / "files" / digest.removeprefix("sha256:")

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def head(self) -> Path:
        return self.data / "head.json"

    @property
    def txlog(self) -> Path:
        return self.data / "txlog"

    @property
    def entities(self) -> Path:
        return self.data / "entities"

    @property
    def audit(self) -> Path:
        return self.root / "audit"

    @property
    def blobs(self) -> Path:
        """Stored file bytes by hash; created on first use, never reserved empty."""
        return self.root / "blobs"

    def blob_path(self, digest: str) -> Path:
        """``blobs/sha256/<first 2 hex>/<64 hex>`` for a C.2 hash string."""
        algorithm, _, hex_digest = digest.partition(":")
        return self.blobs / algorithm / hex_digest[:2] / hex_digest

    def entity_dir(self, entity: EntityId) -> Path:
        """Sharded by the last two hex chars, which are random (L.1)."""
        return self.entities / entity.hex[-2:] / str(entity)

    def revision_path(self, entity: EntityId, rev: int) -> Path:
        return self.entity_dir(entity) / revision_file_name(rev)


def init_graph_dir(root: Path, syncer: Syncer) -> GraphLayout:
    """Lay out an empty graph: ``data/txlog``, ``data/entities``, ``audit/``, ``.lock``.

    Used by ``create_graph`` on the staging directory and by tests. Idempotent.
    """
    layout = GraphLayout(root)
    for directory in (layout.txlog, layout.entities, layout.audit):
        ensure_dir(directory, syncer)
    if not layout.lock.exists():
        os.close(os.open(layout.lock, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, FILE_MODE))
        syncer.sync_dir(root)
    return layout
