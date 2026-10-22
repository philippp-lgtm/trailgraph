"""Chained JSONL documents: sealing and verification (contracts C.2, C.3, B4.5, B4.11, B5.3).

Tx logs and audit streams share one line format: the JCS bytes of a document
that carries ``seq``, ``prev_hash`` and ``hash``, followed by ``\\n``. ``seq``
starts at 1 and is contiguous; ``hash`` is C.2 over the document without
``hash``; ``prev_hash`` is the previous line's hash, or the genesis hash.

Damage is classified by position (B4.11):

- a **torn** tail (last line without ``\\n``) or an **invalid** last line
  (not canonical JSON, hash mismatch) is "not committed": readers ignore it
  and the next writer truncates it;
- anything else is **corruption**: an invalid interior line, or a well-formed
  line that does not continue the chain (even as the last line, because
  truncating it could drop committed data).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    CanonicalError,
    JsonObject,
    JsonValue,
    hash_canonical_document,
    hash_document,
    jsonl_line,
    parse_canonical,
)

CHAIN_KEYS: Final = ("seq", "prev_hash", "hash")

TailState = Literal["clean", "torn", "invalid"]


@dataclass(frozen=True, slots=True)
class ChainLink:
    """A position in a chain: the last line's ``seq`` and ``hash``."""

    seq: int
    hash: str


GENESIS: Final = ChainLink(0, GENESIS_HASH)


class ChainError(ValueError):
    """A line is torn, invalid or does not continue the chain."""

    def __init__(self, kind: Literal["torn", "invalid", "broken"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


def seal(body: JsonObject, prev: ChainLink) -> tuple[dict[str, JsonValue], bytes]:
    """Chain ``body`` after ``prev``; return the sealed document and its JSONL line."""
    clash = [key for key in CHAIN_KEYS if key in body]
    if clash:
        raise ValueError(f"body already carries chain keys: {clash}")
    document: dict[str, JsonValue] = {**body, "seq": prev.seq + 1, "prev_hash": prev.hash}
    document["hash"] = hash_document(document)
    return document, jsonl_line(document)


def link_of(document: JsonObject) -> ChainLink:
    """The chain position of a verified document."""
    seq, digest = document["seq"], document["hash"]
    assert isinstance(seq, int) and isinstance(digest, str)
    return ChainLink(seq, digest)


def verify_line(line: bytes, prev: ChainLink) -> JsonObject:
    """Parse one line and check canonical bytes, hash and continuity after ``prev``."""
    if not line.endswith(b"\n"):
        raise ChainError("torn", "line has no terminating newline")
    body = line[:-1]
    try:
        document = parse_canonical(body)
    except CanonicalError as exc:
        raise ChainError("invalid", f"not canonical JSON: {exc}") from None
    if not isinstance(document, Mapping):
        raise ChainError("invalid", "line is not a JSON object")
    seq, prev_hash, digest = (document.get(key) for key in CHAIN_KEYS)
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ChainError("invalid", "seq is missing or not an integer")
    if not isinstance(prev_hash, str) or not isinstance(digest, str):
        raise ChainError("invalid", "prev_hash or hash is missing")
    if hash_canonical_document(body, document) != digest:
        raise ChainError("invalid", "hash does not match the document")
    if seq != prev.seq + 1:
        raise ChainError("broken", f"seq {seq} does not follow {prev.seq}")
    if prev_hash != prev.hash:
        raise ChainError("broken", f"prev_hash of seq {seq} does not match the previous hash")
    return document


@dataclass(frozen=True, slots=True)
class ScannedLine:
    document: JsonObject
    start: int  # byte offset of the line
    end: int  # byte offset after its newline


@dataclass(frozen=True, slots=True)
class Corruption:
    offset: int  # byte offset of the damaged line
    expected_seq: int
    reason: str


@dataclass(frozen=True, slots=True)
class ChainScan:
    lines: tuple[ScannedLine, ...]
    end: ChainLink  # position after the last good line
    good_end: int  # byte offset after the last good line
    tail: TailState  # what follows ``good_end`` when there is no corruption
    corruption: Corruption | None


def scan(data: bytes, start: ChainLink = GENESIS, *, final: bool = True) -> ChainScan:
    """Verify consecutive lines of ``data`` continuing ``start``.

    ``final`` says whether ``data`` ends the log. A torn or invalid last line
    is a tail only at the very end of the log; anywhere else it is corruption.
    """
    lines: list[ScannedLine] = []
    link, offset = start, 0
    while offset < len(data):
        newline = data.find(b"\n", offset)
        end = len(data) if newline < 0 else newline + 1
        try:
            document = verify_line(data[offset:end], link)
        except ChainError as exc:
            is_last = end == len(data)
            if final and is_last and exc.kind in ("torn", "invalid"):
                tail: TailState = "torn" if exc.kind == "torn" else "invalid"
                return ChainScan(tuple(lines), link, offset, tail, None)
            damage = Corruption(offset, link.seq + 1, str(exc))
            return ChainScan(tuple(lines), link, offset, "clean", damage)
        lines.append(ScannedLine(document, offset, end))
        link, offset = link_of(document), end
    return ChainScan(tuple(lines), link, offset, "clean", None)
