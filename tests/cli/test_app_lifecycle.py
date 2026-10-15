"""S: graph transition, supersede and retract on both fixtures (plan A7; contracts B3.18,
B6.3, B8.12). Role-based: values come from the compiled model, no type is named."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.app import ENV, app, cli_create, codes, create_graph
from support.fixtures import TOY_ONTOLOGIES, compiled_model
from support.roles import Roles, lifecycle_path, load_roles
from support.services import OpsFactory


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


def _last_installation_event(root: Path) -> dict[str, Any]:
    (segment,) = (root / "audit").glob("events-*.jsonl")
    event: dict[str, Any] = json.loads(segment.read_bytes().splitlines()[-1])
    return event


def test_transition_and_guard_failure(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    attr = roles.term("lifecycle_attribute").rpartition(".")[2]
    guarded = roles.get("guarded_transition")
    assert isinstance(guarded, dict)
    lifecycle = factory.model.lifecycles[guarded["lifecycle"]]
    good = cli_create(run_cli, factory, cls, "good")["good"]
    bad = cli_create(run_cli, factory, cls, "bad")["bad"]
    for entity in (good, bad):
        for state in lifecycle_path(
            factory.model, guarded["lifecycle"], lifecycle.initial[0], guarded["from"]
        ):
            moved = app(run_cli, "transition", entity, state, env=ENV)
            assert moved.code == 0, moved.envelope
    breaking = roles.get("guard_breaking_update")
    assert isinstance(breaking, dict)
    sets = [a for name, v in breaking.items() for a in ("--set-json", f"{name}={json.dumps(v)}")]
    assert app(run_cli, "update", bad, *sets, env=ENV).code == 0
    dry = app(run_cli, "transition", bad, guarded["to"], "--dry-run", env=ENV)
    assert (dry.code, dry.error["code"]) == (1, "write.invalid")
    assert codes(dry.error["details"]["findings"]) == ["data.lifecycle.guard_failed"]
    done = app(run_cli, "transition", good, guarded["to"], env=ENV)
    assert done.code == 0 and done.data["entities"][0]["ops"] == ["transition"]
    shown = app(run_cli, "get", good, env=ENV).data["entity"]
    assert shown["attrs"][attr] == guarded["to"]
    unknown = app(run_cli, "transition", good, "no_such_state", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "write.invalid")


def test_supersede_needs_a_reason_and_reads_warn(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("supersede_target_type")
    old = cli_create(run_cli, factory, cls, "old")["old"]
    new = cli_create(run_cli, factory, cls, "new")["new"]
    before = _last_installation_event(root)
    dry = app(run_cli, "supersede", new, old, "--dry-run", env=ENV)
    assert (dry.code, dry.error["code"]) == (3, "access.reason_required")
    assert _last_installation_event(root) == before  # dry-run denials are not audited
    refused = app(run_cli, "supersede", new, old, env=ENV)
    assert (refused.code, refused.error["code"]) == (3, "access.reason_required")
    denial = _last_installation_event(root)
    assert denial["result"] == {"status": "denied", "code": "access.reason_required"}
    done = app(run_cli, "supersede", new, old, "--reason", "replaced", env=ENV)
    assert done.code == 0, done.envelope
    assert [(e["id"], e["ops"]) for e in done.data["entities"]] == [
        (new, ["supersede"]),
        (old, ["supersede"]),
    ]
    shown = app(run_cli, "get", old, env=ENV)
    assert shown.code == 0 and shown.warning_codes == ["entity.superseded"]
    assert shown.data["superseded_by"] == [new]
    assert shown.data["entity"]["id"] == old  # no redirect (B8.12)
    found = app(run_cli, "find", "--type", cls, env=ENV).data["items"]
    assert [i["id"] for i in found] == [new]
    twice = app(run_cli, "supersede", new, old, old, "--reason", "x", env=ENV)
    assert (twice.code, twice.error["code"]) == (2, "usage.invalid_argument")


def test_retract_with_and_without_by(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("retractable_type")
    first = cli_create(run_cli, factory, cls, "r")["r"]
    second = cli_create(run_cli, factory, cls, "s")["s"]
    correction = cli_create(run_cli, factory, cls, "c")["c"]
    refused = app(run_cli, "retract", first, env=ENV)
    assert (refused.code, refused.error["code"]) == (3, "access.reason_required")
    assert app(run_cli, "retract", first, "--reason", "wrong", env=ENV).code == 0
    shown = app(run_cli, "get", first, env=ENV)
    assert shown.warning_codes == ["entity.retracted"] and shown.data["retracted_by"] == []
    again = app(run_cli, "retract", first, "--reason", "again", env=ENV)
    assert (again.code, again.error["code"]) == (1, "write.invalid")
    assert codes(again.error["details"]["findings"]) == ["data.temporal.record_not_active"]
    by = app(run_cli, "retract", second, "--by", correction, "--reason", "corrected", env=ENV)
    assert by.code == 0, by.envelope
    assert app(run_cli, "get", second, env=ENV).data["retracted_by"] == [correction]
    text = run_cli("graph", "get", second, env=ENV)
    assert text.returncode == 0 and "entity.retracted" in text.stderr
