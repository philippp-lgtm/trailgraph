"""Layering and distribution boundaries.

The app distribution must contain no maintenance code and no test fixtures,
and imports must follow the layer contracts in pyproject.toml.
"""

import shutil
import subprocess
import zipfile

import pytest
from harness import BIN_DIR, REPO_ROOT


def test_import_contracts() -> None:
    lint_imports = BIN_DIR / "lint-imports"
    result = subprocess.run(
        [str(lint_imports)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
    assert not [f for f in files if f.startswith("graph_cli_admin")]
    assert not [f for f in files if "fixtures" in f or f.startswith("tests/")]


@pytest.mark.slow
def test_admin_wheel_does_not_bundle_app_code(wheels: dict[str, list[str]]) -> None:
    files = wheels["graph_cli_admin"]
    assert not [f for f in files if f.startswith("graph_cli/")]
