"""S: graph apply on both fixtures (plan A7; contracts B3.8, B3.9, B3.10, B3.12, B1.9).

The seeds go through ``graph apply -`` as JSONL with cross-document locals.
Role-based: values come from the compiled model, no type is named.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.app import ENV, app, codes, create_graph
from support.fixtures import FIXTURES, TOY_ONTOLOGIES, compiled_model
from support.roles import Roles, load_roles
from support.services import OpsFactory
from support.trees import tree_hash

SEEDS = FIXTURES / "seeds"


@pytest.fixture(params=TOY_ONTOLOGIES)
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], ontology: str) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ontology)
    return root


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


@pytest.fixture
def factory(ontology: str) -> OpsFactory:
    return OpsFactory(compiled_model(ontology))


def seed(ontology: str) -> str:
    return (SEEDS / ontology / "changes.jsonl").read_text()


def jsonl(*documents: dict[str, Any]) -> str:
    return "".join(json.dumps(d) + "\n" for d in documents)


def total(run_cli: CliRunner) -> int:
    found = app(run_cli, "find", "--include-inactive", env=ENV)
    assert found.code == 0, found.envelope
    count: int = found.envelope["page"]["total"]
    return count


def test_seed_through_stdin_and_idempotent_rerun(
    run_cli: CliRunner, root: Path, ontology: str
) -> None:
    lines = seed(ontology).splitlines()
    applied = app(run_cli, "apply", "-", env=ENV, input=seed(ontology))
    assert applied.code == 0, applied.envelope
    data = applied.data
    assert data["documents"] == data["applied"] == len(lines)
    assert [r["index"] for r in data["results"]] == list(range(len(lines)))
    assert [r["result"]["seq"] for r in data["results"]] == list(range(1, len(lines) + 1))
    assert applied.envelope["graph"]["seq"] == len(lines)
    count = total(run_cli)
    assert count > len(lines)  # documents create several entities via locals
    again = app(run_cli, "apply", "-", env=ENV, input=seed(ontology))
    assert again.code == 0, again.envelope
    assert all(r["result"]["idempotent_replay"] for r in again.data["results"])
    assert again.envelope["graph"]["seq"] == len(lines) and total(run_cli) == count


def test_file_argument_and_dry_run(
    run_cli: CliRunner, root: Path, ontology: str, tmp_path: Path
) -> None:
    path = tmp_path / "changes.jsonl"
    path.write_text(seed(ontology))
    before = tree_hash(root)
    dry = app(run_cli, "apply", str(path), "--dry-run", env=ENV)
    assert dry.code == 0, dry.envelope
    assert dry.data["dry_run"] is True and dry.data["applied"] == dry.data["documents"]
    assert all(r["result"]["tx_id"] is None for r in dry.data["results"])
    assert dry.envelope["graph"]["seq"] is None
    assert tree_hash(root) == before
    done = app(run_cli, "apply", str(path), env=ENV)
    assert done.code == 0 and done.data["applied"] == done.data["documents"]
    missing = app(run_cli, "apply", str(tmp_path / "missing.jsonl"), env=ENV)
    assert (missing.code, missing.error["code"]) == (2, "usage.invalid_argument")


def test_stop_at_the_first_failure_with_results(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    attr = next(n for n, v in factory.attrs(cls).items() if isinstance(v, str))
    documents = jsonl(
        {"ops": factory.create(cls, "a")},
        {"ops": [{"op": "update", "id": "@a", "set": {attr: "renamed"}}]},
        {"ops": [{"op": "update", "id": "@a", "set": {"no_such_attribute": 1}}]},
        {"ops": factory.create(cls, "never")},
    )
    outcome = app(run_cli, "apply", "-", env=ENV, input=documents)
    assert (outcome.code, outcome.error["code"]) == (1, "write.invalid")
    assert outcome.error["details"]["document_index"] == 2
    assert codes(outcome.error["details"]["findings"]) == ["data.structural.unknown_attribute"]
    data = outcome.data
    assert (data["documents"], data["applied"]) == (4, 2)
    assert [(r["index"], r["ok"]) for r in data["results"]] == [(0, True), (1, True), (2, False)]
    assert data["results"][2]["error"]["code"] == "write.invalid"
    first = data["results"][0]["result"]
    created = next(e for e in first["entities"] if e["local"] == "a")
    assert data["results"][1]["result"]["id"] == created["id"]  # @a across documents
    assert outcome.envelope["graph"]["seq"] == 2
    shown = app(run_cli, "get", created["id"], env=ENV).data["entity"]
    assert shown["attrs"][attr] == "renamed" and shown["rev"] == 2
    failing = jsonl(
        {"ops": [{"op": "update", "id": created["id"], "set": {"no_such_attribute": 1}}]}
    )
    text = run_cli("graph", "apply", "-", env=ENV, input=failing)
    assert text.returncode == 1 and "results" in text.stdout  # partial data on stdout
    assert "error: write.invalid: document 0:" in text.stderr


def test_broken_input_writes_nothing(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    good = json.dumps({"ops": factory.create(cls, "a")})
    before = tree_hash(root)
    broken_line = app(run_cli, "apply", "-", env=ENV, input=f"{good}\n{good}\n{{broken\n")
    assert (broken_line.code, broken_line.error["code"]) == (2, "usage.invalid_input")
    assert broken_line.error["details"]["line"] == 3 and broken_line.data is None
    abbreviated = {"ops": [{"op": "update", "id": "0123456789abcdef", "set": {}}]}
    malformed = app(run_cli, "apply", "-", env=ENV, input=jsonl({"ops": []}, abbreviated))
    assert (malformed.code, malformed.error["code"]) == (2, "changeset.malformed")
    assert malformed.error["details"]["document_index"] == 0
    abbrev = app(run_cli, "apply", "-", env=ENV, input=f"{good}\n{json.dumps(abbreviated)}\n")
    assert (abbrev.code, abbrev.error["code"]) == (2, "changeset.malformed")
    assert abbrev.error["details"]["document_index"] == 1
    empty = app(run_cli, "apply", "-", env=ENV, input="\n")
    assert (empty.code, empty.error["code"]) == (2, "usage.invalid_input")
    assert tree_hash(root) == before
