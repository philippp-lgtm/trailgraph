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


@pytest.mark.doc_claim("security_model.transport")
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


# ---- roles (B6.7, B14.11) ---------------------------------------------------------------

ROLES = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps"}
  importer: {description: "Importers"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: "*", level: read}
no_project: {"*": read}
"""


@pytest.fixture
def with_roles(run_cli: CliRunner, root: Path, tmp_path: Path) -> Path:
    policy = write_policy(tmp_path, "installation.yaml", ROLES)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "roles").code == 0
    return root


def token_events(root: Path, action: str) -> list[dict]:  # type: ignore[type-arg]
    return [e for e in installation_events(root) if e["actions"] == [action]]


def test_create_with_roles_list_and_filter(run_cli: CliRunner, with_roles: Path) -> None:
    created = admin(
        run_cli,
        "token",
        "create",
        "ci",
        "--role",
        "reader",
        "--role",
        "importer",
        "--role",
        "reader",
    )
    assert created.code == 0, created.envelope
    assert created.data["roles"] == ["importer", "reader"]  # sorted, unique
    assert created.warning_codes == []  # the policy grants a reader
    assert admin(run_cli, "token", "create", "plain").data["roles"] == []
    event = token_events(with_roles, "maint.token_create")[-2]
    assert event["change"]["token"]["roles"] == ["importer", "reader"]
    assert "roles" not in token_events(with_roles, "maint.token_create")[-1]["change"]["token"]
    assert "roles" in json.loads(tokens_path(with_roles).read_text())["tokens"][0]

    listed = admin(run_cli, "token", "list").data["tokens"]
    assert {t["name"]: t["roles"] for t in listed} == {"ci": ["importer", "reader"], "plain": []}
    only = admin(run_cli, "token", "list", "--role", "importer").data["tokens"]
    assert [t["name"] for t in only] == ["ci"]
    assert admin(run_cli, "token", "list", "--role", "nobody").data["tokens"] == []
    assert admin(run_cli, "token", "list", "--role", "Bad").code == 2
    assert admin(run_cli, "audit", "verify").code == 0


def test_role_add_and_remove_write_the_full_set_and_verify_replays_them(
    run_cli: CliRunner, with_roles: Path
) -> None:
    assert admin(run_cli, "token", "create", "ci", "--role", "reader").code == 0
    added = admin(run_cli, "token", "role", "add", "ci", "importer", "--reason", "now imports")
    assert added.code == 0, added.envelope
    assert added.data["roles"] == ["importer", "reader"] and added.data["changed"] is True
    assert added.data["hash_prefix"].startswith("sha256:")
    *_, event = token_events(with_roles, "maint.token_update")
    assert event["change"]["token"] == {
        "name": "ci",
        "hash_prefix": added.data["hash_prefix"],
        "roles": ["importer", "reader"],
    }
    assert event["reason"] == "now imports"
    assert admin(run_cli, "audit", "verify").code == 0

    removed = admin(run_cli, "token", "role", "remove", "ci", "reader", "importer")
    assert removed.code == 0, removed.envelope
    assert removed.data["roles"] == [] and removed.data["changed"] is True
    assert removed.warning_codes == ["admin.token_not_granted"]  # no role, no grant
    *_, event = token_events(with_roles, "maint.token_update")
    assert event["change"]["token"]["roles"] == []
    assert "roles" not in tokens_path(with_roles).read_text()
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope  # a real replay of maint.token_update
    assert len(token_events(with_roles, "maint.token_update")) == 2


def test_a_change_that_changes_nothing_writes_nothing(run_cli: CliRunner, with_roles: Path) -> None:
    assert admin(run_cli, "token", "create", "ci", "--role", "reader").code == 0
    before = len(installation_events(with_roles))
    again = admin(run_cli, "token", "role", "add", "ci", "reader")
    assert again.code == 0 and again.data["changed"] is False
    assert again.data["roles"] == ["reader"]
    missing = admin(run_cli, "token", "role", "remove", "ci", "importer")
    assert missing.code == 0 and missing.data["changed"] is False
    assert len(installation_events(with_roles)) == before


def test_revoke_is_unaffected_by_roles(run_cli: CliRunner, with_roles: Path) -> None:
    assert admin(run_cli, "token", "create", "ci", "--role", "reader").code == 0
    assert admin(run_cli, "token", "role", "add", "ci", "importer").code == 0
    revoked = admin(run_cli, "token", "revoke", "ci")
    assert revoked.code == 0 and set(revoked.data) == {"name", "revoked", "hash_prefix"}
    assert admin(run_cli, "token", "list").data["tokens"] == []
    assert admin(run_cli, "audit", "verify").code == 0


def test_an_undeclared_role_is_refused_but_can_be_removed(
    run_cli: CliRunner, with_roles: Path, tmp_path: Path
) -> None:
    refused = admin(run_cli, "token", "create", "ci", "--role", "importr")
    assert refused.code == 1 and refused.error["code"] == "admin.role_unknown"
    assert refused.error["details"] == {
        "role": "importr",
        "known": ["importer", "reader"],
        "did_you_mean": ["importer"],
    }
    assert refused.error["hint"] == "graph-admin policy show"
    assert not tokens_path(with_roles).exists()
    assert admin(run_cli, "token", "create", "ci", "--role", "importer").code == 0
    unknown = admin(run_cli, "token", "role", "add", "ci", "nobody")
    assert unknown.error["code"] == "admin.role_unknown"
    # the policy drops `importer`: the token keeps it until it is taken off
    narrowed = write_policy(
        tmp_path / "w",
        "installation.yaml",
        ROLES.replace('  importer: {description: "Importers"}\n', ""),
    )
    assert admin(run_cli, "policy", "apply", str(narrowed), "--reason", "drop").code == 0
    assert admin(run_cli, "token", "role", "add", "ci", "importer").data["changed"] is False
    removed = admin(run_cli, "token", "role", "remove", "ci", "importer")
    assert removed.code == 0 and removed.data["roles"] == []
    assert admin(run_cli, "audit", "verify").code == 0


@pytest.mark.parametrize(
    ("args", "argument"),
    [
        (("token", "create", "ci", "--role", "reader", "--scope", "admin"), "--role"),
        (("token", "create", "ci", "--role", "Reader"), "--role"),
        (("token", "create", "ci", "--role", "gct_" + "a" * 40), "--role"),
        (("token", "create", "ci", *(f"--role=r{i}" for i in range(17))), "--role"),
        (("token", "role", "add", "ci", "Bad Role"), "role"),
        (("token", "role", "add", "gct_" + "a" * 40, "reader"), "name"),
        (("token", "role", "add", "admin1", "reader"), "name"),  # an admin token
    ],
)
def test_role_refusals(
    run_cli: CliRunner, with_roles: Path, args: tuple[str, ...], argument: str
) -> None:
    assert admin(run_cli, "token", "create", "admin1", "--scope", "admin").code == 0
    outcome = admin(run_cli, *args)
    assert outcome.code == 2, outcome.envelope
    assert outcome.error["code"] == "usage.invalid_argument"
    assert outcome.error["details"]["argument"] == argument
    assert "gct_" not in json.dumps(outcome.envelope)
    assert [t["name"] for t in admin(run_cli, "token", "list").data["tokens"]] == ["admin1"]


def test_too_many_roles_after_an_add_is_refused(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    names = [f"r{i:02d}" for i in range(17)]
    declared = "".join(f'  {n}: {{description: "Role"}}\n' for n in names)
    text = ROLES.replace('  reader: {description: "Reporting apps"}\n', declared)
    text = text.replace('  importer: {description: "Importers"}\n', "")
    text = text.replace("principal: {role: reader}", "principal: {role: r00}")
    policy = write_policy(tmp_path, "many.yaml", text)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "many").code == 0
    assert admin(run_cli, "token", "create", "ci", *(f"--role={n}" for n in names[:16])).code == 0
    over = admin(run_cli, "token", "role", "add", "ci", names[16])
    assert over.code == 2 and over.error["details"]["argument"] == "role"
    assert len(token_events(root, "maint.token_update")) == 0
    assert admin(run_cli, "token", "role", "add", "ci", names[0]).data["changed"] is False


def test_an_unknown_token_name_and_the_secret_for_a_name(
    run_cli: CliRunner, with_roles: Path
) -> None:
    gone = admin(run_cli, "token", "role", "add", "nobody", "reader")
    assert gone.code == 1 and gone.error["code"] == "admin.token_not_found"
    assert gone.error["hint"] == "graph-admin token list"
    gone = admin(run_cli, "token", "role", "remove", "nobody", "reader")
    assert gone.error["code"] == "admin.token_not_found"


def test_the_policy_decides_whether_a_role_grants_anything(
    run_cli: CliRunner, with_roles: Path
) -> None:
    created = admin(run_cli, "token", "create", "ci", "--role", "importer")
    assert created.warning_codes == ["admin.token_not_granted"]  # only readers are granted
    added = admin(run_cli, "token", "role", "add", "ci", "reader")
    assert added.warning_codes == []
