"""Installed policy files (contracts B6.14, section 5 ``policy/``, ``graphs/<id>/policy.json``).

Each holds one compiled ``graph_cli.policy/1`` document (C.4). Storage
treats it as an opaque JSON object: access control checks its meaning. A
file is replaced with one rename, its commit point, so a reader sees the
old or the new document. Readers are lock-free; the writer holds the
registry lock (installation) or the graph lock (graph) and has appended the
intent first (B5.15).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonValue, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files.durable import Syncer, ensure_dir, write_temp

POLICY_DIR: Final = "policy"
INSTALLATION_POLICY: Final = "installation.json"


def installation_policy_path(data_root: Path) -> Path:
    return data_root / POLICY_DIR / INSTALLATION_POLICY


def read_policy_file(path: Path) -> bytes | None:
    """The file's bytes, ``None`` when no policy is installed there."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"cannot read the policy {path.name}: {exc.strerror or exc}"
        ) from exc


def write_policy_file(
    path: Path, document: Mapping[str, JsonValue], syncer: Syncer, faults: FaultHook | None
) -> None:
    """Replace the policy at ``path`` durably: temp file, sync, rename, sync the directory."""
    try:
        ensure_dir(path.parent, syncer)
        temp = write_temp(path, pretty_bytes(dict(document)), syncer)
        fire(faults, CrashPoint.POLICY_BEFORE_RENAME)
        os.replace(temp, path)
        syncer.sync_dir(path.parent)
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"cannot install the policy: {exc.strerror or exc}"
        ) from exc
