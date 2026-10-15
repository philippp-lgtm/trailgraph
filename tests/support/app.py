"""Running ``graph`` as a subprocess (S tests), next to ``support.admin``."""

from pathlib import Path
from typing import Any

from harness import CliRunner

from support.admin import Outcome, admin, parse
from support.fixtures import ONTOLOGIES, compiled_model


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
