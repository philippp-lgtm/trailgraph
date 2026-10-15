"""Running ``graph`` as a subprocess (S tests), next to ``support.admin``."""

import json
from pathlib import Path
from typing import Any

from harness import CliRunner

from support.admin import Outcome, admin, parse
from support.fixtures import ONTOLOGIES, compiled_model
from support.services import OpsFactory

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


def app(
    run_cli: CliRunner,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    input: str | None = None,
) -> Outcome:
    """``graph <args> --json``; stderr must stay empty in JSON mode (B9.1)."""
    outcome = parse(run_cli("graph", *args, "--json", env=env, cwd=cwd, input=input))
    assert outcome.stderr == "", outcome.stderr
    return outcome


def create_graph(run_cli: CliRunner, name: str, ontology: str = "inventory") -> str:
    version = compiled_model(ontology).bundle_version
    outcome = admin(
        run_cli, "graph", "create", name, "--ontology", str(ONTOLOGIES / ontology / version)
    )
    assert outcome.code == 0, outcome.envelope
    graph_id: str = outcome.data["id"]
    return graph_id


def codes(findings: Any) -> list[str]:
    return [f["code"] for f in findings]


def create_args(op: dict[str, Any], ids: dict[str, str]) -> list[str]:
    args = ["create", op["type"]]
    for name, value in op.get("attrs", {}).items():
        args += ["--set-json", f"{name}={json.dumps(value)}"]
    for edge in op.get("edges", []):
        args += ["--edge", f"{edge['rel']}={ids[edge['target'][1:]]}"]
    return args


def cli_create(
    run_cli: CliRunner, factory: OpsFactory, cls: str, local: str = "x"
) -> dict[str, str]:
    """Create ``cls`` and its required targets through ``graph create``; local -> id."""
    ids: dict[str, str] = {}
    for op in factory.create(cls, local):
        outcome = app(run_cli, *create_args(op, ids), env=ENV)
        assert outcome.code == 0, outcome.envelope
        ids[op["local"]] = outcome.data["entities"][0]["id"]
    return ids
