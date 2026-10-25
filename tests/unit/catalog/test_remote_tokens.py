"""Where a client's token comes from (contracts B14.10, B14.15, B14.21).

The environment first, then the token ``graph connect`` stored for the
pointer's project, then the configured token file; each file only when
nobody else may read it.
"""

import stat
from pathlib import Path

import pytest

from graph_cli.catalog.endpoint import Endpoint, parse_endpoint
from graph_cli.catalog.remote import (
    APP_TOKEN_FILE,
    project_token_path,
    read_token,
    write_token_file,
)
from graph_cli.kernel.errors import GraphCliError


def endpoint(text: str) -> Endpoint:
    found = parse_endpoint(text)
    assert isinstance(found, Endpoint)
    return found


SERVER = endpoint("https://graphs.example.org")
OTHER = endpoint("https://graphs.example.org:8443/api/")


def token_at(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def test_the_environment_then_the_project_then_the_configured_file(tmp_path: Path) -> None:
    config = tmp_path / "graph_cli" / "config.toml"
    configured = token_at(tmp_path / "configured", "gct_configured")
    project = project_token_path(config, SERVER, "p1", APP_TOKEN_FILE)
    assert project == tmp_path / "graph_cli" / "tokens" / "graphs.example.org_443" / "p1" / "token"
    other = project_token_path(config, OTHER, "p1", APP_TOKEN_FILE)
    assert other is not None and other.parent.parent.name == "graphs.example.org_8443_api"

    def found(given: str | None, project_file: Path | None) -> str:
        return read_token(
            given,
            configured,
            config=config,
            key="remote.token_file",
            hint="h",
            project_file=project_file,
        )

    assert found(None, project) == "gct_configured"  # nothing stored for p1 yet
    write_token_file(project, "gct_project")
    assert found(None, project) == "gct_project"
    assert found("gct_env", project) == "gct_env"
    assert found(None, None) == "gct_configured"
    assert stat.S_IMODE(project.stat().st_mode) == 0o600
    assert stat.S_IMODE(project.parent.stat().st_mode) == 0o700
    project.chmod(0o644)
    with pytest.raises(GraphCliError) as caught:
        found(None, project)
    assert caught.value.code == "config.invalid" and "chmod 600" in caught.value.message


@pytest.mark.parametrize("project", [None, "", "../escape", "a/b", ".hidden", "p1\n"])
def test_only_a_project_name_names_a_token_directory(project: str | None) -> None:
    assert project_token_path(Path("/c/config.toml"), SERVER, project, APP_TOKEN_FILE) is None
