"""T: installed policies against their policy events (contracts B5.15, R.6; L5).

``integrity.audit.policy_mismatch`` lives here because it needs installed
policy files. Policies are applied with the real ``graph-admin policy apply``
and edited by hand afterwards, as a same-user agent could.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.policies import GRAPH, INSTALLATION, write_policy

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.canonical import hash_document, jsonl_line, pretty_bytes
from graph_cli.storage.files.layout import segment_name

LOCATION = ("create", "inv:Location", "--set", "name=Shelf", "--set", "code=S1")
POINTER = 'format_version = 1\nproject = "example_project"\ngraphs = ["example_graph"]\n'


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


def test_mirrors_decide_with_the_graph_layer(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A graph with its own policy records the mirror under both layers."""
    apply(run_cli, write_policy(tmp_path, "g.yaml", GRAPH), "--graph", "example_graph")
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
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
    mismatches = [f for f in findings if f["code"] == audit_codes.POLICY_MISMATCH]
    assert len(mismatches) == 1
    assert "seq" in mismatches[0]["target"]  # the apply whose ``from`` does not continue
    # The second apply decided under the hand-edited file: its mirror in each graph stream.
    hashes = [f for f in findings if f["code"] == audit_codes.POLICY_HASH_MISMATCH]
    assert len(hashes) == 2 and len(findings) == 3


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


def test_a_file_in_place_of_the_policy_directory(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Moving the policy out of the way must not fall back to the built-in policy."""
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    directory = root / "policy"
    (directory / "installation.json").unlink()
    directory.rmdir()
    directory.write_bytes(b"")
    refused = app(run_cli, "find", "--graph", "example_graph")
    assert (refused.code, refused.error["code"]) == (5, "policy.invalid")
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


def rewrite(
    path: Path, keep: Callable[[dict[str, Any]], bool], change: Callable[[dict[str, Any]], None]
) -> None:
    """Drop and edit events, then renumber and rechain: a consistent rewrite of history."""
    out: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        document = json.loads(line)
        if not keep(document):
            continue
        change(document)
        document["seq"] = len(out) + 1
        if out:
            document["prev_hash"] = out[-1]["hash"]
        body = {k: v for k, v in document.items() if k != "hash"}
        out.append({**body, "hash": hash_document(body)})
    path.write_bytes(b"".join(jsonl_line(d) for d in out))


def is_mirror(document: dict[str, Any]) -> bool:
    return "installation_policy" in (document.get("change") or {})


def test_a_write_without_the_installed_policy(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Finding 1: the policy moved away for one write (or another data root without it)."""
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    policy = root / "policy" / "installation.json"
    aside = tmp_path / "aside.json"
    policy.rename(aside)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    written = app(run_cli, *LOCATION, "--graph", "example_graph", cwd=elsewhere)
    assert written.code == 0, written.envelope  # the built-in policy allowed it
    aside.rename(policy)
    assert codes(run_cli) == [audit_codes.POLICY_HASH_MISMATCH]


def test_a_cut_installation_history(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """Finding 2: the apply cut from the installation stream and the policy deleted."""
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    installation = root / "audit" / segment_name("events-", 1)
    rewrite(installation, lambda d: d["actions"] != ["maint.policy_change"], lambda d: None)
    (root / "policy" / "installation.json").unlink()
    found = codes(run_cli)
    assert found and set(found) == {audit_codes.POLICY_MISMATCH}  # each graph's mirror


def test_graph_admin_without_the_installed_policy(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    policy = root / "policy" / "installation.json"
    intact = policy.read_bytes()
    policy.write_bytes(b"[]\n")
    apply(run_cli, write_policy(tmp_path, "g.yaml", GRAPH), "--graph", "example_graph")
    policy.write_bytes(intact)  # repaired; the graph policy was decided without it
    assert codes(run_cli) == [audit_codes.POLICY_FALLBACK]


def test_a_change_applied_before_mirrors(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """An installation that applied its policy before mirror events existed."""
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))
    project = tmp_path / "example_project"
    project.mkdir()
    (project / ".graph_cli.toml").write_text(POINTER)
    assert app(run_cli, *LOCATION, "--graph", "example_graph", cwd=project).code == 0
    for stream in (root / "graphs").glob("*/audit/events-*.jsonl"):
        rewrite(stream, lambda d: not is_mirror(d), lambda d: None)
        (stream.parent.parent / "data" / "head.json").unlink(missing_ok=True)  # a checkpoint
    rewrite(root / "audit" / segment_name("events-", 1), lambda d: True, strip_anchors)
    assert codes(run_cli) == [audit_codes.POLICY_HASH_UNCHECKED]
    apply(run_cli, write_policy(tmp_path, "i.yaml", INSTALLATION))  # records the mirrors
    # The old range stays loose; each mirror itself was decided in it, so both graphs have one.
    assert codes(run_cli) == [audit_codes.POLICY_HASH_UNCHECKED] * 2
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    policy = root / "policy" / "installation.json"
    policy.rename(tmp_path / "aside.json")
    second = (*LOCATION[:-1], "code=S2")
    assert app(run_cli, *second, "--graph", "example_graph", cwd=elsewhere).code == 0
    (tmp_path / "aside.json").rename(policy)
    assert audit_codes.POLICY_HASH_MISMATCH in codes(run_cli)  # strict after the mirrors


def strip_anchors(document: dict[str, Any]) -> None:
    if document["actions"] == ["maint.policy_change"] and "graph" not in document:
        document.pop("anchors", None)
