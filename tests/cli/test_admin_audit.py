"""S: graph-admin audit verify and anchor (contracts B5.11, B5.12; plan M7).

Tamper scenarios through the CLI: one per finding class (graph stream,
installation stream, storage, pin, anchor), plus the clean and the benign
(info) cases. The per-code tamper scenarios live in tests/tamper/.
"""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_random_txs
from support.clock import StepClock
from support.fixtures import ONTOLOGIES
from support.ids import SeededIdProvider

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.canonical import hash_document, jsonl_line, parse_json
from graph_cli.kernel.ids import GraphId, format_id, parse_id
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditEventDraft, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.store import FileStore

INVENTORY = ONTOLOGIES / "inventory"
CHAIN_KEYS = ("seq", "prev_hash", "hash")


class Installation:
    def __init__(self, run_cli: CliRunner, root: Path) -> None:
        self.run_cli = run_cli
        self.root = root
        created = admin(
            run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.0.0")
        )
        self.graph_id: str = created.data["id"]
        assert (
            admin(run_cli, "ontology", "release", "example_graph", str(INVENTORY / "1.1.0")).code
            == 0
        )
        write_random_txs(root, self.graph_id, ontology="inventory", version="1.1.0", count=5)
        other = admin(
            run_cli,
            "graph",
            "create",
            "notes",
            "--ontology",
            str(ONTOLOGIES / "notes_and_sources" / "1.0.0"),
        )
        self.other_id: str = other.data["id"]

    def verify(self) -> Outcome:
        return admin(self.run_cli, "audit", "verify")

    @property
    def graph_dir(self) -> Path:
        return self.root / "graphs" / self.graph_id

    def graph_audit(self) -> Path:
        (segment,) = (self.graph_dir / "audit").glob("events-*.jsonl")
        return segment

    def installation_audit(self) -> Path:
        (segment,) = (self.root / "audit").glob("events-*.jsonl")
        return segment


@pytest.fixture
def inst(run_cli: CliRunner, isolated_env: dict[str, str]) -> Installation:
    return Installation(run_cli, init(run_cli, isolated_env))


def codes(outcome: Outcome) -> list[str]:
    findings = (
        outcome.data["findings"] if outcome.code == 0 else outcome.error["details"]["findings"]
    )
    return sorted(f["code"] for f in findings)


def rewrite_line(path: Path, index: int, *, rehash: bool) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    document = dict(parse_json(lines[index]))  # type: ignore[arg-type]
    document["ts"] = "2031-01-01T00:00:00.000Z"
    if rehash:
        document["hash"] = hash_document(document)
    lines[index] = jsonl_line(document)
    path.write_bytes(b"".join(lines))


def test_clean_installation_verifies_and_records_anchors(inst: Installation) -> None:
    outcome = inst.verify()
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert outcome.data["recorded"] is True
    assert {g["id"]: g["clean"] for g in outcome.data["graphs"]} == {
        inst.graph_id: True,
        inst.other_id: True,
    }
    report = verify_stream(
        FileAuditLog(inst.root).read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM
    )
    last = report.events[-1]
    assert last["actions"] == ["maint.audit_verify"]
    assert sorted(a["graph_id"] for a in last["anchors"]) == sorted([inst.graph_id, inst.other_id])
    assert inst.verify().code == 0  # the verify event itself verifies


def test_an_interrupted_write_is_info_only(inst: Installation) -> None:
    """An intent appended without its tx (a crash between W.12 and W.13)."""
    lines = inst.graph_audit().read_bytes().splitlines(keepends=True)
    intent = next(line for line in reversed(lines) if b'"write.' in line)
    body = {k: v for k, v in as_object(parse_json(intent), "e").items() if k not in CHAIN_KEYS}
    body["event_id"] = format_id(SeededIdProvider(StepClock(), 99).new_id())
    body["change"] = {**as_object(body["change"], "change"), "tx_seq": 99}
    store = FileStore(inst.graph_dir, GraphId(parse_id(inst.graph_id)))
    with store.session(timeout_s=5) as session:
        session.append_audit(AuditEventDraft(graph_stream(inst.graph_id), body))
    outcome = inst.verify()
    assert outcome.code == 0
    assert codes(outcome) == ["integrity.audit.intent_without_effect"]
    assert outcome.data["counts"]["info"] == 1


def test_graph_stream_tamper(inst: Installation) -> None:
    rewrite_line(inst.graph_audit(), 2, rehash=False)
    outcome = inst.verify()
    assert (outcome.code, outcome.error["code"]) == (1, "admin.integrity_errors")
    assert codes(outcome) == ["integrity.audit.hash_mismatch"]
    report = verify_stream(
        FileAuditLog(inst.root).read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM
    )
    anchored = [a["graph_id"] for a in report.events[-1]["anchors"]]
    assert anchored == [inst.other_id]  # the damaged graph is not anchored


def test_storage_tamper(inst: Installation) -> None:
    revision = sorted((inst.graph_dir / "data" / "entities").rglob("r*.json"))[0]
    document = json.loads(revision.read_text())
    document["attrs"]["tampered"] = "yes"
    revision.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    outcome = inst.verify()
    assert outcome.code == 1
    assert codes(outcome) == ["integrity.storage.revision_hash_mismatch"]


def test_manifest_pin_tamper(inst: Installation) -> None:
    manifest = inst.graph_dir / "manifest.toml"
    pinned = admin(inst.run_cli, "graph", "show", "example_graph").data["ontology"]
    first = json.loads((inst.graph_dir / "ontology" / "1.0.0" / "release.json").read_text())
    text = manifest.read_text()
    text = text.replace(pinned["version"], "1.0.0").replace(
        pinned["artifact_hash"], first["artifact_hash"]
    )
    manifest.write_text(text)
    outcome = inst.verify()
    assert outcome.code == 1
    assert codes(outcome) == ["integrity.audit.pin_mismatch"]


def test_installation_tamper_is_not_recorded(inst: Installation) -> None:
    rewrite_line(inst.installation_audit(), 1, rehash=True)
    before = inst.installation_audit().read_bytes()
    outcome = inst.verify()
    assert outcome.code == 1
    assert codes(outcome) == ["integrity.audit.chain_broken"]
    assert outcome.warning_codes == ["admin.verify_not_recorded"]
    assert inst.installation_audit().read_bytes() == before


def test_anchor_then_truncate(inst: Installation) -> None:
    anchored = admin(inst.run_cli, "audit", "anchor")
    assert anchored.code == 0
    assert {a["graph_id"] for a in anchored.data["anchors"]} == {inst.graph_id, inst.other_id}
    assert inst.verify().code == 0
    lines = inst.graph_audit().read_bytes().splitlines(keepends=True)
    inst.graph_audit().write_bytes(b"".join(lines[:-1]))
    outcome = inst.verify()
    assert outcome.code == 1
    assert "integrity.audit.anchor_mismatch" in codes(outcome)
