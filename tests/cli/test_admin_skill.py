"""S: graph-admin graph skill publish (D15 Q2; contracts B6 ``maint.graph_configure``, W.e).

The publish event pair (graph intent plus installation anchor) verifies
clean, a re-publish of the same files changes nothing, every broken tree
rule is reported, and a skill with maintenance vocabulary is refused (the
L positive control for graph skills).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES, ONTOLOGIES
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.catalog import skill_hash
from graph_cli.skills.tree import read_tree
from graph_cli.storage.files.audit_log import FileAuditLog

SKILL = FIXTURES / "skills" / "graph-example-graph"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    return create_graph(run_cli, "example_graph")


def publish(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "skill", "publish", "example_graph", str(source), *args)


def copy(tmp_path: Path, name: str = "graph-example-graph") -> Path:
    target = tmp_path / "work" / name
    shutil.copytree(SKILL, target)
    return target


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def installation_events(root: Path) -> list[dict[str, Any]]:
    log = FileAuditLog(root).read_lines(INSTALLATION_STREAM)
    report = verify_stream(log, stream=INSTALLATION_STREAM)
    assert report.findings == ()
    return [dict(event) for event in report.events]


def test_publish_records_the_change_and_verifies_clean(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    assert admin(run_cli, "graph", "show", "example_graph").data["skill"] is None
    files, _ = read_tree(SKILL)
    outcome = publish(run_cli, SKILL, "--reason", "first skill")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["published"], data["dry_run"], data["from"]) == (True, False, None)
    assert data["skill"] == "graph-example-graph"
    assert data["files"] == ["SKILL.md", "references/model.md"]
    assert data["to"]["hash"] == skill_hash(files)
    assert data["to"]["kind"] == "static"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["skill"] == data["to"]
    assert shown["skill"]["published_against"]["version"] == shown["ontology"]["version"]

    intent = graph_events(root, graph_id)[-1]
    assert intent["actions"] == ["maint.graph_configure"]
    assert intent["change"] == {"skill": {"from": None, "to": skill_hash(files)}}
    assert intent["reason"] == "first skill"
    companion = installation_events(root)[-1]
    assert companion["actions"] == ["maint.graph_configure"]
    assert companion["anchors"] == [
        {"graph_id": graph_id, "seq": intent["seq"], "hash": intent["hash"]}
    ]
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_republishing_the_same_files_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    assert publish(run_cli, SKILL).code == 0
    before = tree_hash(root)
    again = publish(run_cli, copy(tmp_path))  # same files from another place
    assert again.code == 0 and again.data["published"] is False
    assert again.data["from"] == again.data["to"]
    assert tree_hash(root) == before


def test_republishing_changed_files_names_both_hashes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    first = publish(run_cli, SKILL).data["to"]["hash"]
    changed = copy(tmp_path)
    (changed / "references" / "model.md").write_text("# Model\n\nRewritten.\n")
    outcome = publish(run_cli, changed)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == first
    assert graph_events(root, graph_id)[-1]["change"]["skill"]["from"] == first


def test_a_new_release_makes_the_same_files_publishable_again(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    publish(run_cli, SKILL)
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    outcome = publish(run_cli, SKILL)
    assert outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == outcome.data["to"]["hash"]
    assert outcome.data["to"]["published_against"]["version"] == "1.0.1"
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_writes_nothing(run_cli: CliRunner, root: Path, graph_id: str) -> None:
    before = tree_hash(root)
    outcome = publish(run_cli, SKILL, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["published"], outcome.data["dry_run"]) == (False, True)
    assert tree_hash(root) == before


def test_invalid_trees_list_every_broken_rule(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    broken = copy(tmp_path, "graph-other")
    skill_md = broken / "SKILL.md"
    skill_md.write_text(skill_md.read_text().replace("Read the", "Ask graph-admin or read the"))
    (broken / ".notes").write_text("x")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert outcome.code == 1
    assert outcome.error["code"] == "admin.skill_invalid"
    assert outcome.error["category"] == "invalid" and outcome.error["hint"] is None
    problems = outcome.error["details"]["problems"]
    assert sorted(p["rule"] for p in problems) == ["file", "leak", "name"]
    (leak,) = [p for p in problems if p["rule"] == "leak"]
    assert leak["path"] == "SKILL.md" and "graph-admin" in leak["message"]
    assert tree_hash(root) == before


def test_a_clean_copy_publishes(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    """Positive control for the refusal above: the same tree without the breakage."""
    assert publish(run_cli, copy(tmp_path)).code == 0


def test_text_mode_names_the_problems(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    broken = copy(tmp_path)
    (broken / "SKILL.md").write_text("no frontmatter\n")
    result = run_cli("graph-admin", "graph", "skill", "publish", "example_graph", str(broken))
    assert result.returncode == 1
    assert "admin.skill_invalid" in result.stderr and "frontmatter" in result.stderr
