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

``cross_check`` matches write intents with the tx log (B5.6, B5.12) and
``check_anchors`` matches installation anchors with graph streams (B5.11).
``verify_graph`` and ``verify_installation`` compose them for
``graph-admin audit verify``. The caller passes the txs only when storage
verified the tx log clean, so a damaged log does not cascade into
``tx_without_intent`` findings.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from graph_cli.audit import finding_codes as codes
from graph_cli.audit.schema import AUDIT_SCHEMA, event_problems, is_write_intent, unknown_actions
from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    CanonicalError,
    JsonValue,
    hash_document,
    jsonl_line,
    parse_json,
)
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditLine, AuditStream, graph_stream
from graph_cli.kernel.records import TxRecord


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
        findings.append(_finding(code, target, message, expected=expected, actual=actual))

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


def cross_check(
    graph_id: GraphId, events: Iterable[Mapping[str, JsonValue]], txs: Iterable[TxRecord]
) -> list[Finding]:
    """Every tx has its ``ok`` write intent; every write intent has its tx or is benign."""
    stream = graph_stream(str(graph_id))
    intents = {event.get("event_id"): event for event in events if is_write_intent(event)}
    committed: set[JsonValue] = set()
    findings = []
    for tx in txs:
        target: dict[str, JsonValue] = {"graph_id": str(graph_id), "tx_seq": tx.seq}
        event = intents.get(str(tx.event_id))
        if event is None or str(tx.event_id) in committed:
            findings.append(
                _finding(codes.TX_WITHOUT_INTENT, target, f"no write intent {tx.event_id}")
            )
            continue
        committed.add(str(tx.event_id))
        change = event.get("change")
        if not isinstance(change, Mapping):
            change = {}  # a malformed intent: event_malformed already, and no match here
        recorded = {k: change.get(k) for k in ("tx_id", "tx_seq", "changeset_hash")}
        expected: dict[str, JsonValue] = {
            "tx_id": str(tx.tx_id),
            "tx_seq": tx.seq,
            "changeset_hash": tx.changeset_hash,
        }
        if recorded != expected:
            differing = sorted(k for k in expected if recorded[k] != expected[k])
            findings.append(
                _finding(
                    codes.TX_WITHOUT_INTENT,
                    target,
                    f"write intent {tx.event_id} differs in {', '.join(differing)}",
                    expected={k: expected[k] for k in differing},
                    actual={k: recorded[k] for k in differing},
                )
            )
    for event_id, event in intents.items():
        if event_id not in committed:
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "write intent without a committed tx (an interrupted write)",
                )
            )
    return findings


def check_anchors(
    installation_events: Iterable[Mapping[str, JsonValue]],
    graph_events: Mapping[str, Sequence[Mapping[str, JsonValue]]],
) -> list[Finding]:
    """Every anchor names an event that is still in its graph stream with that hash."""
    hashes = {
        graph_id: {event.get("seq"): event.get("hash") for event in events}
        for graph_id, events in graph_events.items()
    }
    findings = []
    for event in installation_events:
        anchors = event.get("anchors")
        if not isinstance(anchors, list):
            continue
        target: dict[str, JsonValue] = {"stream": INSTALLATION_STREAM, "seq": event.get("seq")}
        for anchor in anchors:
            if not isinstance(anchor, Mapping):
                continue  # a shape problem, reported as event_malformed
            graph_id, seq = anchor.get("graph_id"), anchor.get("seq")
            stream_hashes = hashes.get(str(graph_id))
            if stream_hashes is None:
                message = f"anchored graph stream {graph_id} is missing"
                actual: JsonValue = None
            elif seq not in stream_hashes:
                message = f"graph stream {graph_id} ends before the anchored seq {seq}"
                actual = None
            elif stream_hashes[seq] != anchor.get("hash"):
                message = f"graph stream {graph_id} event {seq} differs from its anchor"
                actual = stream_hashes[seq]
            else:
                continue
            findings.append(
                _finding(
                    codes.ANCHOR_MISMATCH,
                    target,
                    message,
                    expected=dict(anchor),
                    actual=actual,
                )
            )
    return findings


def verify_graph(
    lines: Iterable[AuditLine],
    *,
    graph_id: GraphId,
    txs: Iterable[TxRecord] | None,
    ext_actions: Iterable[str] = (),
) -> StreamReport:
    """One graph stream plus the tx cross-check (``txs=None`` skips it: damaged tx log)."""
    report = verify_stream(lines, stream=graph_stream(str(graph_id)), ext_actions=ext_actions)
    if txs is None:
        return report
    extra = cross_check(graph_id, report.events, txs)
    return dataclasses.replace(report, findings=(*report.findings, *extra))


def verify_installation(
    lines: Iterable[AuditLine],
    *,
    graphs: Mapping[str, StreamReport],
    ext_actions: Iterable[str] = (),
) -> StreamReport:
    """The installation stream plus its anchors into the given graph reports."""
    report = verify_stream(lines, stream=INSTALLATION_STREAM, ext_actions=ext_actions)
    extra = check_anchors(report.events, {g: r.events for g, r in graphs.items()})
    return dataclasses.replace(report, findings=(*report.findings, *extra))


def _finding(
    code: str,
    target: Mapping[str, JsonValue],
    message: str,
    *,
    expected: JsonValue | None = None,
    actual: JsonValue | None = None,
) -> Finding:
    return Finding(_SEVERITY[code], code, target, message, expected, actual)


_SEVERITY = {spec.code: spec.default_severity for spec in codes.FINDING_CODES}


def _parse(data: bytes) -> Mapping[str, JsonValue] | None:
    if not data.endswith(b"\n"):
        return None
    try:
        event = parse_json(data[:-1])
    except CanonicalError:
        return None
    return event if isinstance(event, Mapping) else None
