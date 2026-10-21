"""T: installed policies against their policy events (contracts B5.15, R.6; L5).

``integrity.audit.policy_mismatch`` lives here because it needs installed
policy files. Policies are applied with the real ``graph-admin policy apply``
and edited by hand afterwards, as a same-user agent could.
"""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.policies import GRAPH, INSTALLATION, write_policy

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.canonical import pretty_bytes


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    return root


def apply(run_cli: CliRunner, path: Path, *args: str) -> None:
    outcome = admin(run_cli, "policy", "apply", str(path), *args, "--reason", "tamper test")
    assert outcome.code == 0, outcome.envelope


def codes(run_cli: CliRunner) -> list[str]:
    outcome = admin(run_cli, "audit", "verify")
    if outcome.code == 0:
        return [f["code"] for f in outcome.data["findings"]]
    return [f["code"] for f in outcome.error["details"]["findings"]]


def widen(path: Path) -> None:
    """The hand edit an agent would make: write access for every project."""
    document = json.loads(path.read_bytes())
    document["rules"] = [r for r in document["rules"] if r["effect"] == "allow"]
    path.write_bytes(pretty_bytes(document))


def test_applied_policies_verify_clean(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    apply(run_cli, write_policy(tmp_path, "g.yaml", GRAPH), "--graph", "example_graph")
    assert codes(run_cli) == []


def test_hand_edited_installation_policy(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    widen(root / "policy" / "installation.json")
    assert codes(run_cli) == [audit_codes.POLICY_MISMATCH]


def test_a_later_apply_does_not_hide_a_hand_edit(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    widen(root / "policy" / "installation.json")
    apply(run_cli, write_policy(tmp_path, "i2.yaml", INSTALLATION.replace("read}", "none}")))
    outcome = admin(run_cli, "audit", "verify")
    findings = outcome.error["details"]["findings"]
    assert [f["code"] for f in findings] == [audit_codes.POLICY_MISMATCH]
    assert "seq" in findings[0]["target"]  # the apply whose ``from`` does not continue


def test_hand_placed_and_deleted_policies(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    graph = root / "graphs"
    graph_id = next(p.name for p in graph.iterdir() if (p / "manifest.toml").exists())
    placed = graph / graph_id / "policy.json"
    placed.write_bytes(
        pretty_bytes(
            {
                "schema": "graph_cli.policy/1",
                "id": "graph",
                "layer": "graph",
                "graph": graph_id,
                "types": {},
                "rules": [],
            }
        )
    )
    assert codes(run_cli) == [audit_codes.POLICY_MISMATCH]
    placed.unlink()
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    (root / "policy" / "installation.json").unlink()
    assert codes(run_cli) == [audit_codes.POLICY_MISMATCH]


def test_interrupted_apply_is_an_intent_without_effect(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The event is the intent: a crash before the rename leaves only it."""
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    path = root / "policy" / "installation.json"
    before = path.read_bytes()
    apply(run_cli, write_policy(tmp_path, "i2.yaml", INSTALLATION.replace("read}", "none}")))
    path.write_bytes(before)  # as if the rename never happened ...
    stream = root / "audit" / "events-000000000001.jsonl"
    lines = stream.read_bytes().splitlines(keepends=True)
    assert json.loads(lines[-1])["actions"] == ["maint.policy_change"]
    stream.write_bytes(b"".join(lines[:-1]))  # ... and its confirmation was never appended
    assert codes(run_cli) == [audit_codes.INTENT_WITHOUT_EFFECT]
    path.write_bytes(b"[]\n")  # a file that is no policy at all
    assert codes(run_cli) == [audit_codes.POLICY_MISMATCH]
