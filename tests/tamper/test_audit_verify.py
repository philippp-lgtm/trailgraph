"""T: one tamper scenario per integrity.audit.* code (contracts B5.12, R.6).

A FileStore graph with real audit events (four write txs) plus an
installation stream (an anchor on graph seq 2, then three denials). Each
scenario edits files the way an attacker or a damaged disk would and must
produce exactly its own code.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from support.audit_events import ENV, REQUEST, allow, denial, maint_actor, verify_graph_stream
from support.stores import StoreEnv, snapshot_state

from graph_cli.audit import events as builders
from graph_cli.audit import finding_codes
from graph_cli.audit.verify import verify_installation
from graph_cli.kernel.canonical import JsonValue, hash_document, jsonl_line, parse_json
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditReceipt
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.layout import segment_name

Event = dict[str, JsonValue]


class Scene:
    def __init__(self, root: Path) -> None:
        self.env = StoreEnv("files", "notes_and_sources", root, real_audit=True)
        self.env.random(4)
        audit = self.env.audit
        assert isinstance(audit, FileAuditLog)
        self.audit = audit
        seq, digest = 2, self.graph_lines()[1]
        anchored = as_object(parse_json(digest), "e")["hash"]
        assert isinstance(anchored, str)
        receipt = AuditReceipt(self.env.stream, self.event_id(), seq, anchored, None)
        audit.append_installation(
            builders.maintenance(
                actor=maint_actor(),
                decisions=[allow("maint.audit_anchor", "installation")],
                request=REQUEST,
                ts=self.env.clock.now(),
                event_id=self.event_id(),
                env=ENV,
                anchors=[receipt],
            )
        )
        for _ in range(3):
            audit.append_installation(denial(self.event_id(), self.env.clock.now()))

    def event_id(self) -> EventId:
        return EventId(self.env.ids.new_id())

    @property
    def graph_file(self) -> Path:
        return (
            self.env.root / "graphs" / str(self.env.graph_id) / "audit" / segment_name("events-", 1)
        )

    @property
    def installation_file(self) -> Path:
        return self.audit.installation_dir / segment_name("events-", 1)

    def graph_lines(self) -> list[bytes]:
        return self.graph_file.read_bytes().splitlines(keepends=True)

    def installation_lines(self) -> list[bytes]:
        return self.installation_file.read_bytes().splitlines(keepends=True)

    def codes(self) -> list[str]:
        graph = verify_graph_stream(self.env.store, self.audit, self.env.graph_id)
        installation = verify_installation(
            self.audit.read_lines(INSTALLATION_STREAM), graphs={str(self.env.graph_id): graph}
        )
        return sorted(f.code for f in (*graph.findings, *installation.findings))


def event(line: bytes) -> Event:
    return dict(as_object(parse_json(line), "event"))


def sealed(document: Event) -> bytes:
    body = {k: v for k, v in document.items() if k != "hash"}
    body["hash"] = hash_document(body)
    return jsonl_line(body)


def rechain(lines: list[bytes], start: int) -> list[bytes]:
    """Recompute prev_hash and hash from ``start`` on: a rewrite of history."""
    out = lines[:start]
    for line in lines[start:]:
        document = event(line)
        if out:
            document["prev_hash"] = event(out[-1])["hash"]
        out.append(sealed(document))
    return out


def edit(line: bytes, change: Callable[[Event], None], *, rehash: bool) -> bytes:
    document = event(line)
    change(document)
    return sealed(document) if rehash else jsonl_line(document)


def later_ts(document: Event) -> None:
    document["ts"] = "2030-01-01T00:00:00.000Z"


def chain_broken(scene: Scene) -> None:
    lines = scene.graph_lines()
    lines[2] = edit(lines[2], later_ts, rehash=True)
    scene.graph_file.write_bytes(b"".join(lines))


def seq_gap(scene: Scene) -> None:
    lines = scene.installation_lines()
    del lines[2]
    scene.installation_file.write_bytes(b"".join(lines))


def hash_mismatch(scene: Scene) -> None:
    lines = scene.installation_lines()
    lines[1] = edit(lines[1], later_ts, rehash=False)
    scene.installation_file.write_bytes(b"".join(lines))


def torn_interior_line(scene: Scene) -> None:
    lines = scene.installation_lines()
    lines[2] = lines[2][: len(lines[2]) // 2] + b"\n"
    scene.installation_file.write_bytes(b"".join(lines))


def _last_installation(change: Callable[[Event], None]) -> Callable[[Scene], None]:
    def apply(scene: Scene) -> None:
        lines = scene.installation_lines()
        lines[-1] = edit(lines[-1], change, rehash=True)
        scene.installation_file.write_bytes(b"".join(lines))

    return apply


def _schema(document: Event) -> None:
    document["schema"] = "graph_cli.audit/9"


def _drop_request(document: Event) -> None:
    del document["request"]


def _teleport(document: Event) -> None:
    document["actions"] = ["maint.teleport"]
    authz = dict(as_object(document["authz"], "authz"))
    (decision,) = authz["decisions"]  # type: ignore[misc]
    authz["decisions"] = [{**as_object(decision, "d"), "action": "maint.teleport"}]
    document["authz"] = authz


def intent_without_effect(scene: Scene) -> None:
    """Not a file edit: an intent whose write never committed (crash between W.12 and W.13)."""
    env = scene.env
    with env.store.session(timeout_s=5) as session:
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        env.builder.random_tx(draft)
        session.append_audit(env.intent(draft.build(), session.snapshot()))


def tx_without_intent(scene: Scene) -> None:
    def other_changeset(document: Event) -> None:
        change = dict(as_object(document["change"], "change"))
        change["changeset_hash"] = "sha256:" + "f" * 64
        document["change"] = change

    lines = scene.graph_lines()
    lines[-1] = edit(lines[-1], other_changeset, rehash=True)
    scene.graph_file.write_bytes(b"".join(lines))


def anchor_mismatch(scene: Scene) -> None:
    lines = scene.graph_lines()
    lines[1] = edit(lines[1], later_ts, rehash=True)
    scene.graph_file.write_bytes(b"".join(rechain(lines, 1)))


SCENARIOS: dict[str, Callable[[Scene], None]] = {
    finding_codes.CHAIN_BROKEN: chain_broken,
    finding_codes.SEQ_GAP: seq_gap,
    finding_codes.HASH_MISMATCH: hash_mismatch,
    finding_codes.TORN_INTERIOR_LINE: torn_interior_line,
    finding_codes.UNKNOWN_SCHEMA: _last_installation(_schema),
    finding_codes.EVENT_MALFORMED: _last_installation(_drop_request),
    finding_codes.UNKNOWN_ACTION: _last_installation(_teleport),
    finding_codes.INTENT_WITHOUT_EFFECT: intent_without_effect,
    finding_codes.TX_WITHOUT_INTENT: tx_without_intent,
    finding_codes.ANCHOR_MISMATCH: anchor_mismatch,
}


# Codes whose scenario needs manifests and releases (tests/tamper/test_maint_verify.py),
# installed policies (tests/tamper/test_policy_verify.py) or tokens
# (tests/tamper/test_token_verify.py).
MAINT_SCENARIO_CODES = {
    finding_codes.TOKEN_MISMATCH,
    finding_codes.PIN_MISMATCH,
    finding_codes.POLICY_MISMATCH,
    finding_codes.POLICY_HASH_MISMATCH,
    finding_codes.POLICY_FALLBACK,
    finding_codes.POLICY_HASH_UNCHECKED,
}


def test_every_audit_code_has_one_scenario() -> None:
    assert set(SCENARIOS) | MAINT_SCENARIO_CODES == {
        spec.code for spec in finding_codes.FINDING_CODES
    }
    assert not set(SCENARIOS) & MAINT_SCENARIO_CODES


def test_clean_installation_verifies_clean(tmp_path: Path) -> None:
    assert Scene(tmp_path).codes() == []


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(tmp_path: Path, code: str) -> None:
    scene = Scene(tmp_path)
    SCENARIOS[code](scene)
    assert scene.codes() == [code]


def test_storage_readers_still_work_on_a_tampered_stream(tmp_path: Path) -> None:
    """Verification reads raw lines; damage never turns into an exception there."""
    scene = Scene(tmp_path)
    lines = scene.installation_lines()
    scene.installation_file.write_bytes(lines[0] + b"\x00\xff garbage\n" + b"".join(lines[2:]))
    assert scene.codes() == [finding_codes.TORN_INTERIOR_LINE]
