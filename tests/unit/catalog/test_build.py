"""Build ids: ``scripts/build_id.py`` and ``catalog.build`` (plan origin_lifecycle E1)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from harness import REPO_ROOT

from graph_cli.catalog.build import build_header, parse_build_header, read_build, short

BUILD_A = "sha256:" + "a" * 64


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_id", REPO_ROOT / "scripts" / "build_id.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_id"] = module
    spec.loader.exec_module(module)
    return module


def workspace(root: Path) -> Path:
    """Two distributions: ``lower`` and ``upper``, which depends on it."""
    for name, dependencies in (("lower", '["click>=8.1"]'), ("upper", '["lower", "x<2"]')):
        package = root / name
        (package / "src" / name).mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\ndependencies = {dependencies}\n'
        )
        (package / "src" / name / "__init__.py").write_text(f'"""{name}."""\n')
    return root


def ids(module: ModuleType, packages: Path) -> dict[str, str]:
    assert module.main(["--packages", str(packages)]) == 0
    return {
        name: (packages / name / "src" / name / "_build_id").read_text().strip()
        for name in ("lower", "upper")
    }


def test_a_build_id_covers_the_sources_and_the_dependencies(tmp_path: Path) -> None:
    module = script()
    packages = workspace(tmp_path)
    first = ids(module, packages)
    assert first == ids(module, packages)  # the same tree, the same ids
    assert first["lower"] != first["upper"]
    # caches, compiled files and dot files are no part of it
    cache = packages / "lower" / "src" / "lower" / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-313.pyc").write_bytes(b"\0")
    (packages / "lower" / "src" / "lower" / ".DS_Store").write_bytes(b"\0")
    assert ids(module, packages) == first
    (packages / "upper" / "src" / "upper" / "more.py").write_text("X = 1\n")
    changed = ids(module, packages)
    assert changed["lower"] == first["lower"] and changed["upper"] != first["upper"]
    (packages / "lower" / "src" / "lower" / "__init__.py").write_text('"""changed."""\n')
    again = ids(module, packages)
    assert again["lower"] != first["lower"] and again["upper"] != changed["upper"]
    assert module.main(["--packages", str(packages), "--clean"]) == 0
    assert not list(packages.rglob("_build_id"))


def test_read_build(tmp_path: Path) -> None:
    init = tmp_path / "__init__.py"
    assert read_build(str(init)) is None
    (tmp_path / "_build_id").write_text(BUILD_A + "\n")
    assert read_build(str(init)) == BUILD_A
    (tmp_path / "_build_id").write_text("sha256:short\n")
    assert read_build(str(init)) is None


def test_the_build_header() -> None:
    assert parse_build_header(build_header("graph-admin", BUILD_A)) == ("graph-admin", BUILD_A)
    for bad in (None, "", BUILD_A, "graph", "graph sha256:x", "Graph " + BUILD_A, "graph  x"):
        assert parse_build_header(bad) is None
    assert short(BUILD_A) == "a" * 12


def test_the_checkout_claims_no_build() -> None:
    """The editable install of the checkout has no build file (it would go stale)."""
    for name in ("graph_cli", "graph_cli_admin", "graph_cli_server"):
        assert not (REPO_ROOT / "packages" / name / "src" / name / "_build_id").exists()
