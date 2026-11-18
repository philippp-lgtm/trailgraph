"""S: derived file metadata is written by its extractor only, through the CLI on a throwaway root
(file metadata plan M9, step 3).

``graph`` has no way to write a record: every command that would write one, or one of its parts,
ends in ``write.invalid`` with the finding ``data.semantic.managed_class``, exit 1, audited as an
invalid write. Pointing at a record from an ordinary entity stays allowed. A document cannot name
the reserved source kind, and a reconcile of a record type by a plain source is refused by its
scope before anything is planned. Records are seeded in process by a request of kind extractor.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import ENV, app
from support.managed_scene import GRAPH, MANAGED_CODE, Scene, build

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

pytestmark = pytest.mark.slow

ACTION = """\
schema: graph_cli.action/1
name: record_page_count
module: notes_and_sources
summary: Record the page count of a file as a metadata record.
use_when: >-
  Someone wants a page count stored for a file; this action writes a metadata record, which
  only the extraction service may do.
params:
  key:
    type: string
    summary: Key of the record.
  file:
    type: ref
    class: files:File
    summary: The file.
ops:
  - op: create
    type: files_pdf:PdfMetadata
    attrs: {key: $key, extractor: pdf, extractor_version: 1, status: ok}
    edges:
      - {rel: files:metadata_of, target: $file}
examples:
  - request: Store the page count of the report.
"""


@pytest.fixture
def scene(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Scene:
    root = init(run_cli, isolated_env)
    return build(run_cli, root, tmp_path / "work")


def findings(outcome: Outcome) -> list[dict[str, Any]]:
    assert outcome.code == 1, outcome.envelope
    assert outcome.error["code"] == "write.invalid", outcome.error
    return list(outcome.error["details"]["findings"])


def codes(outcome: Outcome) -> list[str]:
    return [f["code"] for f in findings(outcome)]


def last_event(root: Path) -> dict[str, Any]:
    graph = admin_graph_id(root)
    lines = FileAuditLog(root).read_lines(graph_stream(graph))
    return dict(verify_stream(lines, stream=graph_stream(graph)).events[-1])


def admin_graph_id(root: Path) -> str:
    (found,) = list((root / "graphs").iterdir())
    return found.name


def refused_and_audited(scene: Scene, outcome: Outcome) -> list[dict[str, Any]]:
    found = findings(outcome)
    assert [f["code"] for f in found] == [MANAGED_CODE]
    event = last_event(scene.root)
    assert event["result"]["status"] == "invalid"
    return found


def test_a_create_of_a_record_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    attrs = {"key": f"pdf/{scene.file}x", "extractor": "pdf", "extractor_version": 1}
    outcome = app(
        run_cli,
        *("create", "files_pdf:PdfMetadata"),
        *("--set-json", f"key={json.dumps(attrs['key'])}"),
        *("--set", "extractor=pdf", "--set-json", "extractor_version=1", "--set", "status=ok"),
        *("--edge", f"files:metadata_of={scene.file}"),
        env=ENV,
    )
    found = refused_and_audited(scene, outcome)
    assert found[0]["target"]["type"] == "files_pdf:PdfMetadata"


def test_a_create_of_a_part_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    outcome = app(
        run_cli,
        *("create", "files_sqlite:SqliteObject"),
        *("--set", f"key=sqlite/{scene.file}/other", "--set", "name=other", "--set", "kind=table"),
        env=ENV,
    )
    refused_and_audited(scene, outcome)


@pytest.mark.parametrize("which", ["pdf", "part"])
def test_an_update_is_refused(run_cli: CliRunner, scene: Scene, which: str) -> None:
    target = scene.pdf if which == "pdf" else scene.part
    attr = "page_count=9" if which == "pdf" else "column_count=3"
    found = refused_and_audited(scene, app(run_cli, "update", target, "--set", attr, env=ENV))
    assert found[0]["target"]["id"] == target


def test_a_link_from_a_record_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    outcome = app(run_cli, "link", scene.pdf, "core:source", scene.file, env=ENV)
    refused_and_audited(scene, outcome)


def test_an_unlink_from_a_record_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    outcome = app(run_cli, "unlink", scene.pdf, "files:metadata_of", scene.file, env=ENV)
    refused_and_audited(scene, outcome)


def test_a_transition_of_a_record_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    outcome = app(run_cli, "transition", scene.pdf, "retired", env=ENV)
    refused_and_audited(scene, outcome)


@pytest.mark.parametrize("which", ["pdf", "part"])
def test_a_retract_is_refused(run_cli: CliRunner, scene: Scene, which: str) -> None:
    target = scene.pdf if which == "pdf" else scene.part
    outcome = app(run_cli, "retract", target, "--reason", "tidy up", env=ENV)
    refused_and_audited(scene, outcome)


def test_an_alias_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    document = {"ops": [{"op": "set_alias", "id": scene.pdf, "alias": "PDF-0099"}]}
    outcome = app(run_cli, "apply", "-", env=ENV, input=json.dumps(document))
    refused_and_audited(scene, outcome)


def test_a_supersede_names_both_sides(run_cli: CliRunner, scene: Scene) -> None:
    outcome = app(run_cli, "supersede", scene.pdf, scene.sqlite, "--reason", "merge", env=ENV)
    assert codes(outcome) == [MANAGED_CODE, MANAGED_CODE]
    named = sorted(f["target"]["id"] for f in findings(outcome))
    assert named == sorted([scene.pdf, scene.sqlite])
    assert last_event(scene.root)["result"]["status"] == "invalid"


def test_an_apply_dry_run_with_a_record_op_is_refused(run_cli: CliRunner, scene: Scene) -> None:
    document = {"ops": [{"op": "retract", "id": scene.pdf}], "reason": "tidy up"}
    outcome = app(run_cli, "apply", "-", "--dry-run", env=ENV, input=json.dumps(document))
    assert codes(outcome) == [MANAGED_CODE]


def test_nothing_was_written_by_the_refusals(run_cli: CliRunner, scene: Scene) -> None:
    app(run_cli, "retract", scene.pdf, "--reason", "tidy up", env=ENV)
    shown = app(run_cli, "get", scene.pdf, env=ENV)
    assert shown.code == 0
    assert shown.data["entity"]["rev"] == 1
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"]["error"] == 0


def test_an_edge_from_an_ordinary_entity_to_a_record_is_allowed(
    run_cli: CliRunner, scene: Scene
) -> None:
    outcome = app(run_cli, "link", scene.file, "core:source", scene.pdf, env=ENV)
    assert outcome.code == 0, outcome.envelope


def test_a_reconcile_of_a_record_type_by_a_plain_source_is_refused_before_planning(
    run_cli: CliRunner, scene: Scene
) -> None:
    """A source named like an extractor, with no kind, and a scope that plans no op at all:
    the scope's type alone refuses it."""
    document = {
        "reason": "sync",
        "source": {"name": "format", "version": "1"},
        "reconcile": [
            {
                "type": "files:FormatMetadata",
                "key": "key",
                "under": scene.file,
                "fields": ["extractor"],
                "items": [],
            }
        ],
    }
    outcome = app(run_cli, "apply", "-", env=ENV, input=json.dumps(document))
    found = findings(outcome)
    assert [f["code"] for f in found] == [MANAGED_CODE]
    assert found[0]["target"] == {"type": "files:FormatMetadata", "path": "reconcile[0]"}
    assert last_event(scene.root)["result"]["status"] == "invalid"


def test_a_document_cannot_name_the_reserved_source_kind(run_cli: CliRunner, scene: Scene) -> None:
    document = {
        "source": {"name": "pdf", "kind": "extractor"},
        "ops": [{"op": "update", "id": scene.pdf, "set": {"page_count": 9}}],
        "reason": "forged",
    }
    outcome = app(run_cli, "apply", "-", env=ENV, input=json.dumps(document))
    assert outcome.code == 2, outcome.envelope
    assert outcome.error["code"] == "changeset.malformed"
    assert outcome.error["details"]["path"] == "source.kind"


def test_an_action_that_writes_a_record_is_refused_at_run_time(
    run_cli: CliRunner, scene: Scene, tmp_path: Path
) -> None:
    folder = tmp_path / "actions"
    folder.mkdir()
    (folder / "record_page_count.yaml").write_text(ACTION)
    published = admin(run_cli, "graph", "actions", "publish", GRAPH, str(folder))
    assert published.code == 0, published.envelope
    outcome = app(
        run_cli,
        *("do", "record_page_count"),
        *("--param", f"key=pdf/{scene.file}x", "--param", f"file={scene.file}"),
        env=ENV,
    )
    assert codes(outcome) == [MANAGED_CODE]
    assert last_event(scene.root)["result"]["status"] == "invalid"
