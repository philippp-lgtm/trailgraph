"""S: ``graph view`` on the example company model with its action and function fixtures published
(object views stage 1, step 2): the card of an empty and of a large entity, the hints for what
was cut, ``--as-of``, status warnings, the text form, and that the record reads stay as they are."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.actions import VALID as ACTIONS
from support.admin import admin, init
from support.app import app
from support.fixtures import EXAMPLES

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
FUNCTIONS = EXAMPLES / "company" / "functions"  # written against the example ontology
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
LIMIT = 6144


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    for kind, folder in (("actions", ACTIONS), ("functions", FUNCTIONS)):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    return root


def apply(run_cli: CliRunner, ops: list[dict[str, Any]]) -> dict[str, str]:
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    entities = applied.data["results"][0]["result"]["entities"]
    return {e["local"]: e["id"] for e in entities if e["local"]}


def department(local: str, name: str, *teams: str) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "org:Department", "local": local, "attrs": {"name": name}}
    ]
    ops += [
        {
            "op": "create",
            "type": "org:Team",
            "local": f"{local}_{team.lower()}",
            "attrs": {"name": team},
            "edges": [{"rel": "org:part_of", "target": f"@{local}"}],
        }
        for team in teams
    ]
    return ops


def card(run_cli: CliRunner, ref: str, *options: str) -> dict[str, Any]:
    outcome = app(run_cli, "view", ref, *options, env=ENV)
    assert outcome.code == 0, outcome.envelope
    found: dict[str, Any] = outcome.data
    return found


def section(data: dict[str, Any], kind: str) -> dict[str, Any]:
    found = [s for s in data["sections"] if s["kind"] == kind]
    assert len(found) == 1, [s["kind"] for s in data["sections"]]
    found_section: dict[str, Any] = found[0]
    return found_section


def test_an_entity_with_nothing_around_it_has_its_fields_and_what_applies(
    run_cli: CliRunner, root: Path
) -> None:
    position: dict[str, Any] = {
        "op": "create",
        "type": "org:Position",
        "local": "p",
        "attrs": {"title": "Lead"},
        "edges": [{"rel": "org:in_unit", "target": "@d"}],
    }
    ids = apply(run_cli, [*department("d", "Ops"), position])
    outcome = app(run_cli, "view", ids["p"], env=ENV)
    assert outcome.code == 0 and outcome.warnings == [] and outcome.hints == []
    data = outcome.data
    assert set(data) == {"view", "entity", "sections", "bytes"}
    assert data["view"] == {"name": "card", "origin": "generated"}
    assert data["entity"]["id"] == ids["p"] and data["entity"]["label"] == "Lead"
    assert data["entity"]["type"] == "org:Position" and data["entity"]["record_status"] == "active"
    fields = section(data, "fields")
    assert fields["items"][0] == {"name": "title", "role": "label", "value": "Lead"}
    assert [i["name"] for i in fields["items"] if i.get("role") == "state"] == ["status"]
    rel = section(data, "relations")
    assert [(g["relation"], g["direction"], g["count"]) for g in rel["groups"]] == [
        ("org:in_unit", "out", 1)
    ]
    aff = section(data, "affordances")
    assert aff["detail"].startswith("graph get ") and aff["detail"].endswith(" --affordances")
    assert aff["functions"] == ["reporting_chain"]
    assert aff["transitions"] and aff["actions"]["apply"]
    assert data["bytes"] == len(
        json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert data["bytes"] < LIMIT


def test_a_large_entity_shows_the_first_neighbours_and_hints_at_the_rest(
    run_cli: CliRunner, root: Path
) -> None:
    ids = apply(run_cli, department("d", "Ops", "Zeta", "Alpha", "Echo", "Delta", "Bravo"))
    outcome = app(run_cli, "view", ids["d"], env=ENV)
    assert outcome.code == 0, outcome.envelope
    group = next(g for g in section(outcome.data, "relations")["groups"] if g["direction"] == "in")
    assert (group["relation"], group["count"], group["more"]) == ("org:part_of", 5, 2)
    assert [i["label"] for i in group["items"]] == ["Alpha", "Bravo", "Delta"]
    assert any(
        h["command"] == "graph neighbors DEPT-0001 --graph example_graph --rel org:part_of"
        for h in outcome.hints
    ), outcome.hints
    # the hint is a command that runs and shows the rest
    rest = app(run_cli, "neighbors", ids["d"], "--rel", "org:part_of", env=ENV)
    assert len(rest.data["items"]) == 5


def test_attributes_that_the_card_only_names_get_a_hint_for_get(
    run_cli: CliRunner, root: Path
) -> None:
    ids = apply(
        run_cli,
        [
            {
                "op": "create",
                "type": "ppl:Employee",
                "local": "e",
                "attrs": {"employee_number": "E-1", "name": "Mara", "joined_on": "2020-01-01"},
            }
        ],
    )
    outcome = app(run_cli, "view", ids["e"], env=ENV)
    assert section(outcome.data, "fields")["other"] == ["joined_on"]
    command = "graph get EMP-0001 --graph example_graph --fields joined_on"
    assert [h["command"] for h in outcome.hints] == [command]
    assert app(run_cli, "get", ids["e"], "--fields", "joined_on", env=ENV).code == 0


def test_the_alias_and_the_end_of_the_id_name_the_same_entity(
    run_cli: CliRunner, root: Path
) -> None:
    ids = apply(run_cli, department("d", "Ops"))
    alias = app(run_cli, "get", ids["d"], env=ENV).data["entity"]["alias"]
    by_id = card(run_cli, ids["d"])
    assert card(run_cli, alias) == by_id
    assert card(run_cli, ids["d"].replace("-", "")[-12:]) == by_id
    assert by_id["entity"]["alias"] == alias
    assert section(by_id, "affordances")["detail"] == f"graph get {alias} --affordances"


def test_a_missing_entity_is_not_found_and_rev_is_not_an_option(
    run_cli: CliRunner, root: Path
) -> None:
    missing = app(run_cli, "view", "0" * 16, env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "entity.not_found")
    ids = apply(run_cli, department("d", "Ops"))
    refused = app(run_cli, "view", ids["d"], "--rev", "1", env=ENV)
    assert refused.code == 2 and refused.error["code"] == "usage.invalid_argument"


def test_as_of_is_the_card_of_that_point(run_cli: CliRunner, root: Path) -> None:
    ids = apply(run_cli, department("d", "Ops", "Alpha"))
    before = app(run_cli, "get", ids["d"], env=ENV).envelope["graph"]["seq"]
    apply(
        run_cli,
        [
            {
                "op": "create",
                "type": "org:Team",
                "local": "t",
                "attrs": {"name": "Beta"},
                "edges": [{"rel": "org:part_of", "target": ids["d"]}],
            }
        ],
    )
    now = app(run_cli, "view", ids["d"], env=ENV)
    then = app(run_cli, "view", ids["d"], "--as-of", f"tx:{before}", env=ENV)
    assert then.code == 0, then.envelope

    def count(outcome: Any) -> int:
        groups = section(outcome.data, "relations")["groups"]
        return int(next(g["count"] for g in groups if g["relation"] == "org:part_of"))

    assert (count(now), count(then)) == (2, 1)
    assert then.envelope["graph"]["seq"] == before
    assert "temporal.as_of_read" in then.warning_codes


def test_a_superseded_entity_has_the_warning_the_replacement_and_nothing_applies(
    run_cli: CliRunner, root: Path
) -> None:
    ids = apply(run_cli, department("old", "Old") + department("new", "New"))
    done = app(run_cli, "supersede", ids["new"], ids["old"], "--reason", "replaced", env=ENV)
    assert done.code == 0, done.envelope
    outcome = app(run_cli, "view", ids["old"], env=ENV)
    assert outcome.warning_codes == ["entity.superseded"]
    assert outcome.data["entity"]["record_status"] == "superseded"
    assert outcome.data["entity"]["superseded_by"] == [ids["new"]]
    assert section(outcome.data, "affordances") == {"kind": "affordances"}
    retracted = apply(run_cli, department("gone", "Gone"))
    assert app(run_cli, "retract", retracted["gone"], "--reason", "wrong", env=ENV).code == 0
    outcome = app(run_cli, "view", retracted["gone"], env=ENV)
    assert outcome.warning_codes == ["entity.retracted"]


def test_view_reads_only_and_get_is_unchanged(run_cli: CliRunner, root: Path) -> None:
    ids = apply(run_cli, department("d", "Ops", "Alpha"))
    before = app(run_cli, "get", ids["d"], "--full", env=ENV)
    seq = before.envelope["graph"]["seq"]
    for _ in range(2):
        assert app(run_cli, "view", ids["d"], env=ENV).code == 0
    after = app(run_cli, "get", ids["d"], "--full", env=ENV)
    assert after.envelope["graph"]["seq"] == seq
    assert after.data == before.data
    assert set(after.data) == {"entity", "current_rev", "superseded_by", "retracted_by"}


def test_the_text_form_is_shorter_than_the_json_and_reads_the_same_card(
    run_cli: CliRunner, root: Path
) -> None:
    ids = apply(run_cli, department("d", "Ops", "Zeta", "Alpha", "Echo", "Delta", "Bravo"))
    alias = app(run_cli, "get", ids["d"], env=ENV).data["entity"]["alias"]
    plain = run_cli("graph", "view", ids["d"], env=ENV)
    assert plain.returncode == 0, plain.stderr
    lines = plain.stdout.splitlines()
    assert lines[0] == f"{alias}  Ops  (org:Department, rev 1, active)"
    assert "  name: Ops" in lines
    assert any(
        line.startswith("org:part_of <-   5  first 3 by name: ")
        and "Alpha" in line
        and line.endswith("(+2)")
        for line in lines
    ), lines
    assert any(line.startswith("can: run ") for line in lines), lines
    assert lines[-1] == f"detail: graph get {alias} --affordances"
    assert "hint: " in plain.stderr and "--rel org:part_of" in plain.stderr
    as_json = app(run_cli, "view", ids["d"], env=ENV).data
    assert len(plain.stdout.encode()) < as_json["bytes"]
