"""Enrollment codes: one-time codes a client exchanges for a token (contracts B14.21).

A maintainer creates a code for a project and a token scope (``graph-admin
enroll create``); whoever holds it exchanges it once at ``POST /v1/enroll``
for a token bound to that project (``graph connect``), so the maintainer's
own token never travels to a client. A code is ``gce_`` plus 26 base32
characters (128 random bits), shown once. ``tokens/enrollments.json``
(``graph_cli.enrollments/1``) keeps the codes not used yet, each by a short
id with the SHA-256 of the code, its project, scope and expiry, and the roles
its token gets when there are any (a code without roles has no key, as before);
a used or revoked code leaves the file. ``find`` compares a code's hash with every
listed one in constant time. Defects of the file are ``backend.unavailable``:
only maintenance writes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from graph_cli.catalog.tokens import SCOPES, TOKEN_NAME_RE, hash_prefix, parse_roles
from graph_cli.kernel.actor import MAX_ROLES, is_valid_asserted_value
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.clock import parse_timestamp
from graph_cli.kernel.errors import GraphCliError

ENROLLMENTS_SCHEMA: Final = "graph_cli.enrollments/1"
CODE_PREFIX: Final = "gce_"
CODE_RE: Final = re.compile(r"^gce_[a-z2-7]{26}\Z")
ID_RE: Final = re.compile(r"^[0-9a-f]{12}\Z")
_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_KEYS: Final = frozenset({"id", "hash", "project", "scope", "expires_at", "created_at"})
_OPTIONAL_KEYS: Final = frozenset({"roles"})
_DURATION_RE: Final = re.compile(r"^([1-9][0-9]{0,4})([mh])\Z")
DEFAULT_LIFETIME: Final = timedelta(minutes=15)
MAX_LIFETIME: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class EnrollmentRecord:
    id: str  # 12 hex: names the code in listings, revocations and events
    hash: str  # sha256:<hex> of the whole code
    project: str  # the project the token is bound to
    scope: str  # the token's scope: app or admin
    expires_at: str  # RFC 3339 UTC; the code is refused from then on
    created_at: str
    roles: tuple[str, ...] = ()  # sorted, unique: the roles of the token the code yields

    @property
    def hash_prefix(self) -> str:
        return hash_prefix(self.hash)

    def valid_at(self, now: datetime) -> bool:
        return now < parse_timestamp(self.expires_at)

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "id": self.id,
            "hash": self.hash,
            "project": self.project,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }
        if self.roles:
            data["roles"] = list(self.roles)
        return data


def new_code() -> str:
    return CODE_PREFIX + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def new_id() -> str:
    return secrets.token_hex(6)


def code_hash(code: str) -> str:
    return "sha256:" + hashlib.sha256(code.encode()).hexdigest()


def is_code(text: str) -> bool:
    return CODE_RE.match(text) is not None


def find(records: Sequence[EnrollmentRecord], code: str) -> EnrollmentRecord | None:
    """The record of ``code``, compared with every record in constant time; ``None`` for
    a code that is malformed or not listed. Expiry is the caller's check."""
    digest = code_hash(code if is_code(code) else "")
    found = None
    for record in records:
        if hmac.compare_digest(record.hash, digest):
            found = record
    return found if is_code(code) else None


def parse_lifetime(text: str) -> timedelta | None:
    """``<n>m`` or ``<n>h``, more than nothing and at most a day; ``None`` otherwise."""
    match = _DURATION_RE.match(text)
    if match is None:
        return None
    count, unit = int(match.group(1)), match.group(2)
    lifetime = timedelta(minutes=count) if unit == "m" else timedelta(hours=count)
    return lifetime if lifetime <= MAX_LIFETIME else None


def default_token_name(project: str, label: str) -> str:
    """``<project>.<label>`` made a token name: lowercase, other characters ``-``, at
    most 64 characters; ``<project>.client`` when the label leaves nothing."""

    def slug(text: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")

    base = slug(project)[:40] or "project"
    name = f"{base}.{slug(label)[:23].strip('-_') or 'client'}"
    return name if TOKEN_NAME_RE.match(name) else f"{base}.client"


def host_label() -> str:
    """This machine's host name without its domain: the default label of its token."""
    return socket.gethostname().split(".")[0]


def enrollments_document(records: Sequence[EnrollmentRecord]) -> dict[str, JsonValue]:
    return {
        "schema": ENROLLMENTS_SCHEMA,
        "enrollments": [r.to_json() for r in sorted(records, key=lambda r: r.id)],
    }


def parse_enrollments(data: bytes | None) -> tuple[EnrollmentRecord, ...]:
    """The records of an enrollment file; ``()`` without one."""
    if data is None:
        return ()
    try:
        document = parse_json(data)
    except CanonicalError as exc:
        raise _invalid(f"not JSON: {exc}") from None
    if not isinstance(document, Mapping) or document.get("schema") != ENROLLMENTS_SCHEMA:
        raise _invalid(f"not a {ENROLLMENTS_SCHEMA} document")
    entries = document.get("enrollments")
    if not isinstance(entries, list):
        raise _invalid("enrollments must be a list")
    records = tuple(_record(entry, index) for index, entry in enumerate(entries))
    if len({r.id for r in records}) != len(records):
        raise _invalid("an enrollment id is listed twice")
    return records


def _record(entry: JsonValue, index: int) -> EnrollmentRecord:
    if not isinstance(entry, Mapping) or not _KEYS <= set(entry) <= _KEYS | _OPTIONAL_KEYS:
        raise _invalid(f"enrollments[{index}] is not an enrollment")
    values = {key: entry[key] for key in _KEYS}
    if not all(isinstance(value, str) for value in values.values()):
        raise _invalid(f"enrollments[{index}] has a value that is not a string")
    text = {key: str(value) for key, value in values.items()}
    if not ID_RE.match(text["id"]):
        raise _invalid(f"enrollments[{index}].id is not an enrollment id")
    if not _HASH_RE.match(text["hash"]):
        raise _invalid(f"enrollments[{index}].hash is not a sha256 hash")
    if not is_valid_asserted_value(text["project"]):
        raise _invalid(f"enrollments[{index}].project is not a project name")
    if text["scope"] not in SCOPES:
        raise _invalid(f"enrollments[{index}].scope is not one of {', '.join(SCOPES)}")
    for key in ("expires_at", "created_at"):
        try:
            parse_timestamp(text[key])
        except (GraphCliError, ValueError):
            raise _invalid(f"enrollments[{index}].{key} is not a time") from None
    roles: tuple[str, ...] = ()
    if "roles" in entry:
        found = parse_roles(entry["roles"])
        if found is None:
            raise _invalid(
                f"enrollments[{index}].roles is not a sorted list of at most {MAX_ROLES} role names"
            )
        roles = found
    return EnrollmentRecord(
        text["id"],
        text["hash"],
        text["project"],
        text["scope"],
        text["expires_at"],
        text["created_at"],
        roles,
    )


def _invalid(why: str) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"the enrollment file is invalid: {why}")
