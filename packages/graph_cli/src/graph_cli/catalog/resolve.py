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
from graph_cli.storage.files.keyrings import KeySettings

RootSource = Literal["flag", "env", "config", "default"]
APP_DIR: Final = "graph_cli"
ROOT_ENV: Final = "GRAPH_CLI_ROOT"
KEK_FILE_ENV: Final = "GRAPH_CLI_KEK_FILE"
KEK_FILE: Final = "kek"
KEYS_DIR: Final = "graph_cli_keys"


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


def _xdg(env: Mapping[str, str], name: str, fallback: str, app_dir: str = APP_DIR) -> Path:
    value = env.get(name, "")
    if value.startswith("/"):
        return Path(value) / app_dir
    return _home(env) / fallback / app_dir


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


def resolve_keys(env: Mapping[str, str], *, config: Config, cwd: Path, roots: Roots) -> KeySettings:
    """Where the key stores and key-encryption keys of sealed graphs are (plan T16).

    Key stores: ``[keys] dir``, default ``$XDG_DATA_HOME/graph_cli_keys``: as lasting as
    the data (not state, which may be cleaned), beside the default data root, not in it,
    so a backup of the data root holds no key. The KEK file:
    ``GRAPH_CLI_KEK_FILE``, ``[keys] kek_file``, default ``config.toml``'s directory
    ``/kek``. ``[keys] previous_kek_file`` only during a KEK rotation.
    """

    def expand(text: str | None) -> Path | None:
        if text is None:
            return None
        return _home(env) / text[2:] if text.startswith("~/") else Path(text)

    kek_file = (
        cwd / env[KEK_FILE_ENV]
        if env.get(KEK_FILE_ENV)
        else expand(config.kek_file) or roots.config_file.parent / KEK_FILE
    )
    return KeySettings(
        dir=expand(config.keys_dir) or _xdg(env, "XDG_DATA_HOME", ".local/share", KEYS_DIR),
        kek_file=kek_file,
        previous_kek_file=expand(config.previous_kek_file),
    )
