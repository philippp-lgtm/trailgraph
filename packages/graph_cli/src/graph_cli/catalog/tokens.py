"""Bearer tokens of the server API: the document, hashing and the store (contracts B14.11).

A token is ``gct_`` plus 32 random bytes in base64url. Only the SHA-256 of
the whole token is stored, by name, in ``tokens/tokens.json``
(``graph_cli.tokens/1``); audit events name a token by a 16-hex prefix of
that hash. A token's scope says which surface it reaches: ``app`` (the
default, the ``graph`` commands) or ``admin`` (the ``graph-admin`` commands
of the admin API). The file names the scope of admin tokens only, so a
server that predates scopes fails closed on it instead of taking an admin
token for an app token. An admin token bound to a project is a project
admin: it maintains the graphs the project's scope says ``maintain`` for.
``TokenStore`` is the server's lock-free reader: it parses the file again
only when its mtime, size or inode changed, and fails closed on an
unreadable or invalid file (``backend.unavailable``). A token is valid
through its ``expires`` date in UTC.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

from graph_cli.kernel.actor import is_valid_asserted_value
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files.token_files import read_tokens_file, tokens_path

TOKENS_SCHEMA: Final = "graph_cli.tokens/1"
TOKEN_PREFIX: Final = "gct_"
TOKEN_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS: Final = frozenset({"name", "hash", "project", "expires", "created_at", "scope"})
PREFIX_HEX: Final = 16
APP_SCOPE: Final = "app"
ADMIN_SCOPE: Final = "admin"
SCOPES: Final = (APP_SCOPE, ADMIN_SCOPE)


@dataclass(frozen=True, slots=True)
class TokenRecord:
    name: str
    hash: str  # sha256:<hex> of the whole token
    project: str | None = None  # bound project: the request's project facet is verified
    expires: str | None = None  # YYYY-MM-DD, valid through that day (UTC)
    created_at: str | None = None
    scope: str = APP_SCOPE  # the surface it reaches: app or admin

    @property
    def hash_prefix(self) -> str:
        return hash_prefix(self.hash)

    def valid_on(self, today: date) -> bool:
        return self.expires is None or today <= date.fromisoformat(self.expires)

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": self.name,
            "hash": self.hash,
            "project": self.project,
            "expires": self.expires,
            "created_at": self.created_at,
        }
        if self.scope != APP_SCOPE:
            data["scope"] = self.scope
        return data


def new_token() -> str:
    return TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def hash_prefix(digest: str) -> str:
    """What audit events and listings show of a token's hash."""
    return digest[: len("sha256:") + PREFIX_HEX]


def tokens_document(records: Sequence[TokenRecord]) -> dict[str, JsonValue]:
    return {
        "schema": TOKENS_SCHEMA,
        "tokens": [r.to_json() for r in sorted(records, key=lambda r: r.name)],
    }


def parse_tokens(data: bytes | None) -> tuple[TokenRecord, ...]:
    """The records of a token file; ``()`` without one. Defects are ``backend.unavailable``:
    the file is written only by maintenance, so a defect means damage."""
    if data is None:
        return ()
    try:
        document = parse_json(data)
    except CanonicalError as exc:
        raise _invalid(f"not JSON: {exc}") from None
    if not isinstance(document, Mapping) or document.get("schema") != TOKENS_SCHEMA:
        raise _invalid(f"not a {TOKENS_SCHEMA} document")
    entries = document.get("tokens")
    if not isinstance(entries, list):
        raise _invalid("tokens must be a list")
    records: list[TokenRecord] = []
    for index, entry in enumerate(entries):
        records.append(_record(entry, index))
    if len({r.name for r in records}) != len(records):
        raise _invalid("a token name is listed twice")
    return tuple(records)


def _record(entry: JsonValue, index: int) -> TokenRecord:
    if not isinstance(entry, Mapping) or set(entry) - _KEYS:
        raise _invalid(f"tokens[{index}] is not a token")
    name, digest = entry.get("name"), entry.get("hash")
    project, expires = entry.get("project"), entry.get("expires")
    created = entry.get("created_at")
    scope = entry.get("scope", APP_SCOPE)
    if not isinstance(name, str) or not TOKEN_NAME_RE.match(name):
        raise _invalid(f"tokens[{index}].name is not a token name")
    if not isinstance(digest, str) or not _HASH_RE.match(digest):
        raise _invalid(f"tokens[{index}].hash is not a sha256 hash")
    if project is not None and not (isinstance(project, str) and is_valid_asserted_value(project)):
        raise _invalid(f"tokens[{index}].project is not a project name")
    if expires is not None and not (isinstance(expires, str) and _is_date(expires)):
        raise _invalid(f"tokens[{index}].expires is not a date")
    if created is not None and not isinstance(created, str):
        raise _invalid(f"tokens[{index}].created_at is not a time")
    if scope not in SCOPES:
        raise _invalid(f"tokens[{index}].scope is not one of {', '.join(SCOPES)}")
    assert isinstance(scope, str)
    return TokenRecord(name, digest, project, expires, created, scope)


def _is_date(text: str) -> bool:
    if not _DATE_RE.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _invalid(why: str) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"the token file is invalid: {why}")


class TokenStore:
    """The server's view of ``tokens/tokens.json``; safe to share between threads."""

    def __init__(self, data_root: Path) -> None:
        self.path = tokens_path(data_root)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._by_hash: dict[str, TokenRecord] = {}

    def lookup(self, token: str, now: datetime) -> TokenRecord | None:
        """The record of a valid token; ``None`` for an unknown or expired one."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = token_hash(token)
        found = self._records().get(digest)
        return found if found is not None and found.valid_on(now.date()) else None

    def _records(self) -> dict[str, TokenRecord]:
        with self._lock:
            try:
                status = self.path.stat()
                stamp: tuple[int, int, int] | None = (
                    status.st_mtime_ns,
                    status.st_size,
                    status.st_ino,
                )
            except FileNotFoundError:
                stamp = None
            except OSError as exc:
                raise _invalid(exc.strerror or str(exc)) from exc
            if stamp != self._stamp or stamp is None:
                records = parse_tokens(read_tokens_file(self.path))
                self._by_hash = {r.hash: r for r in records}
                self._stamp = stamp
            return self._by_hash
