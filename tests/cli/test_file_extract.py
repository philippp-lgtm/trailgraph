"""S: derived file metadata through the CLI on a throwaway root (file metadata plan M12, step 6).

``graph file add`` extracts the metadata of the file it stored, in the same command, and answers
them; ``graph-admin files extract`` brings a whole graph (or named files) to the current
extractor versions, writes nothing the second time and in a dry run, and ``files verify`` names
records behind their extractor. Real extractors, run in the child process, on the files of the
golden corpus; the graph imports the PDF and SQLite kinds (``notes_and_sources`` 1.3.0).
"""

from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import ENV, app, create_graph
from support.managed_scene import extractor_write, record_ops
from support.policies import write_policy

pytestmark = pytest.mark.slow

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "files"
GRAPH = "example_graph"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, GRAPH, "notes_and_sources", version="1.3.0")
    return root


def add(run_cli: CliRunner, name: str, *flags: str) -> Outcome:
    outcome = app(run_cli, "file", "add", str(CORPUS / name), "--no-origin", *flags, env=ENV)
    assert outcome.code == 0, outcome.envelope
    return outcome


def extract(run_cli: CliRunner, *args: str) -> Outcome:
    return admin(run_cli, "files", "extract", "--graph", GRAPH, *args)


def head(outcome: Outcome) -> int | None:
    seq = outcome.envelope["graph"]["seq"]
    return None if seq is None else int(seq)


def tally(outcome: Outcome, name: str) -> dict[str, Any]:
    (found,) = [t for t in outcome.data["extractors"] if t["name"] == name]
    return dict(found)


def statuses(outcome: Outcome) -> dict[str, str]:
    return {m["extractor"]: m["status"] for m in outcome.data["metadata"]}


def records(run_cli: CliRunner, type_: str) -> list[dict[str, Any]]:
    found = app(run_cli, "find", "--type", type_, "--limit", "100", env=ENV)
    assert found.code == 0, found.envelope
    return list(found.data["items"])


def test_file_add_answers_the_metadata_of_the_file_it_stored(
    run_cli: CliRunner, root: Path
) -> None:
    added = add(run_cli, "one_page.pdf")
    assert statuses(added) == {"format": "ok", "pdf": "ok"}
    by_name = {m["extractor"]: m for m in added.data["metadata"]}
    assert all(m["version"] == 1 and m["id"] for m in by_name.values())
    assert head(added) == 3  # the file, then one tx per record
    (pdf,) = records(run_cli, "files_pdf:PdfMetadata")
    assert pdf["id"] == by_name["pdf"]["id"]
    shown = app(run_cli, "get", by_name["pdf"]["id"], env=ENV)
    assert shown.data["entity"]["attrs"]["page_count"] == 1
    assert shown.data["entity"]["attrs"]["key"] == f"pdf/{added.data['entity']['id']}"


def test_an_extractor_that_does_not_apply_leaves_no_record(run_cli: CliRunner, root: Path) -> None:
    added = add(run_cli, "notes.txt")
    assert statuses(added) == {"format": "ok"}
    assert records(run_cli, "files_pdf:PdfMetadata") == []


def test_a_damaged_file_is_a_recorded_result_and_the_add_succeeds(
    run_cli: CliRunner, root: Path
) -> None:
    added = add(run_cli, "damaged.pdf")
    by_name = {m["extractor"]: m for m in added.data["metadata"]}
    assert by_name["pdf"]["status"] == "failed" and by_name["pdf"]["problem"] == "pdf.unreadable"
    assert added.data["entity"]["id"]


def test_no_extract_stores_the_file_alone_and_files_extract_finishes_the_job(
    run_cli: CliRunner, root: Path
) -> None:
    added = add(run_cli, "database.sqlite", "--no-extract")
    assert added.data["metadata"] == [] and head(added) == 1
    assert records(run_cli, "files:FormatMetadata") == []
    done = extract(run_cli)
    assert done.code == 0, done.envelope
    assert tally(done, "format")["created"] == 1 and tally(done, "sqlite")["created"] == 1
    (record,) = records(run_cli, "files_sqlite:SqliteMetadata")
    shown = app(run_cli, "get", record["id"], env=ENV)
    assert shown.data["entity"]["attrs"]["table_count"] == 2


def test_a_dry_run_of_the_add_extracts_nothing(run_cli: CliRunner, root: Path) -> None:
    dry = add(run_cli, "one_page.pdf", "--dry-run")
    assert dry.data["metadata"] == [] and dry.data["dry_run"] is True
    assert head(dry) is None
    assert records(run_cli, "files:FormatMetadata") == []


def test_adding_a_known_file_again_writes_nothing(run_cli: CliRunner, root: Path) -> None:
    first = add(run_cli, "one_page.pdf")
    again = add(run_cli, "one_page.pdf")
    assert again.data["existing"] is True
    assert head(again) == head(first)  # every record is current: no tx
    assert [m["id"] for m in again.data["metadata"]] == [m["id"] for m in first.data["metadata"]]


def test_the_records_name_the_extractor_as_their_source(run_cli: CliRunner, root: Path) -> None:
    added = add(run_cli, "one_page.pdf")
    record = next(m["id"] for m in added.data["metadata"] if m["extractor"] == "pdf")
    history = app(run_cli, "history", record, env=ENV)
    (item,) = history.data["items"]
    assert item["source"] == {"name": "pdf", "version": "1", "kind": "extractor"}  # no run
    assert item["command"] == "file add" and item["reason"] == "extract pdf@1"
    admin_run = add(run_cli, "three_pages.pdf", "--no-extract")
    done = extract(run_cli, "--file", admin_run.data["entity"]["id"])
    assert done.data["run"] is not None
    other = next(r for r in records(run_cli, "files_pdf:PdfMetadata") if r["id"] != record)
    (item,) = app(run_cli, "history", other["id"], env=ENV).data["items"]
    assert item["source"]["run"] == done.data["run"] and item["command"] == "files extract"


def test_a_dry_run_runs_the_extractors_and_writes_nothing(run_cli: CliRunner, root: Path) -> None:
    add(run_cli, "one_page.pdf", "--no-extract")
    add(run_cli, "database.sqlite", "--no-extract")
    dry = extract(run_cli, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert dry.data["dry_run"] is True and dry.data["files"] == 2
    assert tally(dry, "format")["created"] == 2 and tally(dry, "pdf")["created"] == 1
    assert head(dry) is None  # nothing written
    assert records(run_cli, "files:FormatMetadata") == []


def test_a_second_run_writes_nothing(run_cli: CliRunner, root: Path) -> None:
    add(run_cli, "one_page.pdf", "--no-extract")
    add(run_cli, "notes.txt", "--no-extract")
    first = extract(run_cli)
    assert tally(first, "format")["created"] == 2 and tally(first, "pdf")["created"] == 1
    assert head(first) == 2 + 3
    again = extract(run_cli)
    assert head(again) is None and tally(again, "format")["current"] == 2
    assert all(t["created"] == t["updated"] == 0 for t in again.data["extractors"])


def test_the_selection_limits_what_is_written(run_cli: CliRunner, root: Path) -> None:
    pdf = add(run_cli, "one_page.pdf", "--no-extract").data["entity"]["id"]
    add(run_cli, "database.sqlite", "--no-extract")
    done = extract(run_cli, "--file", pdf, "--extractor", "pdf")
    assert done.data["files"] == 1
    assert tally(done, "pdf")["created"] == 1 and tally(done, "format")["created"] == 1
    assert records(run_cli, "files_sqlite:SqliteMetadata") == []  # not selected
    unknown = extract(run_cli, "--extractor", "nope")
    assert unknown.code == 2 and unknown.error["code"] == "usage.invalid_argument"
    missing = extract(run_cli, "--file", "01a1eef4-0000-7000-8000-000000000000")
    assert missing.code != 0 and missing.error["code"] == "entity.not_found"


def test_retry_failed_runs_failed_records_again(run_cli: CliRunner, root: Path) -> None:
    add(run_cli, "damaged.pdf")
    plain = extract(run_cli)
    assert tally(plain, "pdf")["current"] == 1 and head(plain) is None
    retried = extract(run_cli, "--retry-failed")
    assert tally(retried, "pdf")["current"] == 0
    assert tally(retried, "pdf")["failed"] == 1 and tally(retried, "pdf")["unchanged"] == 1


def test_extractors_lists_what_the_program_can_extract(run_cli: CliRunner, root: Path) -> None:
    listed = admin(run_cli, "files", "extractors")
    assert listed.code == 0, listed.envelope
    names = [e["name"] for e in listed.data["extractors"]]
    assert names == ["format", "pdf", "image", "table", "sqlite"]
    sqlite = listed.data["extractors"][-1]
    assert sqlite["part_class"] == "files_sqlite:SqliteObject"
    assert sqlite["module"] == {"id": "files_sqlite", "min_version": "1.0.0"}
    assert sqlite["accepts"] == ["application/vnd.sqlite3"]


def test_verify_names_records_of_a_newer_install_and_extract_leaves_them(
    run_cli: CliRunner, root: Path
) -> None:
    """Version 1 is the lowest there is, so only the newer side can be written here; the
    records behind an extractor are covered in ``tests/unit/admin/test_records_behind.py``."""
    file = add(run_cli, "one_page.pdf", "--no-extract").data["entity"]["id"]
    create = {**record_ops("pdf", file, page_count=1), "type": "files_pdf:PdfMetadata"}
    created = extractor_write(root, [create], "pdf")
    assert admin(run_cli, "files", "verify", "--graph", GRAPH).data["metadata"]["behind"] == []
    update = {"op": "update", "id": str(created.entities[0].id), "set": {"extractor_version": 9}}
    extractor_write(root, [update], "pdf")  # as a newer install wrote it
    newer = extract(run_cli)
    assert tally(newer, "pdf")["newer_version"] == 1 and tally(newer, "pdf")["updated"] == 0
    assert newer.data["newer_versions"] == [
        {"extractor": "pdf", "file": file, "stored_version": 9, "version": 1}
    ]
    (listed,) = admin(run_cli, "files", "verify", "--graph", GRAPH).data["metadata"]["behind"]
    assert listed == {"extractor": "pdf", "version": 1, "records": 1, "behind": 0, "newer": 1}


def test_a_graph_that_does_not_import_the_files_module_has_nothing_to_extract(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, GRAPH, "inventory")
    done = extract(run_cli)
    assert done.code == 0 and done.data["enabled"] is False and done.data["files"] == 0


NO_RECORDS = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_records, action: [write.create], type: [files:FormatMetadata, files_pdf:PdfMetadata]}
"""


def test_a_caller_without_the_right_to_write_records_stores_the_file_and_is_warned(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The records are written as the caller (M12): without ``write.create`` on the record
    classes the add still stores the file, answers no metadata, and says what is missing; the
    refusal is audited like any other denial."""
    path = write_policy(tmp_path / "policies", "graph.yaml", NO_RECORDS)
    applied = admin(run_cli, "policy", "apply", str(path), "--graph", GRAPH, "--reason", "no")
    assert applied.code == 0, applied.envelope
    added = add(run_cli, "one_page.pdf")
    assert added.data["entity"]["id"] and added.data["metadata"] == []
    (warning,) = [
        w for w in added.envelope["warnings"] if w["code"] == "file.extraction_incomplete"
    ]
    assert warning["details"]["cause"] == "access.denied"
    assert warning["details"]["action"] == "write.create"
    assert "you may not write its metadata records" in warning["message"]
    assert records(run_cli, "files:FormatMetadata") == []
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"]["error"] == 0
