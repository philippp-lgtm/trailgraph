"""Layering and distribution boundaries.

The app distribution must contain no maintenance code and no test fixtures,
and imports must follow the layer contracts in pyproject.toml.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from harness import BIN_DIR, REPO_ROOT


def test_import_contracts() -> None:
    lint_imports = BIN_DIR / "lint-imports"
    result = subprocess.run(
        [str(lint_imports)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Contract name -> (module file in a scratch copy of the sources, violating import).
POSITIVE_CONTROLS = {
    "Only graph_cli_admin uses the FileStore maintenance port": (
        "graph_cli/services/__init__.py",
        "import graph_cli.storage.files.maintenance\n",
    ),
    "Storage backends are independent of each other": (
        "graph_cli/storage/memory/__init__.py",
        "import graph_cli.storage.files.store\n",
    ),
    "The shared storage core imports no backend": (
        "graph_cli/storage/common/__init__.py",
        "import graph_cli.storage.memory.store\n",
    ),
    "Audit and access control import only the kernel": (
        "graph_cli/audit/__init__.py",
        "import graph_cli.query\n",
    ),
}
# Allowed by the same contracts: admin may use the maintenance port.
ALLOWED_IMPORTS = {
    "graph_cli_admin/services_admin/__init__.py": "import graph_cli.storage.files.maintenance\n",
}


def test_contracts_catch_violations(tmp_path: Path) -> None:
    """Positive controls: a scratch copy with one violation per contract breaks exactly
    those contracts, and the allowed admin import breaks none."""
    src = tmp_path / "src"
    for package in ("graph_cli", "graph_cli_admin"):
        shutil.copytree(
            REPO_ROOT / "packages" / package / "src" / package,
            src / package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    for relative, line in [*POSITIVE_CONTROLS.values(), *ALLOWED_IMPORTS.items()]:
        path = src / relative
        path.write_text(path.read_text() + line)
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    result = subprocess.run(
        [str(BIN_DIR / "lint-imports"), "--config", "pyproject.toml", "--no-cache"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    broken = {
        line.removesuffix(" BROKEN")
        for line in result.stdout.splitlines()
        if line.endswith(" BROKEN")
    }
    assert result.returncode != 0
    assert broken == set(POSITIVE_CONTROLS), result.stdout
    assert "graph_cli_admin.services_admin" not in result.stdout


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    out = tmp_path_factory.mktemp("dist")
    names: dict[str, list[str]] = {}
    for package in ("graph_cli", "graph_cli_admin"):
        subprocess.run(
            [uv, "build", "--package", package, "--wheel", "--out-dir", str(out / package)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            timeout=300,
        )
        (wheel,) = (out / package).glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            names[package] = archive.namelist()
    return names


@pytest.mark.slow
def test_app_wheel_contains_no_admin_code_or_fixtures(wheels: dict[str, list[str]]) -> None:
    files = wheels["graph_cli"]
    assert any(f.startswith("graph_cli/") for f in files)
    assert "graph_cli/ontology_layer/modules/core.yaml" in files  # built-in modules ship
    assert not [f for f in files if f.startswith("graph_cli_admin")]
    assert not [f for f in files if "fixtures" in f or f.startswith("tests/")]


@pytest.mark.slow
def test_admin_wheel_does_not_bundle_app_code(wheels: dict[str, list[str]]) -> None:
    files = wheels["graph_cli_admin"]
    assert not [f for f in files if f.startswith("graph_cli/")]
    assert "graph_cli_admin/maintenance_cli/static/view.html" in files  # the view page ships
