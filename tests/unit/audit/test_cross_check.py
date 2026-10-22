"""tx <-> intent cross-check and anchors (contracts B5.6, B5.11, B5.12, A3)."""

import dataclasses
from collections.abc import Callable

from support.audit_events import ENV, REQUEST, allow, intent, maint_actor, sealed_lines
from support.memlog import MemLog
from support.txbuilder import TxDraft

from graph_cli.audit import events as builders
from graph_cli.audit import finding_codes as codes
from graph_cli.audit.verify import (
    check_anchors,
    cross_check,
    verify_graph,
    verify_installation,
)
from graph_cli.kernel.canonical import JsonValue, parse_json
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import EntityId, EventId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditLine, AuditReceipt, graph_stream
from graph_cli.kernel.records import TxRecord

Event = dict[str, JsonValue]


class Graph:
    """A graph with its tx log and intents, built like the write pipeline does."""

    def __init__(self, seed: int = 0) -> None:
        self.log = MemLog("notes_and_sources", seed)
        self.graph_id = self.log.graph_id
        self.drafts: list[AuditEventDraft] = []
        self.txs: list[TxRecord] = []

    def write(self, edit: Callable[[TxDraft], object], *, commit: bool = True) -> None:
        draft = self.log.draft()
        edit(draft)
        tx = draft.build()
        at = self.log.at()
        before: dict[EntityId, str] = {}
        for revision in tx.revisions:
            previous = at.current(revision.id)
            if previous is not None:
                before[revision.id] = previous.content_hash
        self.drafts.append(intent(tx, before=before))
        if commit:
            self.txs.append(self.log.commit(tx))

    def random(self, count: int) -> None:
        for _ in range(count):
            self.write(self.log.builder.random_tx)

    def events(self) -> list[Event]:
        return [dict(as_object(parse_json(line), "e")) for line in self.lines()]

    def lines(self) -> list[bytes]:
        return sealed_lines(self.drafts)

    def raw(self) -> list[AuditLine]:
        return [AuditLine(f"memory:{n}", line) for n, line in enumerate(self.lines())]


def test_consistent_graph_has_no_findings() -> None:
    graph = Graph()
    graph.random(8)
    assert cross_check(graph.graph_id, graph.events(), graph.txs) == []
    report = verify_graph(graph.raw(), graph_id=graph.graph_id, txs=graph.txs)
    assert report.findings == ()


def test_orphan_intent_is_info() -> None:
    graph = Graph()
    graph.random(2)
    graph.write(graph.log.builder.random_tx, commit=False)
    (finding,) = cross_check(graph.graph_id, graph.events(), graph.txs)
    assert (finding.code, finding.severity) == (codes.INTENT_WITHOUT_EFFECT, Severity.INFO)
    assert finding.target == {"stream": graph_stream(str(graph.graph_id)), "seq": 3}


def test_tx_without_intent() -> None:
    graph = Graph()
    graph.random(3)
    events = graph.events()
    del events[1]
    (finding,) = cross_check(graph.graph_id, events, graph.txs)
    assert finding.code == codes.TX_WITHOUT_INTENT
    assert finding.target == {"graph_id": str(graph.graph_id), "tx_seq": 2}


def test_intent_that_does_not_match_its_tx() -> None:
    graph = Graph()
    graph.random(2)
    tampered = dataclasses.replace(graph.txs[1], changeset_hash="sha256:" + "f" * 64, seq=7)
    findings = cross_check(graph.graph_id, graph.events(), [graph.txs[0], tampered])
    (finding,) = findings
    assert finding.code == codes.TX_WITHOUT_INTENT
    assert finding.expected == {"changeset_hash": tampered.changeset_hash, "tx_seq": 7}
    assert finding.actual == {"changeset_hash": graph.txs[1].changeset_hash, "tx_seq": 2}


def test_an_intent_with_another_changeset_size_does_not_match() -> None:
    graph = Graph()
    graph.random(1)
    events = graph.events()
    change = as_object(events[0]["change"], "change")
    size = change["changeset_size"]
    assert size == builders.changeset_size(graph.txs[0].changeset)
    events[0] = {**events[0], "change": {**change, "changeset_size": 1}}
    (finding,) = cross_check(graph.graph_id, events, graph.txs)
    assert finding.code == codes.TX_WITHOUT_INTENT
    assert (finding.expected, finding.actual) == ({"changeset_size": size}, {"changeset_size": 1})


def test_an_intent_without_a_changeset_size_still_matches() -> None:
    """Intents recorded before the size was added carry no changeset_size."""
    graph = Graph()
    graph.random(1)
    events = graph.events()
    change = dict(as_object(events[0]["change"], "change"))
    del change["changeset_size"]
    events[0] = {**events[0], "change": change}
    assert cross_check(graph.graph_id, events, graph.txs) == []


def test_a_denied_or_malformed_event_is_no_intent() -> None:
    graph = Graph()
    graph.random(1)
    events = graph.events()
    events[0] = {**events[0], "result": {"status": "invalid", "code": "write.invalid"}}
    (finding,) = cross_check(graph.graph_id, events, graph.txs)
    assert finding.code == codes.TX_WITHOUT_INTENT
    events[0] = {**graph.events()[0], "change": "broken"}
    (finding,) = cross_check(graph.graph_id, events, graph.txs)
    assert finding.code == codes.TX_WITHOUT_INTENT


def test_one_intent_serves_one_tx() -> None:
    graph = Graph()
    graph.random(1)
    findings = cross_check(graph.graph_id, graph.events(), [graph.txs[0], graph.txs[0]])
    assert [f.code for f in findings] == [codes.TX_WITHOUT_INTENT]


def test_damaged_tx_log_skips_the_cross_check() -> None:
    graph = Graph()
    graph.random(2)
    graph.write(graph.log.builder.random_tx, commit=False)
    report = verify_graph(graph.raw(), graph_id=graph.graph_id, txs=None)
    assert report.findings == ()


# ---- anchors -----------------------------------------------------------------


def _anchor_event(graph: Graph, seq: int, digest: str) -> AuditEventDraft:
    stream = graph_stream(str(graph.graph_id))
    receipt = AuditReceipt(stream, EventId(graph.log.builder.ids.new_id()), seq, digest, None)
    return builders.maintenance(
        actor=maint_actor(),
        decisions=[allow("maint.audit_anchor", "installation")],
        request=REQUEST,
        ts=graph.log.builder.clock.now(),
        event_id=EventId(graph.log.builder.ids.new_id()),
        env=ENV,
        anchors=[receipt],
    )


def _installation(*drafts: AuditEventDraft) -> list[Event]:
    return [dict(as_object(parse_json(line), "e")) for line in sealed_lines(drafts)]


def test_anchors_match() -> None:
    graph = Graph()
    graph.random(3)
    last = graph.events()[-1]
    seq, digest = last["seq"], last["hash"]
    assert isinstance(seq, int) and isinstance(digest, str)
    anchors = _installation(_anchor_event(graph, seq, digest))
    assert check_anchors(anchors, {str(graph.graph_id): graph.events()}) == []


def test_anchor_mismatches() -> None:
    graph = Graph()
    graph.random(3)
    events = graph.events()
    digest = events[1]["hash"]
    assert isinstance(digest, str)
    installation = _installation(
        _anchor_event(graph, 2, "sha256:" + "e" * 64),  # rewritten history
        _anchor_event(graph, 9, digest),  # truncated below the anchor
    )
    findings = check_anchors(installation, {str(graph.graph_id): events})
    assert [(f.code, f.target["seq"]) for f in findings] == [
        (codes.ANCHOR_MISMATCH, 1),
        (codes.ANCHOR_MISMATCH, 2),
    ]
    assert findings[0].actual == digest
    (missing,) = check_anchors(installation[:1], {})
    assert "missing" in missing.message


def test_verify_installation_composes_anchors() -> None:
    graph = Graph()
    graph.random(2)
    graph_report = verify_graph(graph.raw(), graph_id=graph.graph_id, txs=graph.txs)
    lines = sealed_lines([_anchor_event(graph, 5, "sha256:" + "e" * 64)])
    raw = [AuditLine(f"memory:{n}", line) for n, line in enumerate(lines)]
    report = verify_installation(raw, graphs={str(graph.graph_id): graph_report})
    assert [f.code for f in report.findings] == [codes.ANCHOR_MISMATCH]


def test_a_retry_that_repeats_the_event_id_leaves_one_orphan() -> None:
    graph = Graph()
    graph.random(1)
    graph.drafts.insert(0, graph.drafts[0])  # the first attempt died after its intent
    findings = cross_check(graph.graph_id, graph.events(), graph.txs)
    assert [(f.code, f.target["seq"]) for f in findings] == [(codes.INTENT_WITHOUT_EFFECT, 1)]
