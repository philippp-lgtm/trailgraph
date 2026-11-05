"""S: ``graph-admin keys verify|gc|backup|rotate-kek`` through both programs (plan T16, step 7).

A sealed graph gets an entity through ``graph``; its key store verifies clean, is
backed up, and is rotated to a new key-encryption key named by
``GRAPH_CLI_KEK_FILE`` with the earlier one in ``[keys] previous_kek_file``;
afterwards ``graph`` reads the entity with the new key alone.
"""

import os
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph

pytestmark = pytest.mark.slow

NEW_KEK_HEX = bytes(range(100, 132)).hex()


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    return root


def test_keys_are_verified_backed_up_and_rotated(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    created = app(
        run_cli, "create", "nas:Topic", "--set", "label=a topic", "--reason", "why", env=ENV
    )
    assert created.code == 0, created.envelope
    entity = created.data["id"]

    verified = admin(run_cli, "keys", "verify", "--graph", "example_graph")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}
    old_kek_id = verified.data["kek_id"]
    gc = admin(run_cli, "keys", "gc", "--graph", "example_graph", "--dry-run")
    assert gc.code == 0, gc.envelope
    assert (gc.data["keys_removed"], gc.data["removed"]) == ([], False)

    target = tmp_path / "key-backups"
    backup = admin(run_cli, "keys", "backup", "--to", str(target))
    assert backup.code == 0, backup.envelope
    (copy,) = backup.data["graphs"]
    assert copy["name"] == "example_graph" and Path(copy["file"]).is_file()
    again = admin(run_cli, "keys", "backup", "--to", str(target))
    assert again.error["code"] == "usage.invalid_argument"

    config_dir = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli"
    new_kek = tmp_path / "secrets" / "kek-new"
    new_kek.parent.mkdir(mode=0o700)
    fd = os.open(new_kek, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(NEW_KEK_HEX + "\n")
    config = config_dir / "config.toml"
    with config.open("a") as handle:
        handle.write(f'\n[keys]\nprevious_kek_file = "{config_dir / "kek"}"\n')
    rotating = {"GRAPH_CLI_KEK_FILE": str(new_kek)}
    stale = admin(run_cli, "keys", "verify", "--graph", "example_graph", env=rotating)
    assert stale.code == 0, stale.envelope
    assert [f["code"] for f in stale.data["findings"]] == ["integrity.keys.kek_not_current"]
    rotated = admin(run_cli, "keys", "rotate-kek", "--reason", "yearly", env=rotating)
    assert rotated.code == 0, rotated.envelope
    (graph,) = rotated.data["graphs"]
    assert (graph["rotated"], graph["rewrapped"], graph["retired"]) == (True, 2, [old_kek_id])

    config.write_text(config.read_text().replace("previous_kek_file", "# previous_kek_file"))
    got = app(run_cli, "get", entity, env={**ENV, **rotating})
    assert got.code == 0, got.envelope
    assert got.data["entity"]["attrs"] == {"label": "a topic"}
    clean = admin(run_cli, "audit", "verify", env=rotating)
    assert clean.code == 0, clean.envelope
    assert admin(run_cli, "keys", "verify", "--graph", "example_graph", env=rotating).data[
        "counts"
    ] == {"error": 0, "warning": 0, "info": 0}
