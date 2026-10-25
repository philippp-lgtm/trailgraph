"""Compatibility ids: ``catalog.compat`` (plan client_compatibility C1-C3, B14.16)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from harness import REPO_ROOT

from graph_cli.app_cli.commands.skills import app_compat
from graph_cli.catalog import compat
from graph_cli.catalog.build import BUILD_RE
from graph_cli.catalog.compat import cached_compat, compat_id

BUILD_A = "sha256:" + "a" * 64
INVENTORY = {"schema": "graph_cli.inventory/1", "commands": [{"path": ["get"]}]}
GUIDANCE = {"graph-cli": {"SKILL.md": b"# skill\n"}}


def test_the_id_covers_wire_level_inventory_and_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    first = compat_id(INVENTORY, GUIDANCE)
    assert BUILD_RE.match(first) and first == compat_id(dict(INVENTORY), dict(GUIDANCE))
    assert compat_id({**INVENTORY, "commands": []}, GUIDANCE) != first
    assert compat_id(INVENTORY, {"graph-cli": {"SKILL.md": b"# other\n"}}) != first
    assert compat_id(INVENTORY, {"graph-cli": {**GUIDANCE["graph-cli"], "x.md": b""}}) != first
    monkeypatch.setattr(compat, "WIRE_LEVEL", compat.WIRE_LEVEL + 1)
    assert compat_id(INVENTORY, GUIDANCE) != first


def test_the_cache_is_keyed_by_the_build(tmp_path: Path) -> None:
    calls: list[str] = []

    def compute() -> str:
        calls.append("x")
        return "sha256:" + "c" * 64

    assert cached_compat(tmp_path, "graph", None, compute) is None  # no build, no id
    assert cached_compat(tmp_path, "graph", BUILD_A, compute) == "sha256:" + "c" * 64
    assert cached_compat(tmp_path, "graph", BUILD_A, compute) == "sha256:" + "c" * 64
    assert len(calls) == 1
    cache = tmp_path / "compat" / "graph" / ("a" * 64)
    assert cache.read_text() == "sha256:" + "c" * 64 + "\n"
    assert [p.name for p in cache.parent.iterdir()] == ["a" * 64]  # no temp file left
    cache.write_text("garbage\n")  # an invalid cache is computed again and replaced
    assert cached_compat(tmp_path, "graph", BUILD_A, compute) == "sha256:" + "c" * 64
    assert len(calls) == 2 and cache.read_text().startswith("sha256:c")
    # another program or build is another file; no state root, no cache
    cached_compat(tmp_path, "graph-admin", BUILD_A, compute)
    cached_compat(None, "graph", BUILD_A, compute)
    assert len(calls) == 4


def test_a_cache_that_cannot_be_written_costs_a_compute(tmp_path: Path) -> None:
    blocked = tmp_path / "state"
    blocked.write_text("a file, not a directory")
    assert cached_compat(blocked, "graph", BUILD_A, lambda: BUILD_A) == BUILD_A


def test_app_compat_is_the_programs_own() -> None:
    assert BUILD_RE.match(app_compat()) and app_compat() == app_compat()


COMPUTE = "from graph_cli.app_cli.commands.skills import app_compat; print(app_compat())"


def _compat_of(packages: Path) -> str:
    """``app_compat`` of a copied source tree, in a fresh interpreter."""
    done = subprocess.run(
        [sys.executable, "-c", COMPUTE],
        env={"PYTHONPATH": str(packages / "graph_cli" / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


def test_only_a_change_of_the_surface_changes_the_id(tmp_path: Path) -> None:
    """C1: a change that only the server runs keeps the id; a skill 1 text change does not."""
    packages = tmp_path / "packages"
    shutil.copytree(
        REPO_ROOT / "packages" / "graph_cli",
        packages / "graph_cli",
        ignore=shutil.ignore_patterns("__pycache__", "_build_id"),
    )
    source = packages / "graph_cli" / "src" / "graph_cli"
    first = _compat_of(packages)
    assert first == app_compat()
    activity = source / "catalog" / "activity.py"
    activity.write_text(activity.read_text() + "\n# a server-side change\n")
    assert _compat_of(packages) == first
    skill = source / "skills" / "app_skill.py"
    text = skill.read_text()
    marker = "never install or change the program yourself."
    assert marker in text
    skill.write_text(text.replace(marker, marker + " Changed.", 1))
    assert _compat_of(packages) != first
