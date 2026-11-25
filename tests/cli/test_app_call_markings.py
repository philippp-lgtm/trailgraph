"""S: ``graph call --expect-hash`` for a caller who holds only some markings (tool manifest plan
TM2, TM7): the hash the call compares is the one the manifest gives this caller, and a tool the
caller cannot see is not found. The ``staff`` graph and its catalogs are those of
``support.markings``."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.markings import (
    ACTION_SOURCES,
    FUNCTION_SOURCES,
    HOLDER,
    PERSONAL_ONLY,
    READER,
    STAFF,
    documents,
    without_twin,
)
from support.read_limits import install

ENV = {"GRAPH_CLI_GRAPH": "example_graph", "GRAPH_CLI_SPILL_BYTES": "0"}
POLICIES = {
    name: without_twin(text)
    for name, text in (("none", READER), ("personal", PERSONAL_ONLY), ("all", HOLDER))
}


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    for kind, sources in (("functions", FUNCTION_SOURCES), ("actions", ACTION_SOURCES)):
        folder = tmp_path / kind
        folder.mkdir()
        for file, text in sources.items():
            (folder / file).write_text(text)
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", documents())
    return tmp_path / "policies"


def tools(run_cli: CliRunner) -> dict[str, dict[str, Any]]:
    outcome = app(run_cli, "describe", "tools", env=ENV)
    assert outcome.code == 0, outcome.envelope
    return {t["name"]: t for t in outcome.data["tools"]}


def call(run_cli: CliRunner, tool: str, *args: str) -> Outcome:
    return app(run_cli, "call", tool, *args, env=ENV)


def code_of(outcome: Outcome) -> str | None:
    """The error code of an answer, ``None`` for one that succeeded."""
    error = outcome.envelope.get("error")
    return None if error is None else str(error["code"])


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
def test_the_hash_of_every_tool_is_the_one_of_the_manifest_of_the_caller(
    run_cli: CliRunner, world: Path
) -> None:
    seen: dict[str, set[str]] = {}
    for caller, policy in POLICIES.items():
        install(run_cli, world, policy)
        manifest = tools(run_cli)
        seen[caller] = set(manifest)
        for name, tool in manifest.items():
            good = call(run_cli, name, "--expect-hash", tool["hash"])
            assert code_of(good) != "tool.hash_mismatch", (caller, name)
            stale = call(run_cli, name, "--expect-hash", "sha256:" + "0" * 64)
            assert stale.error["code"] == "tool.hash_mismatch", (caller, name)
            assert stale.error["details"]["actual"] == tool["hash"], (caller, name)
    assert seen["none"] < seen["personal"] < seen["all"]  # the callers see different tools
    install(run_cli, world, POLICIES["none"])
    for name in sorted(seen["all"] - seen["none"]):  # what it cannot see is not found, hash or not
        for extra in ((), ("--expect-hash", "sha256:" + "0" * 64)):
            missing = call(run_cli, name, *extra)
            assert missing.error["code"] == "tool.not_found", (name, extra)
            assert name not in json.dumps(missing.error["details"]["did_you_mean"])
    # the hash of a tool the callers share can differ: the enum values and classes are theirs
    assert tools(run_cli).keys() == seen["none"]
