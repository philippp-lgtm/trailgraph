"""The token file ``tokens/tokens.json`` (contracts B14.11, section 5 ``tokens/``).

It holds one ``graph_cli.tokens/1`` document (C.4): the bearer tokens of the
server API, each by name with the SHA-256 of its secret, never the secret.
Storage treats the document as an opaque JSON object; ``catalog.tokens``
checks its meaning. The file is replaced with one rename, its commit point,
so a reader sees the old or the new document. Readers are lock-free; the
writer holds the registry lock and has appended its installation event first.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonValue, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files.durable import Syncer, ensure_dir, write_temp

TOKENS_DIR: Final = "tokens"
TOKENS_FILE: Final = "tokens.json"


def tokens_path(data_root: Path) -> Path:
    return data_root / TOKENS_DIR / TOKENS_FILE


def read_tokens_file(path: Path) -> bytes | None:
    """The file's bytes, ``None`` when no token was ever created.

    Only a missing file means "no tokens"; anything else in the way is an
    error, so a broken layout fails closed.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"cannot read the token file: {exc.strerror or exc}"
        ) from exc


def write_tokens_file(path: Path, document: Mapping[str, JsonValue], syncer: Syncer) -> None:
    """Replace the token file durably: temp file (0600), sync, rename, sync the directory."""
    try:
        ensure_dir(path.parent, syncer)
        temp = write_temp(path, pretty_bytes(dict(document)), syncer)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        syncer.sync_dir(path.parent)
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"cannot write the token file: {exc.strerror or exc}"
        ) from exc
