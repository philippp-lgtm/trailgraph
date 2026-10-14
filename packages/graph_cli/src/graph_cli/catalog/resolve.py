"""Root resolution shared by both CLIs (contracts B12.1-B12.3, B12.10, B12.11).

Pure: the environment and the working directory come in as parameters
(the composition roots read them), and nothing is created. Relative
``XDG_*`` values are ignored, as the XDG base directory spec demands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from graph_cli.catalog.config import Config
from graph_cli.kernel.errors import GraphCliError, WarningRecord

RootSource = Literal["flag", "env", "config", "default"]
APP_DIR: Final = "graph_cli"
ROOT_ENV: Final = "GRAPH_CLI_ROOT"


@dataclass(frozen=True, slots=True)
class Roots:
    data_root: Path
    config_file: Path
    state_root: Path
    root_source: RootSource


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME", "")
    if not home.startswith("/"):
        raise GraphCliError("config.invalid", "HOME is not set to an absolute path")
    return Path(home)


def _xdg(env: Mapping[str, str], name: str, fallback: str) -> Path:
    value = env.get(name, "")
    if value.startswith("/"):
        return Path(value) / APP_DIR
    return _home(env) / fallback / APP_DIR


def config_path(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/graph_cli/config.toml``, else ``~/.config/...`` (B12.2)."""
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / "config.toml"


def resolve_roots(
    env: Mapping[str, str],
    *,
    config: Config,
    cwd: Path,
    data_root_flag: Path | None = None,
    allow_flag: bool = False,
) -> tuple[Roots, tuple[WarningRecord, ...]]:
    """B12.1: flag, ``GRAPH_CLI_ROOT``, config ``[paths] data_root``, XDG data dir, default.

    The flag exists only on graph-admin; for the app it is a usage error.
    ``root.overridden`` is emitted for the env override (B12.3); graph-admin's
    own ``--data-root`` is an explicit maintainer choice and does not warn.
    """
    if data_root_flag is not None and not allow_flag:
        raise GraphCliError("usage.invalid_argument", "--data-root is not available here")
    source: RootSource
    warnings: tuple[WarningRecord, ...] = ()
    if data_root_flag is not None:
        root, source = cwd / data_root_flag, "flag"
    elif env.get(ROOT_ENV):
        root, source = cwd / env[ROOT_ENV], "env"
    elif config.data_root is not None:
        text = config.data_root
        root = _home(env) / text[2:] if text.startswith("~/") else Path(text)
        source = "config"
    else:
        root, source = _xdg(env, "XDG_DATA_HOME", ".local/share"), "default"
    data_root = root.resolve()
    if source == "env":
        warnings = (
            WarningRecord(
                "root.overridden",
                f"the data root comes from {ROOT_ENV}",
                {"root_source": source, "data_root": str(data_root)},
            ),
        )
    roots = Roots(
        data_root=data_root,
        config_file=config.path,
        state_root=_xdg(env, "XDG_STATE_HOME", ".local/state"),
        root_source=source,
    )
    return roots, warnings
