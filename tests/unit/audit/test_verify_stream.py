"""Stream verifier: one finding per damaged line, no cascades (contracts B5.3, B5.9, A2)."""

from collections.abc import Sequence

from support.audit_events import denial, sealed_lines
from support.clock import StepClock
from support.ids import SeededIdProvider

from graph_cli.audit import finding_codes as codes
from graph_cli.audit.verify import StreamReport, verify_stream
from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    JsonValue,
    hash_document,
    jsonl_line,
    parse_json,
)
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditLine
from graph_cli.storage.common.chain import ChainLink, link_of, seal

STREAM = INSTALLATION_STREAM


def stream(count: int) -> list[bytes]:
    clock = StepClock()
    ids = SeededIdProvider(clock, 3)
    return sealed_lines([denial(EventId(ids.new_id()), clock.now()) for _ in range(count)])


def doc(line: bytes) -> dict[str, JsonValue]:
    return dict(as_object(parse_json(line), "event"))


def rehash(event: dict[str, JsonValue]) -> bytes:
    event = {k: v for k, v in event.items() if k != "hash"}
    event["hash"] = hash_document(event)
    return jsonl_line(event)


def verify(lines: Sequence[bytes], **options: object) -> StreamReport:
    raw = [AuditLine(f"memory:{n}", data) for n, data in enumerate(lines)]
    return verify_stream(raw, stream=STREAM, **options)  # type: ignore[arg-type]


def codes_of(report: StreamReport) -> list[tuple[str, JsonValue]]:
    return [(f.code, f.target.get("seq", f.target.get("position"))) for f in report.findings]


def test_clean_stream() -> None:
    lines = stream(4)
    report = verify(lines)
    assert report.findings == ()
    assert [e["seq"] for e in report.events] == [1, 2, 3, 4]
    last = doc(lines[-1])
    assert report.head == (4, last["hash"])


def test_empty_stream() -> None:
    assert verify([]).head == (0, GENESIS_HASH)


def test_torn_or_unreadable_tail_is_no_finding() -> None:
    lines = stream(2)
    assert verify([*lines, b'{"schema":"graph_cli.au']).findings == ()
    assert verify([*lines, b"garbage\n"]).findings == ()
    assert verify([*lines, b"garbage\n"]).head[0] == 2


def test_torn_interior_line_does_not_cascade() -> None:
    lines = stream(4)
    report = verify([lines[0], b"not json\n", lines[2], lines[3]])
    assert codes_of(report) == [(codes.TORN_INTERIOR_LINE, "memory:1")]
    assert report.head[0] == 4


def test_line_merged_into_a_torn_one() -> None:
    """A torn line with the next event appended to it swallows that event too."""
    lines = stream(4)
    merged = lines[1][:20] + lines[2]
    report = verify([lines[0], merged, lines[3]])
    assert codes_of(report) == [(codes.TORN_INTERIOR_LINE, "memory:1"), (codes.SEQ_GAP, 4)]


def test_deleted_line_is_one_seq_gap() -> None:
    lines = stream(4)
    report = verify([lines[0], lines[2], lines[3]])
    assert codes_of(report) == [(codes.SEQ_GAP, 3)]
    (finding,) = report.findings
    assert (finding.expected, finding.actual) == (2, 3)


def test_reordered_lines() -> None:
    lines = stream(4)
    report = verify([lines[0], lines[2], lines[1], lines[3]])
    assert codes_of(report) == [(codes.SEQ_GAP, 3), (codes.SEQ_GAP, 2), (codes.SEQ_GAP, 4)]


def test_edited_field_is_one_hash_mismatch() -> None:
    lines = stream(3)
    event = doc(lines[1])
    event["ts"] = "2030-01-01T00:00:00.000Z"
    report = verify([lines[0], jsonl_line(event), lines[2]])
    assert codes_of(report) == [(codes.HASH_MISMATCH, 2)]


def test_edited_and_rehashed_breaks_the_chain_at_the_next_line() -> None:
    lines = stream(3)
    event = doc(lines[1])
    event["ts"] = "2030-01-01T00:00:00.000Z"
    report = verify([lines[0], rehash(event), lines[2]])
    assert codes_of(report) == [(codes.CHAIN_BROKEN, 3)]


def test_event_from_another_stream() -> None:
    lines = stream(2)
    event = doc(lines[1])
    event["stream"] = "graph:0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
    report = verify([lines[0], rehash(event)])
    found = codes_of(report)
    assert found[0] == (codes.CHAIN_BROKEN, 2)


def test_non_canonical_bytes() -> None:
    lines = stream(2)
    report = verify([lines[0], lines[1].replace(b'{"actions"', b'{ "actions"')])
    assert codes_of(report) == [(codes.EVENT_MALFORMED, 2)]
    assert report.findings[0].message == "line is not canonical JSON"


def _append(lines: list[bytes], body: dict[str, JsonValue]) -> list[bytes]:
    previous = as_object(parse_json(lines[-1]), "event")
    _, line = seal(body, link_of(previous))
    return [*lines, line]


def _body(line: bytes) -> dict[str, JsonValue]:
    event = doc(line)
    for key in ("seq", "prev_hash", "hash"):
        del event[key]
    return event


def test_unknown_schema() -> None:
    lines = stream(2)
    body = _body(lines[1]) | {"schema": "graph_cli.audit/2"}
    report = verify(_append(lines, body))
    assert codes_of(report) == [(codes.UNKNOWN_SCHEMA, 3)]


def test_malformed_event() -> None:
    lines = stream(2)
    body = _body(lines[1])
    del body["request"]
    report = verify(_append(lines, body))
    assert codes_of(report) == [(codes.EVENT_MALFORMED, 3)]
    assert "missing key 'request'" in report.findings[0].message


def test_a_request_may_name_an_action_but_only_well_formed() -> None:
    lines = stream(2)
    named = {"name": "assign_position", "hash": "sha256:" + "d" * 64}
    body = _body(lines[1])
    body["request"] = {**body["request"], "action": named}  # type: ignore[dict-item]
    assert verify(_append(lines, body)).findings == ()
    for broken in ({"name": "assign_position"}, {**named, "hash": "x"}, "assign_position"):
        body["request"] = {**body["request"], "action": broken}  # type: ignore[dict-item]
        report = verify(_append(lines, body))
        assert codes_of(report) == [(codes.EVENT_MALFORMED, 3)]
        assert "request.action is not {name, hash}" in report.findings[0].message
    edited = doc(_append(lines, body)[-1])
    edited["request"] = {**edited["request"], "action": named}  # type: ignore[dict-item]
    report = verify([*lines, jsonl_line(edited)])  # an edit that was not rehashed
    assert (codes.HASH_MISMATCH, 3) in codes_of(report)


def test_unknown_action_is_a_warning() -> None:
    lines = stream(2)
    body = _body(lines[1])
    body["actions"] = ["ext.notes.tag"]
    authz = as_object(body["authz"], "authz")
    decision = {**as_object(authz["decisions"][0], "d"), "action": "ext.notes.tag"}  # type: ignore[index]
    body["authz"] = {**authz, "decisions": [decision]}
    extended = _append(lines, body)
    report = verify(extended)
    assert codes_of(report) == [(codes.UNKNOWN_ACTION, 3)]
    assert report.findings[0].severity is Severity.WARNING
    assert verify(extended, ext_actions=["ext.notes.tag"]).findings == ()


def test_seq_that_is_not_a_number() -> None:
    lines = stream(2)
    event = doc(lines[1]) | {"seq": "2"}
    report = verify([lines[0], rehash(event)])
    assert (codes.SEQ_GAP, "memory:1") in codes_of(report)


def test_chain_resumes_from_the_stored_line() -> None:
    """After a gap the next intact line is clean: one deletion, one finding."""
    lines = stream(5)
    report = verify([lines[0], lines[1], lines[3], lines[4]])
    assert codes_of(report) == [(codes.SEQ_GAP, 4)]
    assert report.head == (5, doc(lines[4])["hash"])
    assert ChainLink(5, str(doc(lines[4])["hash"])) == link_of(doc(lines[4]))
