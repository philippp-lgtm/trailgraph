"""S/K/P: the P1 exit criteria through ``graph`` subprocesses (plan A8; contracts B3.4,
B3.12, B4.2, B5.10, §4.1).

- A write with a failing audit sink is refused: ``test_failing_audit_sink_refuses_the_write``.
- kill -9 mid-write recovers: ``test_kill_mid_write_then_cli_retry_commits_once``.
- Concurrent processes lose no writes: ``test_concurrent_graph_creates_lose_nothing``.
- The app dist contains no admin code: ``tests/test_architecture.py``
  (``test_app_wheel_contains_no_admin_code_or_fixtures``) and the inventory
  leak test in ``tests/cli/test_app_meta.py``.

Core tests run on both toy ontologies elsewhere; these use one fixture per
criterion where the property does not depend on the ontology.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from harness import BIN_DIR, CliRunner
from support.admin import admin, init
from support.app import ENV, app, cli_create, create_args, create_graph, prepared_create
from support.fixtures import TOY_ONTOLOGIES, compiled_model
from support.procs import spawn
from support.roles import Roles, load_roles
from support.services import OpsFactory
from support.trees import tree_hash

from graph_cli.storage.common.faults import CrashPoint

pytestmark = pytest.mark.slow

KEY = "k-cli-crash"
COMMITTED = {CrashPoint.TX_AFTER_SYNC, CrashPoint.HEAD_BEFORE_RENAME}
POINTS = [
    CrashPoint.AUDIT_BEFORE_WRITE,
    CrashPoint.TX_BEFORE_WRITE,
    *sorted(COMMITTED),
]
WORKERS = 8


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


def graph_dir(root: Path) -> Path:
    (found,) = list((root / "graphs").iterdir())
    return found


def head_seq(run_cli: CliRunner) -> int:
    shown = admin(run_cli, "graph", "show", "example_graph")
    assert shown.code == 0, shown.envelope
    seq: int = shown.envelope["graph"]["seq"]
    return seq


def test_failing_audit_sink_refuses_the_write(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    op = prepared_create(run_cli, factory, roles.term("lifecycle_type"))
    base = head_seq(run_cli)
    audit = graph_dir(root) / "audit"
    modes = {path: path.stat().st_mode for path in [audit, *audit.iterdir()]}
    for path, mode in modes.items():
        path.chmod(stat.S_IMODE(mode) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        refused = app(run_cli, *create_args(op, {}), env=ENV)
    finally:
        for path, mode in modes.items():
            path.chmod(stat.S_IMODE(mode))
    assert (refused.code, refused.error["code"]) == (5, "audit.unavailable")
    assert head_seq(run_cli) == base  # no tx
    done = app(run_cli, *create_args(op, {}), env=ENV)
    assert done.code == 0 and done.data["seq"] == base + 1
    assert admin(run_cli, "audit", "verify").code == 0


@pytest.mark.parametrize("point", POINTS, ids=str)
def test_kill_mid_write_then_cli_retry_commits_once(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory, point: CrashPoint
) -> None:
    cls = roles.term("lifecycle_type")
    cli_create(run_cli, factory, cls, "base")
    op = prepared_create(run_cli, factory, cls)
    base = head_seq(run_cli)
    child = spawn(
        "write",
        {
            "root": str(root),
            "graph": "example_graph",
            "documents": [{"ops": [op], "idempotency_key": KEY}],
            "point": point.value,
            "uid": os.getuid(),
        },
    )
    _, err = child.communicate(timeout=60)
    assert child.returncode == -9, err
    retry = app(run_cli, *create_args(op, {}), "--idempotency-key", KEY, env=ENV)
    assert retry.code == 0, retry.envelope
    assert retry.data["idempotent_replay"] is (point in COMMITTED)
    assert retry.data["seq"] == base + 1 and head_seq(run_cli) == base + 1
    again = app(run_cli, *create_args(op, {}), "--idempotency-key", KEY, env=ENV)
    assert again.data["idempotent_replay"] is True and head_seq(run_cli) == base + 1
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0


def test_concurrent_graph_creates_lose_nothing(
    run_cli: CliRunner,
    root: Path,
    roles: Roles,
    factory: OpsFactory,
    isolated_env: dict[str, str],
    tmp_path: Path,
) -> None:
    cls = roles.term("lifecycle_type")
    op = prepared_create(run_cli, factory, cls)
    base = head_seq(run_cli)
    ops = [{**op, "attrs": factory.attrs(cls)} for _ in range(WORKERS)]  # unique values differ
    procs = [
        subprocess.Popen(
            [str(BIN_DIR / "graph"), *create_args(each, {}), "--json"],
            env={**isolated_env, **ENV},
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for each in ops
    ]
    outcomes = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, (out, err)
        outcomes.append(json.loads(out)["data"])
    assert sorted(o["seq"] for o in outcomes) == list(range(base + 1, base + WORKERS + 1))
    assert head_seq(run_cli) == base + WORKERS
    aliases = [e["alias"] for o in outcomes for e in o["entities"] if e["alias"]]
    if aliases:  # B1.8: contiguous and unique
        numbers = sorted(int(a.rpartition("-")[2]) for a in aliases)
        assert numbers == list(range(numbers[0], numbers[0] + WORKERS))
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"]["error"] == 0


def test_reads_help_dry_runs_and_usage_errors_change_nothing(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    """B5.10: only writes and recorded maintenance touch the tree, audit files included."""
    cls = roles.term("lifecycle_type")
    entity = cli_create(run_cli, factory, cls)["x"]
    op = prepared_create(run_cli, factory, cls)
    before = tree_hash(root)
    for args in (
        ("--help",),
        ("create", "--help"),
        ("whoami",),
        ("graphs",),
        ("describe", "types"),
        ("describe", "commands"),
        ("get", entity),
        ("find", "--type", cls),
        ("neighbors", entity),
        (*create_args(op, {}), "--dry-run"),
    ):
        result = run_cli("graph", *args, env=ENV)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    dry = run_cli("graph", "apply", "-", "--dry-run", env=ENV, input=json.dumps({"ops": [op]}))
    assert dry.returncode == 0, dry.stdout
    for args in (
        ("create",),
        ("update", entity, "--unset", "no_such_attribute", "--dry-run"),
        ("find", "--limit", "100000"),
        ("get", entity, "--as-of", "tx:1"),
        ("apply", "-"),
        ("no_such_command",),
    ):
        result = run_cli("graph", *args, env=ENV, input="{broken")
        assert result.returncode in (1, 2), (args, result.stdout, result.stderr)
    assert tree_hash(root) == before
