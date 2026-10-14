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

``cross_check`` matches write intents with the tx log (B5.6, B5.12),
``check_maintenance`` judges maintenance intents by their effects and walks
the pin events against the manifest, and ``check_anchors`` matches
installation anchors with graph streams (B5.11).
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
from graph_cli.audit.schema import (
    AUDIT_SCHEMA,
    MAINT_EFFECT_ACTIONS,
    PIN_ACTION,
    RELEASE_ACTION,
    event_problems,
    is_write_intent,
    unknown_actions,
)
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
from graph_cli.kernel.records import OntologyPin, TxRecord


@dataclass(frozen=True, slots=True)
class MaintEffects:
    """The maintenance effects found on disk, for judging maintenance intents (B5.6).

    ``manifest_pin`` is ``None`` when it cannot be trusted (storage reported
    ``integrity.storage.pin_mismatch``); the pin walk is then skipped.
    ``anchored`` are the graph-stream seqs named by companion anchors
    (``effect_anchors``): a companion is appended only after its effect, so
    an anchored intent is committed (W.e).
    """

    manifest_pin: OntologyPin | None
    releases: Mapping[str, str]  # installed version -> artifact hash of its compiled.json
    anchored: frozenset[int] = frozenset()


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
    """Every tx has its ``ok`` write intent; every write intent has its tx or is benign.

    Each intent serves one tx. A retried request may repeat an event id; the
    tx then takes the latest unused intent and the earlier one stays an orphan.
    """
    stream = graph_stream(str(graph_id))
    intents = [event for event in events if is_write_intent(event)]
    by_id: dict[JsonValue, list[int]] = {}
    for index, event in enumerate(intents):
        by_id.setdefault(event.get("event_id"), []).append(index)
    used: set[int] = set()
    findings = []
    for tx in txs:
        target: dict[str, JsonValue] = {"graph_id": str(graph_id), "tx_seq": tx.seq}
        free = [i for i in by_id.get(str(tx.event_id), ()) if i not in used]
        if not free:
            findings.append(
                _finding(codes.TX_WITHOUT_INTENT, target, f"no write intent {tx.event_id}")
            )
            continue
        used.add(free[-1])
        event = intents[free[-1]]
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
    for index, event in enumerate(intents):
        if index not in used:
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "write intent without a committed tx (an interrupted write)",
                )
            )
    return findings


def check_maintenance(
    graph_id: GraphId, events: Iterable[Mapping[str, JsonValue]], effects: MaintEffects
) -> list[Finding]:
    """Release intents need their release, pin intents their manifest (B5.6, B5.12).

    Pin intents record the manifest pin before (``from``, read under the
    lock) and after (``to``). Walking them from the genesis pin, an intent
    is committed if the next observed pin (the next intent's ``from``, at
    the end the manifest) equals its ``to``; it is an interrupted op if that
    pin is still its ``from`` and no anchor proves the effect; anything else
    means the manifest changed outside maintenance (``pin_mismatch``, after
    which no pin intent is judged: no cascades).
    """
    stream = graph_stream(str(graph_id))
    ok = [e for e in events if _ok(e)]
    findings = []
    for event, release in _effects(ok, RELEASE_ACTION, "release"):
        if event.get("seq") in effects.anchored:
            continue
        if effects.releases.get(str(release.get("version"))) != release.get("artifact_hash"):
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "release intent without its installed release (an interrupted release)",
                )
            )
    if effects.manifest_pin is not None:
        findings += _pin_walk(stream, ok, effects)
    return findings


def _ok(event: Mapping[str, JsonValue]) -> bool:
    result = event.get("result")
    return isinstance(result, Mapping) and result.get("status") == "ok"


def _effects(
    events: Iterable[Mapping[str, JsonValue]], action: str, key: str
) -> list[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]]:
    """Intents carrying ``action`` with a well-formed ``change.<key>``; malformed ones are
    ``event_malformed`` already and not judged again."""
    found = []
    for event in events:
        actions, change = event.get("actions"), event.get("change")
        if not (isinstance(actions, list) and action in actions and isinstance(change, Mapping)):
            continue
        effect = change.get(key)
        if isinstance(effect, Mapping):
            found.append((event, effect))
    return found


def _pin_walk(
    stream: AuditStream, events: Sequence[Mapping[str, JsonValue]], effects: MaintEffects
) -> list[Finding]:
    assert effects.manifest_pin is not None
    state: JsonValue = None
    for event in events:
        if event.get("seq") == 1 and event.get("actions") == ["maint.graph_create"]:
            graph = event.get("graph")
            state = graph.get("ontology") if isinstance(graph, Mapping) else None
    orphans: list[Finding] = []
    pending: tuple[Mapping[str, JsonValue], JsonValue, JsonValue] | None = None

    def mismatch(
        target: Mapping[str, JsonValue], expected: JsonValue, actual: JsonValue
    ) -> list[Finding]:
        return [
            _finding(
                codes.PIN_MISMATCH,
                target,
                "the manifest pin changed outside the recorded pin events",
                expected=expected,
                actual=actual,
            )
        ]

    def settle(observed: JsonValue) -> bool:
        """Judge ``pending`` by the pin observed after it; False on a mismatch."""
        nonlocal state
        if pending is None:
            return state is None or observed == state
        event, old, new = pending
        if observed == new:
            state = new
            return True
        if observed == old and event.get("seq") not in effects.anchored:
            orphans.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "pin intent without its manifest switch (an interrupted pin)",
                )
            )
            state = old
            return True
        return False

    for event, pin in _effects(events, PIN_ACTION, "pin"):
        old, new = pin.get("from"), pin.get("to")
        if not settle(old):
            expected = pending[2] if pending is not None else state
            return mismatch({"stream": stream, "seq": event.get("seq")}, expected, old)
        pending = (event, old, new)
    manifest = effects.manifest_pin.to_json()
    if not settle(manifest):
        expected = pending[2] if pending is not None else state
        return mismatch({"stream": stream, "path": "manifest.toml"}, expected, manifest)
    return orphans


def effect_anchors(
    installation_events: Iterable[Mapping[str, JsonValue]],
) -> dict[str, frozenset[int]]:
    """Per graph id, the graph-stream seqs anchored by companion events (W.e).

    Only a companion, an installation event carrying a maintenance action
    with effect, proves its intent committed: it is appended after the
    rename. Anchors of ``audit verify`` and ``audit anchor`` record stream
    heads and prove nothing about the anchored event's effect.
    """
    found: dict[str, set[int]] = {}
    for event in installation_events:
        anchors, actions = event.get("anchors"), event.get("actions")
        if not isinstance(anchors, list) or not isinstance(actions, list):
            continue
        if not any(a in MAINT_EFFECT_ACTIONS for a in actions):
            continue
        for anchor in anchors:
            seq = anchor.get("seq") if isinstance(anchor, Mapping) else None
            if isinstance(anchor, Mapping) and isinstance(seq, int):
                found.setdefault(str(anchor.get("graph_id")), set()).add(seq)
    return {graph_id: frozenset(seqs) for graph_id, seqs in found.items()}


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
            if stream_hashes is None and event.get("actions") == ["maint.graph_create"]:
                # W.f appends this event before publishing the graph: a crash in
                # between leaves exactly this (the staging dir is ignored).
                findings.append(
                    _finding(
                        codes.INTENT_WITHOUT_EFFECT,
                        target,
                        f"graph create intent without the graph {graph_id} (an interrupted create)",
                    )
                )
                continue
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
    effects: MaintEffects | None = None,
    ext_actions: Iterable[str] = (),
) -> StreamReport:
    """One graph stream plus the cross-checks: ``txs=None`` skips the tx check (damaged
    tx log), ``effects=None`` the maintenance check."""
    report = verify_stream(lines, stream=graph_stream(str(graph_id)), ext_actions=ext_actions)
    extra = []
    if txs is not None:
        extra += cross_check(graph_id, report.events, txs)
    if effects is not None:
        extra += check_maintenance(graph_id, report.events, effects)
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
