"""Identifiers: UUIDv7 minting, canonical text form and reference grammars (B1, ID.1-ID.7).

Python 3.12 has no ``uuid.uuid7``, and tests need seeded determinism, so the
generator is our own (RFC 9562 section 6.2, method 1: fixed-length counter).
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, NewType, Protocol
from uuid import UUID

from graph_cli.kernel.clock import Clock, epoch_ms
from graph_cli.kernel.errors import GraphCliError

EntityId = NewType("EntityId", UUID)
GraphId = NewType("GraphId", UUID)
TxId = NewType("TxId", UUID)
EventId = NewType("EventId", UUID)
EdgeId = NewType("EdgeId", UUID)
TermRef = NewType("TermRef", str)  # CURIE "<prefix>:<Term>"

ALIAS_RE: Final = re.compile(r"^[A-Z][A-Z0-9]{0,7}-[0-9]{4,}$")
ALIAS_PREFIX_RE: Final = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
GRAPH_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")  # B1.11
TERM_REF_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,15}:[A-Za-z][A-Za-z0-9_]{0,63}$")
LOCAL_RE: Final = re.compile(r"^@[a-z][a-z0-9_]{0,63}$")
ABBREV_RE: Final = re.compile(r"^[0-9a-f]{8,32}$")
_UUID_TEXT_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_URN_PREFIX: Final = "urn:uuid:"

_COUNTER_MAX: Final = 0xFFF


@dataclass(frozen=True, slots=True)
class LocalRef:
    name: str  # without the leading "@"


@dataclass(frozen=True, slots=True)
class AliasRef:
    alias: str


@dataclass(frozen=True, slots=True)
class AbbrevRef:
    hex_suffix: str


EntityRef = EntityId | LocalRef | AliasRef | AbbrevRef


def parse_entity_ref(
    text: str, *, allow_local: bool = False, allow_abbrev: bool = False
) -> EntityRef | None:
    """Classify a reference by grammar (B1.9); ``None`` when nothing fits.

    The grammars are disjoint: full UUID (or ``urn:uuid:``), alias,
    ``@local`` (change sets only, stored without ``@``), abbreviation (8-32
    hex of the id's trailing random part; CLI arguments only). Callers choose
    the error code for ``None``.
    """
    if allow_local and LOCAL_RE.match(text):
        return LocalRef(text[1:])
    if ALIAS_RE.match(text):
        return AliasRef(text)
    try:
        return EntityId(parse_id(text))
    except GraphCliError:
        pass
    if allow_abbrev and ABBREV_RE.match(text.lower()):
        return AbbrevRef(text.lower())
    return None


def ref_text(ref: EntityRef) -> str:
    """The canonical text of a reference: full id, alias, ``@name`` or the hex suffix."""
    match ref:
        case LocalRef(name):
            return f"@{name}"
        case AliasRef(alias):
            return alias
        case AbbrevRef(suffix):
            return suffix
    return format_id(ref)


def parse_id(text: str) -> UUID:
    """Parse a UUID in canonical form, uppercase or ``urn:uuid:`` form (ID.5).

    Any RFC 9562 UUID is accepted for lookups; minted ids are checked with
    ``is_uuid7``.
    """
    candidate = text.strip().lower()
    candidate = candidate.removeprefix(_URN_PREFIX)
    if not _UUID_TEXT_RE.match(candidate):
        raise GraphCliError(
            "usage.invalid_argument",
            f"not a UUID: {text!r}",
            details={"expected": "8-4-4-4-12 hex digits", "actual": text},
        )
    value = UUID(candidate)
    if value.variant != "specified in RFC 4122":
        raise GraphCliError(
            "usage.invalid_argument",
            f"not an RFC 9562 UUID: {text!r}",
            details={"actual": text},
        )
    return value


def format_id(value: UUID) -> str:
    """Canonical text form: lowercase, hyphenated, 36 chars."""
    return str(value)


def is_uuid7(value: UUID) -> bool:
    return value.version == 7 and value.variant == "specified in RFC 4122"


def id_timestamp_ms(value: UUID) -> int:
    """The creation millisecond a UUIDv7 discloses (ID.7)."""
    return value.int >> 80


class IdProvider(Protocol):
    def new_id(self) -> UUID:
        """A fresh UUIDv7, strictly greater than every id this provider minted before."""
        ...


class Uuid7Provider:
    """Production UUIDv7 generator (ID.1-ID.3). One per process, thread-safe."""

    def __init__(self, clock: Clock, randbits: Callable[[int], int] = secrets.randbits) -> None:
        self._clock = clock
        self._randbits = randbits
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def new_id(self) -> UUID:
        with self._lock:
            now_ms = epoch_ms(self._clock.now())
            if now_ms > self._last_ms:
                counter = self._randbits(11)  # top bit clear leaves headroom
            else:  # same millisecond, or the clock moved back
                now_ms = self._last_ms
                counter = self._counter + 1
            if counter > _COUNTER_MAX:
                now_ms = self._last_ms + 1
                counter = self._randbits(11)
            self._last_ms, self._counter = now_ms, counter
            rand_b = self._randbits(62)
        value = (now_ms << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | rand_b
        return UUID(int=value)
