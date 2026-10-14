"""Root resolution (contracts B12.1-B12.3; plan M5)."""

from pathlib import Path

import pytest

from graph_cli.catalog.config import Config
from graph_cli.catalog.resolve import config_path, resolve_roots
from graph_cli.kernel.errors import GraphCliError

HOME = "/nohome/user"  # not a real path: resolve() must not follow host symlinks
CWD = Path("/work")


def roots(
    env: dict[str, str], *, config_root: str | None = None, flag: str | None = None
) -> tuple[str, str, list[str]]:
    config = Config(Path("/cfg/config.toml"), data_root=config_root)
    resolved, warnings = resolve_roots(
        {"HOME": HOME, **env},
        config=config,
        cwd=CWD,
        data_root_flag=Path(flag) if flag else None,
        allow_flag=True,
    )
    return str(resolved.data_root), resolved.root_source, [w.code for w in warnings]


@pytest.mark.parametrize(
    ("env", "config_root", "flag", "expected"),
    [
        ({}, None, None, (f"{HOME}/.local/share/graph_cli", "default", [])),
        ({"XDG_DATA_HOME": "/xdg"}, None, None, ("/xdg/graph_cli", "default", [])),
        ({"XDG_DATA_HOME": "rel"}, None, None, (f"{HOME}/.local/share/graph_cli", "default", [])),
        ({}, "/srv/g", None, ("/srv/g", "config", [])),
        ({}, "~/g", None, (f"{HOME}/g", "config", [])),
        ({"GRAPH_CLI_ROOT": "/env"}, "/srv/g", None, ("/env", "env", ["root.overridden"])),
        ({"GRAPH_CLI_ROOT": "rel"}, None, None, ("/work/rel", "env", ["root.overridden"])),
        ({"GRAPH_CLI_ROOT": "/env"}, "/srv/g", "/flag", ("/flag", "flag", [])),
        ({}, None, "x/../y", ("/work/y", "flag", [])),
    ],
)
def test_root_precedence(
    env: dict[str, str],
    config_root: str | None,
    flag: str | None,
    expected: tuple[str, str, list[str]],
) -> None:
    assert roots(env, config_root=config_root, flag=flag) == expected


def test_flag_is_refused_where_not_allowed() -> None:
    with pytest.raises(GraphCliError) as caught:
        resolve_roots({"HOME": HOME}, config=Config(Path("/c")), cwd=CWD, data_root_flag=Path("/x"))
    assert caught.value.code == "usage.invalid_argument"


def test_config_and_state_paths() -> None:
    assert config_path({"HOME": HOME}) == Path(f"{HOME}/.config/graph_cli/config.toml")
    assert config_path({"HOME": HOME, "XDG_CONFIG_HOME": "/c"}) == Path("/c/graph_cli/config.toml")
    resolved, _ = resolve_roots(
        {"HOME": HOME, "XDG_STATE_HOME": "/s"}, config=Config(Path("/c")), cwd=CWD
    )
    assert resolved.state_root == Path("/s/graph_cli")
    resolved, _ = resolve_roots({"HOME": HOME}, config=Config(Path("/c")), cwd=CWD)
    assert resolved.state_root == Path(f"{HOME}/.local/state/graph_cli")


def test_home_is_required() -> None:
    with pytest.raises(GraphCliError) as caught:
        config_path({})
    assert caught.value.code == "config.invalid"


def test_resolution_creates_nothing(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    resolve_roots(env, config=Config(config_path(env)), cwd=tmp_path)
    assert list(tmp_path.iterdir()) == []
