"""Operational log file and the finding-code union (contracts section 4.2, R.4; plan M5)."""

import json
from pathlib import Path

from support.clock import FixedClock

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.catalog.oplog import OpLogFile
from graph_cli.catalog.registries import all_finding_codes
from graph_cli.ontology_layer import finding_codes as ontology_codes
from graph_cli.storage import finding_codes as storage_codes


def test_oplog_appends_one_line_per_note(tmp_path: Path) -> None:
    oplog = OpLogFile(tmp_path / "state", FixedClock("2026-10-15T08:00:00Z"))
    oplog({"event": "recovery", "graph_id": "g", "steps": []})
    oplog({"event": "tail_truncated", "stream": "installation"})
    lines = oplog.path.read_text().splitlines()
    assert oplog.path == tmp_path / "state" / "logs" / "operations.jsonl"
    assert [json.loads(line)["event"] for line in lines] == ["recovery", "tail_truncated"]
    assert json.loads(lines[0])["ts"] == "2026-10-15T08:00:00.000Z"


def test_oplog_failures_are_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be")
    OpLogFile(blocker, FixedClock())({"event": "recovery"})
    assert blocker.read_text().startswith("a file")


def test_finding_code_union() -> None:
    codes = all_finding_codes()
    owners = (ontology_codes.FINDING_CODES, audit_codes.FINDING_CODES, storage_codes.FINDING_CODES)
    assert len(codes) == sum(len(specs) for specs in owners)
    assert list(codes) == sorted(codes)
    assert {"integrity.audit.pin_mismatch", "integrity.storage.pin_mismatch"} <= set(codes)
