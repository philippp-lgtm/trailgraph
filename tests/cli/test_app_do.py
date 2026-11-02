"""S: ``graph do`` over a graph from the company example (actions plan A5-A7): a dry run, the
write, the audit and history naming the action, refusals, usage errors, the text form and the
access log."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.actions import VALID
from support.admin import Outcome, admin, init
from support.app import app
from support.fixtures import EXAMPLES, example_model
from support.services import OpsFactory

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
ENV = {"GRAPH_CLI_GRAPH": "company"}
MARKER = "do-marker-7d2a"
FACTORY = OpsFactory(example_model("company", "1.1.0"))  # numbers values, so names stay distinct


def do(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    return app(run_cli, "do", *args, env=env or ENV)


def create(run_cli: CliRunner, cls: str, local: str) -> str:
    """One entity of the class (and what it requires), through ``apply``."""
    ops = FACTORY.create(cls, local)
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    entities = applied.data["results"][0]["result"]["entities"]
    return str(next(e["id"] for e in entities if e["local"] == local))


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "company"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    published = admin(run_cli, "graph", "actions", "publish", "company", str(VALID))
    assert published.code == 0, published.envelope
    return root


def assign(run_cli: CliRunner, person: str, position: str, *extra: str) -> Outcome:
    return do(
        run_cli,
        "assign_position",
        *("--param", f"person={person}", "--param", f"position={position}"),
        *("--param", "since=2026-03-01", *extra),
    )


def test_a_dry_run_then_the_write(run_cli: CliRunner, root: Path) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "pos")
    dry = assign(run_cli, person, position, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert dry.data["dry_run"] is True and dry.data["seq"] is None and dry.data["tx_id"] is None
    action = dry.data["action"]
    assert action["name"] == "assign_position" and action["hash"].startswith("sha256:")
    assert [(e["id"], e["ops"]) for e in dry.data["entities"]] == [(person, ["link"])]
    assert dry.envelope["graph"]["seq"] is None  # nothing was written
    written = assign(run_cli, person, position, "--reason", "new recruiter")
    assert written.code == 0, written.envelope
    assert written.data["action"] == action and written.data["dry_run"] is False
    assert written.envelope["graph"]["seq"] == written.data["seq"] > 0
    history = app(run_cli, "history", person, env=ENV)
    (item,) = [i for i in history.data["items"] if i["tx_seq"] == written.data["seq"]]
    assert (
        item["command"] == "do" and item["action"] == action and item["reason"] == "new recruiter"
    )
    created = history.data["items"][-1]
    assert "action" not in created  # additive: only writes by an action carry the key
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"]["error"] == 0
    graph_id = admin(run_cli, "graph", "show", "company").data["id"]
    events = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    ).events
    intent = events[-1]
    assert intent["request"]["command"] == "do" and intent["request"]["action"] == action  # type: ignore[index]


def test_a_refused_precondition_names_it_and_the_hint_shows_the_action(
    run_cli: CliRunner, root: Path
) -> None:
    first, second = create(run_cli, "ppl:Employee", "a"), create(run_cli, "ppl:Employee", "b")
    position = create(run_cli, "org:Position", "pos")
    assert assign(run_cli, first, position).code == 0
    refused = assign(run_cli, second, position)
    assert (refused.code, refused.error["code"]) == (1, "action.precondition_failed")
    (failed,) = refused.error["details"]["failed"]
    assert failed["precondition"] == "position_vacant" and failed["entity"] == position
    assert refused.error["hint"] == "graph describe action assign_position"


def test_the_idempotency_key_replays_the_recorded_result(run_cli: CliRunner, root: Path) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "pos")
    first = assign(run_cli, person, position, "--idempotency-key", "k-1")
    again = assign(run_cli, person, position, "--idempotency-key", "k-1")
    assert again.code == 0, again.envelope
    assert again.data["idempotent_replay"] is True and again.data["tx_id"] == first.data["tx_id"]
    other = create(run_cli, "ppl:Employee", "q")
    reused = assign(run_cli, other, position, "--idempotency-key", "k-1")
    assert (reused.code, reused.error["code"]) == (4, "conflict.idempotency_key_reuse")


def test_usage_errors(run_cli: CliRunner, root: Path) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "pos")
    missing = do(run_cli, "assign_position", "--param", f"person={person}")
    assert (missing.code, missing.error["code"]) == (2, "action.parameter_missing")
    unknown = assign(run_cli, person, position, "--param", "x=1")
    assert (unknown.code, unknown.error["code"]) == (2, "action.parameter_unknown")
    bad = do(
        run_cli,
        "assign_position",
        "--param",
        f"person={person}",
        "--param",
        f"position={position}",
        "--param",
        "since=soon",
    )
    assert (bad.code, bad.error["code"]) == (2, "action.parameter_invalid")
    name = do(
        run_cli,
        "assign_position",
        "--param",
        "person=Alex Example",
        "--param",
        f"position={position}",
        "--param",
        "since=2026-03-01",
    )
    assert name.code == 2 and name.error["code"] == "action.parameter_invalid"
    assert "lookup" in str(name.error["hint"])
    wrong = assign(run_cli, position, position)
    assert (wrong.code, wrong.error["code"]) == (1, "action.parameter_type")
    assert wrong.error["hint"] == "graph lookup <text> --for assign_position.person"
    no_equals = do(run_cli, "assign_position", "--param", "person")
    assert (no_equals.code, no_equals.error["code"]) == (2, "usage.invalid_argument")
    key = assign(run_cli, person, position, "--idempotency-key", "bad key")
    assert (key.code, key.error["code"]) == (2, "usage.invalid_argument")
    empty = assign(run_cli, person, position, "--reason", " ")
    assert (empty.code, empty.error["code"]) == (2, "usage.invalid_argument")
    employee = create(run_cli, "ppl:Employee", "e")
    needs_reason = do(run_cli, "offboard_employee", "--param", f"employee={employee}")
    assert (needs_reason.code, needs_reason.error["code"]) == (2, "action.reason_required")
    absent = do(run_cli, "assign_postion", "--param", f"person={person}")
    assert (absent.code, absent.error["code"]) == (1, "action.not_found")
    assert absent.error["details"]["did_you_mean"][0] == "assign_position"
    assert absent.error["hint"] == "graph describe search <words> --kind action"
    graph_missing = do(run_cli, "assign_position", env={"GRAPH_CLI_GRAPH": "nowhere"})
    assert graph_missing.error["code"] == "graph.not_found"


def test_a_graph_without_actions_has_none(run_cli: CliRunner, root: Path) -> None:
    created = admin(
        run_cli,
        *("graph", "create", "plain"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0
    plain = do(run_cli, "assign_position", env={"GRAPH_CLI_GRAPH": "plain"})
    assert (plain.code, plain.error["code"]) == (1, "action.not_found")
    assert plain.error["details"]["did_you_mean"] == []


def test_the_text_form(run_cli: CliRunner, root: Path) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "pos")
    text = run_cli(
        "graph",
        *("do", "assign_position", "--dry-run"),
        *("--param", f"person={person}", "--param", f"position={position}"),
        *("--param", "since=2026-03-01"),
        env=ENV,
    )
    assert text.returncode == 0, text.stderr
    assert text.stderr == "" and "dry_run: true" in text.stdout
    assert "action:\n  name: assign_position\n  hash: sha256:" in text.stdout


def enable_logs(env: dict[str, str]) -> Path:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    return Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"


def test_the_access_log_names_the_action_and_no_value(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    log = enable_logs(isolated_env)
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "pos")
    other = create(run_cli, "ppl:Employee", "q")
    ok = assign(run_cli, person, position, "--reason", MARKER)
    again = assign(run_cli, other, position)
    assert again.error["code"] == "action.precondition_failed"
    unknown = do(run_cli, f"{MARKER}_action")
    assert unknown.error["code"] == "action.not_found"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    ran, refused, absent = lines[-3:]
    assert ran["command"] == "do" and ran["result"] == {"status": "ok"}
    assert ran["action"] == ok.data["action"]
    assert refused["action"] == {"name": "assign_position", "hash": None}
    assert refused["result"]["code"] == "action.precondition_failed"
    assert "action" not in absent
    for value in (person, position, "2026-03-01", MARKER):
        assert value not in log.read_text(), value
