"""S: installed policies in both programs (contracts B6.13-B6.16, B5.15, B12.6).

Toy graphs and the neutral project names ``example_project`` and
``another_project`` only.
"""

from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.policies import GRAPH, INSTALLATION, write_policy

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditStream, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def test_an_invalid_installed_policy_fails_closed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    assert app(run_cli, "graphs").code == 0
    policy = root / "policy" / "installation.json"
    policy.parent.mkdir()
    policy.write_text('{"schema": "graph_cli.policy/1", "rules": "everything"}\n')
    for args in (("graphs",), ("whoami",), ("find", "--graph", "example_graph")):
        outcome = app(run_cli, *args)
        assert outcome.code == 5, (args, outcome.envelope)
        assert outcome.error["code"] == "policy.invalid"
        assert outcome.error["details"]["file"] == "policy/installation.json"
    # graph-admin decides without the broken file, so the maintainer can replace it.
    listed = admin(run_cli, "graph", "list")
    assert listed.code == 0, listed.envelope
    assert "admin.policy_fallback" in listed.warning_codes


def apply(run_cli: CliRunner, path: Path, *args: str) -> Outcome:
    return admin(run_cli, "policy", "apply", str(path), *args, "--reason", "test policy")


def events(root: Path, stream: str) -> list[dict[str, Any]]:
    log = FileAuditLog(root).read_lines(AuditStream(stream))
    report = verify_stream(log, stream=AuditStream(stream))
    assert report.findings == ()
    return [dict(event) for event in report.events]


def test_check_show_diff_and_apply_the_installation_policy(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    checked = admin(run_cli, "policy", "check", str(path))
    assert checked.code == 0, checked.envelope
    assert (checked.data["layer"], checked.data["graph"]) == ("installation", None)
    shown = admin(run_cli, "policy", "show").data
    assert (shown["installed"], shown["document"]["id"]) == (False, "builtin_default")
    diff = admin(run_cli, "policy", "diff", str(path)).data
    assert diff["changed"] and diff["to"] == checked.data["hash"]
    assert "builtin.allow_app" in diff["rules"]["removed"]
    assert "grant.1" in diff["rules"]["added"]
    applied = apply(run_cli, path)
    assert applied.code == 0, applied.envelope
    assert applied.data == {
        "layer": "installation",
        "graph": None,
        "from": None,
        "to": checked.data["hash"],
        "applied": True,
    }
    shown = admin(run_cli, "policy", "show").data
    assert (shown["installed"], shown["hash"]) == (True, checked.data["hash"])
    again = apply(run_cli, path)
    assert again.data["applied"] is False  # the installed document: nothing recorded
    changes = [
        e for e in events(root, INSTALLATION_STREAM) if e["actions"] == ["maint.policy_change"]
    ]
    assert len(changes) == 2  # the intent, then the confirmation after the switch
    assert changes[0]["change"] == {"policy": {"from": None, "to": checked.data["hash"]}}
    assert "change" not in changes[1]
    assert changes[0]["reason"] == "test policy"
    assert changes[0]["authz"]["decisions"][0]["resource"] == "installation"
    assert admin(run_cli, "audit", "verify").code == 0


def test_graph_policy_is_a_graph_scoped_change(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    graph_id = create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "work", "example_graph.yaml", GRAPH)
    missing = admin(run_cli, "policy", "check", str(path))
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")
    applied = apply(run_cli, path, "--graph", "example_graph")
    assert applied.code == 0, applied.envelope
    intent = events(root, graph_stream(graph_id))[-1]
    assert intent["actions"] == ["maint.policy_change"]
    assert intent["change"]["policy"]["to"] == applied.data["to"]
    companion = events(root, INSTALLATION_STREAM)[-1]
    assert companion["anchors"][0]["seq"] == intent["seq"]
    assert "change" not in companion
    shown = admin(run_cli, "policy", "show", "--graph", "example_graph").data
    assert shown["document"]["types"] == {"inv:Location": "read"}
    assert admin(run_cli, "audit", "verify").code == 0
    installation = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    wrong = admin(run_cli, "policy", "check", str(installation), "--graph", "example_graph")
    assert wrong.error["code"] == "usage.invalid_argument"


def test_unknown_types_warn(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "work", "g.yaml", GRAPH.replace("inv:Location", "files:File"))
    checked = admin(run_cli, "policy", "check", str(path), "--graph", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["admin.policy_unknown_type"]


def test_invalid_policy_lists_every_problem(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    broken = INSTALLATION.replace('no_project: {"*": read}\n', "")
    path = write_policy(tmp_path / "work", "installation.yaml", broken)
    checked = admin(run_cli, "policy", "check", str(path))
    assert (checked.code, checked.error["code"]) == (5, "policy.invalid")
    paths = {p["path"] for p in checked.error["details"]["problems"]}
    assert {"no_project", "scopes.example_project.other_graph"} <= paths  # no other_graph here
    assert not (root / "policy").exists()


def test_apply_refuses_a_lockout(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    locked = INSTALLATION + "maintainers:\n  - {name: somebody_else}\n"
    path = write_policy(tmp_path / "work", "installation.yaml", locked)
    refused = apply(run_cli, path)
    assert (refused.code, refused.error["code"]) == (1, "admin.policy_lockout")
    assert refused.error["details"] == {"rule": "default.deny"}
    assert not (root / "policy").exists()
    assert not [
        e for e in events(root, INSTALLATION_STREAM) if "maint.policy_change" in e["actions"]
    ]


def test_explain(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    before = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--file", str(path))
    assert before.code == 0, before.envelope
    assert (before.data["scope"], before.data["level"]) == ("no_project", "read")
    in_project = admin(
        run_cli,
        "policy",
        "explain",
        "--graph",
        "other_graph",
        "--project",
        "example_project",
        "--action",
        "write.create",
        "--type",
        "inv:Item",
        "--file",
        str(path),
    )
    decision = in_project.data["decisions"]
    assert decision == [
        {
            "action": "write.create",
            "effect": "deny",
            "rule": "scope.example_project.other_graph",
            "code": "access.denied",
            "obligations": None,
        }
    ]
    unknown = admin(
        run_cli,
        "policy",
        "explain",
        "--graph",
        "example_graph",
        "--project",
        "unknown_project",
        "--file",
        str(path),
    )
    assert unknown.data["scope"] == "no_project"
    assert unknown.warning_codes == ["access.project_unknown"]
    installed = admin(run_cli, "policy", "explain", "--graph", "example_graph")
    assert (installed.data["scope"], installed.data["level"]) == (None, "write")  # built-in
