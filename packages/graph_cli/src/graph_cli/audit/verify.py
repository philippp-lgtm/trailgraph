"""Audit stream verification (contracts B5.3, B5.9, B5.12, V.5).

Pure: input are the raw lines of ``AuditSink.read_lines``. Damage is reported
as ``integrity.audit.*`` findings and verification goes on, like ``fsck``.
Every damaged line yields one chain finding at most, so one tampered line
does not cascade into findings for every later line:

- a torn or unparseable **last** line is a tail and no finding (B5.9);
- an unparseable interior line is ``torn_interior_line``; the next line may
  skip one seq and its ``prev_hash`` is not checked;
- ``seq_gap`` wins over ``chain_broken``; after either, the chain continues
  from the line as stored;
- ``hash_mismatch`` (content edited) is independent of the chain position;
  so are ``unknown_schema``, ``event_malformed`` and ``unknown_action``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from graph_cli.audit import finding_codes as codes
from graph_cli.audit.schema import AUDIT_SCHEMA, event_problems, unknown_actions
from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    CanonicalError,
    JsonValue,
    hash_document,
    jsonl_line,
    parse_json,
)
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ports.audit import AuditLine, AuditStream


@dataclass(frozen=True, slots=True)
class StreamReport:
    stream: AuditStream
    findings: tuple[Finding, ...]
    events: tuple[Mapping[str, JsonValue], ...]  # every parseable event, in stream order
    head: tuple[int, str]  # seq and hash of the last event as stored


def verify_stream(
    lines: Iterable[AuditLine], *, stream: AuditStream, ext_actions: Iterable[str] = ()
) -> StreamReport:
    known_ext = tuple(ext_actions)
    findings: list[Finding] = []
    events: list[Mapping[str, JsonValue]] = []
    last_seq, last_hash = 0, GENESIS_HASH
    after_torn = False
    items = list(lines)

    def add(
        code: str,
        target: Mapping[str, JsonValue],
        message: str,
        *,
        expected: JsonValue | None = None,
        actual: JsonValue | None = None,
    ) -> None:
        findings.append(Finding(_SEVERITY[code], code, target, message, expected, actual))

    for index, line in enumerate(items):
        event = _parse(line.data)
        if event is None:
            if index == len(items) - 1:
                break  # torn or unreadable tail: readers ignore it, the next writer truncates
            add(
                codes.TORN_INTERIOR_LINE,
                {"stream": stream, "position": line.position},
                "line is not a complete JSON object",
            )
            after_torn = True
            continue
        seq = event.get("seq")
        expected_seq = last_seq + (2 if after_torn else 1)
        target: dict[str, JsonValue] = {"stream": stream, "position": line.position}
        if isinstance(seq, int) and not isinstance(seq, bool):
            target = {"stream": stream, "seq": seq}
            next_seq = seq
        else:
            next_seq = expected_seq
        if seq != expected_seq or "position" in target:
            add(
                codes.SEQ_GAP,
                target,
                "seq does not follow the previous event",
                expected=expected_seq,
                actual=seq,
            )
        elif not after_torn and event.get("prev_hash") != last_hash:
            add(
                codes.CHAIN_BROKEN,
                target,
                "prev_hash is not the previous event's hash",
                expected=last_hash,
                actual=event.get("prev_hash"),
            )
        elif event.get("stream") != stream:
            add(
                codes.CHAIN_BROKEN,
                target,
                "event belongs to another stream",
                expected=stream,
                actual=event.get("stream"),
            )
        stored_hash = event.get("hash")
        if hash_document(event) != stored_hash:
            add(codes.HASH_MISMATCH, target, "event content does not match its hash")
        elif jsonl_line(event) != line.data:
            add(codes.EVENT_MALFORMED, target, "line is not canonical JSON")
        if event.get("schema") != AUDIT_SCHEMA:
            add(codes.UNKNOWN_SCHEMA, target, "unknown event schema", actual=event.get("schema"))
        else:
            problems = event_problems(event)
            if problems:
                add(codes.EVENT_MALFORMED, target, "; ".join(problems))
            unknown = unknown_actions(event, known_ext)
            if unknown:
                add(codes.UNKNOWN_ACTION, target, f"unknown actions: {', '.join(unknown)}")
        last_seq = next_seq
        last_hash = stored_hash if isinstance(stored_hash, str) else ""
        after_torn = False
        events.append(event)
    return StreamReport(stream, tuple(findings), tuple(events), (last_seq, last_hash))


_SEVERITY = {spec.code: spec.default_severity for spec in codes.FINDING_CODES}


def _parse(data: bytes) -> Mapping[str, JsonValue] | None:
    if not data.endswith(b"\n"):
        return None
    try:
        event = parse_json(data[:-1])
    except CanonicalError:
        return None
    return event if isinstance(event, Mapping) else None
