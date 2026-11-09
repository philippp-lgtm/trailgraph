"""Errors of sealed stored files, shared by both backends (plan T16)."""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.sealing import BlobInvalid, KeyUnavailable


def sealed_blob_error(digest: str, exc: KeyUnavailable | BlobInvalid) -> GraphCliError:
    """``backend.corrupt`` for a sealed blob that cannot be read; ``details.reason`` is
    ``shredded``, ``missing`` (its key) or ``invalid`` (the bytes)."""
    if isinstance(exc, KeyUnavailable):
        message = f"the key of a stored file is {exc.reason}; its content cannot be read"
        details: dict[str, JsonValue] = {"blob": digest, "kid": str(exc.kid), "reason": exc.reason}
    else:
        message = f"a stored file does not decrypt: {exc}"
        details = {"blob": digest, "reason": "invalid"}
    return GraphCliError("backend.corrupt", message, details=details)
