"""Static source rules that import-linter cannot express (contracts ID.6, T.3, B2.2, B3.1).

Each rule scans the AST of every module in every distribution and has a
positive control, so a broken scanner cannot pass silently.
"""

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from harness import REPO_ROOT

SRC_ROOTS = [
    REPO_ROOT / "packages" / name / "src"
    for name in ("graph_cli", "graph_cli_admin", "graph_cli_server")
]

UUID_MINTING = {"uuid1", "uuid4", "uuid6", "uuid7", "uuid8"}
WALL_CLOCK = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "localtime"),
    ("time", "gmtime"),
    ("time", "ctime"),
}
ENV_AND_OS = {
    ("os", "environ"),
    ("os", "environb"),
    ("os", "getenv"),
    ("os", "getenvb"),
    ("os", "putenv"),
    ("os", "getuid"),
    ("os", "geteuid"),
    ("os", "getpid"),
    ("os", "getlogin"),
    ("os", "uname"),
    ("socket", "gethostname"),
    ("platform", "node"),
    ("getpass", "getuser"),
    ("pwd", "getpwuid"),
}

WALL_CLOCK_ALLOWED = {"graph_cli.kernel.clock"}
COMMIT_ALLOWED = ("graph_cli.services.write", "graph_cli.storage")
MIGRATE_ALLOWED = (
    "graph_cli.services",
    "graph_cli.storage",
    "graph_cli_admin.services_admin.release",
)
ENV_ALLOWED_PREFIXES = ("graph_cli.catalog", "graph_cli_admin.maintenance_cli.wiring")


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for root in SRC_ROOTS:
        for path in sorted(root.rglob("*.py")):
            name = ".".join(path.relative_to(root).with_suffix("").parts)
            yield name.removesuffix(".__init__"), ast.parse(path.read_text(), str(path))


def _qualified(node: ast.AST) -> tuple[str, str] | None:
    """``module.attr`` accesses such as ``os.environ`` or ``datetime.datetime.now``."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            return node.value.id, node.attr
        if isinstance(node.value, ast.Attribute):
            return node.value.attr, node.attr
    return None


def _uuid_minting(tree: ast.Module) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "uuid":
            hits += [a.name for a in node.names if a.name in UUID_MINTING]
        if isinstance(node, ast.Attribute) and node.attr in UUID_MINTING:
            hits.append(node.attr)
    return hits


def _forbidden_access(tree: ast.Module, forbidden: set[tuple[str, str]]) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        pair = _qualified(node)
        if pair in forbidden:
            hits.append(".".join(pair))
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module.rsplit(".", 1)[-1]
            hits += [f"{module}.{a.name}" for a in node.names if (module, a.name) in forbidden]
    return hits


def _commit_calls(tree: ast.Module) -> list[str]:
    return [
        "commit"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]


def _migrate_calls(tree: ast.Module) -> list[str]:
    return [
        "migrate"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "migrate"
    ]


def _violations(
    check: Callable[[ast.Module], list[str]], allowed: Callable[[str], bool]
) -> dict[str, list[str]]:
    found = {}
    for name, tree in _modules():
        hits = check(tree)
        if hits and not allowed(name):
            found[name] = hits
    return found


def test_scanner_finds_modules() -> None:
    names = {name for name, _ in _modules()}
    assert {"graph_cli.kernel.ids", "graph_cli.kernel.clock"} <= names


def test_no_stdlib_uuid_minting() -> None:
    """ID.6: ids are minted by kernel.ids.Uuid7Provider only."""
    assert _violations(_uuid_minting, lambda name: False) == {}


def test_wall_clock_only_in_kernel_clock() -> None:
    """T.3: all time comes from the injected Clock."""
    found = _violations(lambda t: _forbidden_access(t, WALL_CLOCK), WALL_CLOCK_ALLOWED.__contains__)
    assert found == {}


def test_env_and_os_identity_only_in_wiring() -> None:
    """B2.2: only catalog and the admin wiring read env, uid, pid or hostname."""
    found = _violations(
        lambda t: _forbidden_access(t, ENV_AND_OS), lambda n: n.startswith(ENV_ALLOWED_PREFIXES)
    )
    assert found == {}


def test_only_the_write_service_commits() -> None:
    """B3.1: WriteService.apply is the only caller of WriteSession.commit in graph_cli."""
    found = _violations(_commit_calls, lambda n: n.startswith(COMMIT_ALLOWED))
    assert found == {}
    assert _commit_calls(ast.parse("session.commit(tx, receipt)"))


def _catalog_lookups(tree: ast.Module) -> list[str]:
    """``<x>.catalog.resolve(...)`` and ``<x>.catalog.manifests(...)``."""
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("resolve", "manifests")
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "catalog"
    ]


def test_admin_services_find_graphs_through_the_context() -> None:
    """B14.20: a project admin sees only the graphs it maintains; ``AdminContext.resolve``
    and ``manifests`` are the only way the admin side finds a graph."""
    found = _violations(
        _catalog_lookups,
        lambda n: (
            not n.startswith("graph_cli_admin.") or n == "graph_cli_admin.services_admin.context"
        ),
    )
    assert found == {}
    assert _catalog_lookups(ast.parse("ctx.catalog.resolve(ref)"))


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import uuid\nuuid.uuid4()", "uuid"),
        ("from uuid import uuid7", "uuid"),
        ("import datetime\ndatetime.datetime.now()", "clock"),
        ("from datetime import datetime\ndatetime.utcnow()", "clock"),
        ("from time import time", "clock"),
        ("import os\nos.environ['X']", "env"),
        ("from os import getenv", "env"),
        ("import socket\nsocket.gethostname()", "env"),
        ("import pwd\npwd.getpwuid(0)", "env"),
    ],
)
def test_positive_controls(source: str, rule: str) -> None:
    tree = ast.parse(source)
    if rule == "uuid":
        assert _uuid_minting(tree)
    elif rule == "clock":
        assert _forbidden_access(tree, WALL_CLOCK)
    else:
        assert _forbidden_access(tree, ENV_AND_OS)


def test_clock_port_calls_are_allowed() -> None:
    tree = ast.parse("def f(clock):\n    return clock.now()")
    assert _forbidden_access(tree, WALL_CLOCK) == []


def test_sources_exist() -> None:
    assert all(Path(root).is_dir() for root in SRC_ROOTS)


def test_only_services_and_the_release_migrate() -> None:
    """Plan M4: a migration tx is committed by ``MaintenanceSession.migrate`` only, called
    from the migration service and the release (graph_cli_admin) that builds it."""
    found = _violations(_migrate_calls, lambda n: n.startswith(MIGRATE_ALLOWED))
    assert found == {}
    assert _migrate_calls(ast.parse("session.migrate(tx, receipt)"))
