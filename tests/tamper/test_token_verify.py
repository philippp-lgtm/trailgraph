"""T: the token file against the token events (contracts B14.11, B5.12, R.6).

Tokens are created and revoked with the real ``graph-admin token`` commands;
the token file is then edited by hand, as anyone with write access to the
data root could, to add a token the server would accept.
"""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.catalog.tokens import new_token, token_hash
from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.storage.files.token_files import tokens_path


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def codes(run_cli: CliRunner) -> list[str]:
    outcome = admin(run_cli, "audit", "verify")
    if outcome.code == 0:
        return [f["code"] for f in outcome.data["findings"]]
    return [f["code"] for f in outcome.error["details"]["findings"]]


def create(run_cli: CliRunner, name: str, *args: str) -> None:
    assert admin(run_cli, "token", "create", name, *args).code == 0


def edit(root: Path, change: object) -> None:
    path = tokens_path(root)
    document = json.loads(path.read_bytes())
    change(document)  # type: ignore[operator]
    path.write_bytes(pretty_bytes(document))


def test_created_and_revoked_tokens_verify_clean(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "ci", "--project", "example_project", "--expires", "2099-12-31")
    create(run_cli, "web")
    assert admin(run_cli, "token", "revoke", "web").code == 0
    assert codes(run_cli) == []


def test_a_token_added_by_hand_is_a_mismatch(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "ci")
    forged = {"name": "forged", "hash": token_hash(new_token()), "project": None}
    edit(root, lambda d: d["tokens"].append(forged))
    assert codes(run_cli) == [audit_codes.TOKEN_MISMATCH]


def test_a_widened_token_is_a_mismatch(run_cli: CliRunner, root: Path) -> None:
    """Unbinding a token from its project widens what it may do."""
    create(run_cli, "ci", "--project", "example_project")
    edit(root, lambda d: d["tokens"][0].update(project=None))
    outcome = admin(run_cli, "audit", "verify")
    (finding,) = outcome.error["details"]["findings"]
    assert finding["code"] == audit_codes.TOKEN_MISMATCH
    assert finding["target"]["name"] == "ci"
    assert finding["expected"]["project"] == "example_project"


def test_a_revoked_token_put_back_is_a_mismatch(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "ci")
    kept = json.loads(tokens_path(root).read_bytes())["tokens"]
    assert admin(run_cli, "token", "revoke", "ci").code == 0
    edit(root, lambda d: d.update(tokens=kept))  # what an interrupted revoke leaves
    assert codes(run_cli) == [audit_codes.TOKEN_MISMATCH]
    assert admin(run_cli, "token", "revoke", "ci").code == 0  # revoking again repairs it
    assert codes(run_cli) == []


def test_a_token_missing_from_the_file_is_an_interrupted_create(
    run_cli: CliRunner, root: Path
) -> None:
    create(run_cli, "ci")
    edit(root, lambda d: d.update(tokens=[]))
    assert codes(run_cli) == [audit_codes.INTENT_WITHOUT_EFFECT]


def test_an_unreadable_token_file_is_a_mismatch(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "ci")
    tokens_path(root).write_text("not json")
    assert codes(run_cli) == [audit_codes.TOKEN_MISMATCH]
