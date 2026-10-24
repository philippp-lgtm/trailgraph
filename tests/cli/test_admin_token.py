"""S: ``graph-admin token create|list|revoke`` (contracts B14.11).

The token is printed once and stored only as a hash; events carry a prefix of
the hash. A token the installed policy grants nothing gets a warning.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.policies import write_policy

from graph_cli.catalog.tokens import TokenStore, token_hash
from graph_cli.storage.files.token_files import tokens_path

pytestmark = pytest.mark.slow

GRANT = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
  - {entrypoint: server, principal: {id: "token:ci"}, graphs: "*", level: write}
no_project: {"*": write}
"""


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    return root


def installation_events(root: Path) -> list[dict]:  # type: ignore[type-arg]
    lines: list[dict] = []  # type: ignore[type-arg]
    for segment in sorted((root / "audit").rglob("*.jsonl")):
        lines += [json.loads(line) for line in segment.read_text().splitlines()]
    return lines


def test_create_list_revoke(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    created = admin(
        run_cli, "token", "create", "ci", "--project", "example_project", "--reason", "for ci"
    )
    assert created.code == 0, created.envelope
    secret = created.data["token"]
    assert secret.startswith("gct_")
    assert created.data["project"] == "example_project"
    assert created.warning_codes == ["admin.token_not_granted"]  # the built-in policy

    stored = tokens_path(root).read_text()
    assert secret not in stored and token_hash(secret) in stored
    found = TokenStore(root).lookup(secret, datetime.now(UTC))
    assert found is not None and found.project == "example_project"

    events = [e for e in installation_events(root) if e["actions"] == ["maint.token_create"]]
    (event,) = events
    assert event["change"]["token"] == {
        "name": "ci",
        "hash_prefix": token_hash(secret)[:23],
        "project": "example_project",
        "expires": None,
    }
    assert event["reason"] == "for ci"
    for path in root.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path

    listed = admin(run_cli, "token", "list")
    assert [t["name"] for t in listed.data["tokens"]] == ["ci"]
    assert "token" not in listed.data["tokens"][0]
    assert listed.data["tokens"][0]["expired"] is False

    again = admin(run_cli, "token", "create", "ci")
    assert again.code == 1 and again.error["code"] == "admin.token_exists"
    assert again.error["hint"] == "graph-admin token list"

    revoked = admin(run_cli, "token", "revoke", "ci")
    assert revoked.code == 0, revoked.envelope
    assert TokenStore(root).lookup(secret, datetime.now(UTC)) is None
    assert admin(run_cli, "token", "list").data["tokens"] == []
    gone = admin(run_cli, "token", "revoke", "ci")
    assert gone.code == 1 and gone.error["code"] == "admin.token_not_found"
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_granted_token_does_not_warn(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    policy = write_policy(tmp_path, "installation.yaml", GRANT)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "grant").code == 0
    assert admin(run_cli, "token", "create", "ci").warning_codes == []
    assert admin(run_cli, "token", "create", "other").warning_codes == ["admin.token_not_granted"]


@pytest.mark.parametrize(
    ("args", "argument"),
    [
        (("Bad Name",), "name"),
        (("ci", "--project", "-x"), "--project"),
        (("ci", "--expires", "2020-01-01"), "--expires"),
        (("ci", "--expires", "tomorrow"), "--expires"),
    ],
)
def test_refusals(run_cli: CliRunner, root: Path, args: tuple[str, ...], argument: str) -> None:
    outcome = admin(run_cli, "token", "create", *args)
    assert outcome.code == 2
    assert outcome.error["details"]["argument"] == argument
    assert not tokens_path(root).exists()


def test_text_mode_shows_the_token_once(run_cli: CliRunner, root: Path) -> None:
    result = run_cli("graph-admin", "token", "create", "ci")
    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("gct_")]
    assert TokenStore(root).lookup(line, datetime.now(UTC)) is not None
