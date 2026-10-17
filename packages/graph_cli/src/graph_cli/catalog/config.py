"""``config.toml``: the per-user configuration (contracts B12.2, B12.7, Decision 17).

Reading follows V.3/V.4: an unknown ``format_version`` is
``format.unsupported_version``, unknown keys are warnings
(``config.unknown_key``), any other defect is ``config.invalid`` naming the
key. A missing file is the default configuration. Only ``graph-admin init``
writes the file, and only when it does not exist yet.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from graph_cli.catalog.identity import DEFAULT_PROVIDERS
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.ontology_layer.vocabulary import FILE_ROOT_RE
from graph_cli.storage.files.durable import Durability
from graph_cli.storage.files.tomlw import TomlTable, TomlValue, dumps

CONFIG_FORMAT: Final = 1
OutputMode = Literal["text", "json"]

_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "": frozenset(
        {
            "format_version",
            "device",
            "runtime",
            "paths",
            "identity",
            "storage",
            "audit",
            "output",
            "roots",
            "files",
            "activity",
        }
    ),
    "device": frozenset({"id", "name"}),
    "runtime": frozenset({"mode", "endpoint"}),
    "paths": frozenset({"data_root"}),
    "identity": frozenset({"providers"}),
    "storage": frozenset({"lock_timeout_s", "durability"}),
    "audit": frozenset({"record_data_root"}),
    "output": frozenset({"default"}),
    "files": frozenset({"max_bytes"}),
    "activity": frozenset({"signal_reads"}),
}
_OPEN_TABLES: Final = frozenset({"roots"})  # keys are names chosen by the user
DEFAULT_MAX_FILE_BYTES: Final = 100 * 1024 * 1024
MAX_MAX_FILE_BYTES: Final = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Config:
    path: Path
    exists: bool = False
    device_id: str | None = None
    device_name: str | None = None
    data_root: str | None = None  # as written; absolute or "~/..." (resolve_roots expands it)
    identity_providers: tuple[str, ...] = DEFAULT_PROVIDERS
    lock_timeout_s: float = 10.0
    durability: Durability = "full"
    record_data_root: bool = True
    output: OutputMode = "text"
    file_roots: Mapping[str, str] = field(default_factory=dict)  # name -> path as written
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    signal_reads: bool = False  # [activity]: send reads to a running graph-admin view


def load_config(path: Path) -> tuple[Config, tuple[WarningRecord, ...]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Config(path), ()
    except OSError as exc:
        raise _invalid(path, "", f"cannot be read: {exc.strerror or exc}") from exc
    return parse_config(data, path=path)


def parse_config(data: bytes, *, path: Path) -> tuple[Config, tuple[WarningRecord, ...]]:
    try:
        document = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid(path, "", f"is not valid TOML: {exc}") from None
    version = document.get("format_version")
    if version is None:
        raise _invalid(path, "format_version", "is missing")
    if version != CONFIG_FORMAT:
        raise GraphCliError(
            "format.unsupported_version",
            f"{path}: unsupported format_version {version!r}",
            details={"file": str(path), "expected": CONFIG_FORMAT, "actual": str(version)},
        )
    warnings: list[WarningRecord] = []
    tables: dict[str, Mapping[str, Any]] = {}
    for key in sorted(document):
        if key not in _KEYS[""]:
            warnings.append(_unknown(path, key))
        elif key != "format_version":
            if not isinstance(document[key], dict):
                raise _invalid(path, key, "must be a table")
            tables[key] = document[key]
            if key not in _OPEN_TABLES:
                warnings += [
                    _unknown(path, f"{key}.{k}") for k in sorted(set(document[key]) - _KEYS[key])
                ]

    def get(dotted: str, kind: type | tuple[type, ...], what: str) -> Any:
        table, key = dotted.split(".")
        value = tables.get(table, {}).get(key)
        if value is not None and not _has_type(value, kind):
            raise _invalid(path, dotted, f"must be {what}")
        return value

    mode = get("runtime.mode", str, "a string")
    if mode == "remote":
        raise _invalid(path, "runtime.mode", "'remote' is reserved and not supported yet")
    if mode not in (None, "local"):
        raise _invalid(path, "runtime.mode", "must be 'local'")
    get("runtime.endpoint", str, "a string")
    data_root = get("paths.data_root", str, "a string")
    if data_root is not None and not (data_root.startswith(("/", "~/"))):
        raise _invalid(path, "paths.data_root", "must be an absolute path or start with ~/")
    providers = get("identity.providers", list, "an array of strings")
    if providers is not None and not all(isinstance(p, str) for p in providers):
        raise _invalid(path, "identity.providers", "must be an array of strings")
    timeout = get("storage.lock_timeout_s", (int, float), "a number")
    if timeout is not None and not 0 < timeout <= 3600:
        raise _invalid(path, "storage.lock_timeout_s", "must be between 0 and 3600 seconds")
    durability = get("storage.durability", str, "a string")
    if durability not in (None, "full", "fsync"):
        raise _invalid(path, "storage.durability", "must be 'full' or 'fsync'")
    output = get("output.default", str, "a string")
    if output not in (None, "text", "json"):
        raise _invalid(path, "output.default", "must be 'text' or 'json'")
    record = get("audit.record_data_root", bool, "a boolean")
    max_bytes = get("files.max_bytes", int, "an integer")
    if max_bytes is not None and not 1 <= max_bytes <= MAX_MAX_FILE_BYTES:
        raise _invalid(path, "files.max_bytes", f"must be between 1 and {MAX_MAX_FILE_BYTES}")
    signal_reads = get("activity.signal_reads", bool, "a boolean")
    file_roots = _file_roots(path, tables.get("roots", {}))
    defaults = Config(path)
    config = Config(
        path=path,
        exists=True,
        device_id=get("device.id", str, "a string"),
        device_name=get("device.name", str, "a string"),
        data_root=data_root,
        identity_providers=tuple(providers)
        if providers is not None
        else defaults.identity_providers,
        lock_timeout_s=float(timeout) if timeout is not None else defaults.lock_timeout_s,
        durability=durability or defaults.durability,
        record_data_root=record if record is not None else defaults.record_data_root,
        output=output or defaults.output,
        file_roots=file_roots,
        max_file_bytes=max_bytes if max_bytes is not None else defaults.max_file_bytes,
        signal_reads=signal_reads if signal_reads is not None else defaults.signal_reads,
    )
    return config, tuple(warnings)


def _file_roots(path: Path, table: Mapping[str, Any]) -> dict[str, str]:
    """``[roots]``: name -> absolute path or ``~/...``, like ``[paths] data_root``."""
    roots: dict[str, str] = {}
    for name in sorted(table):
        value = table[name]
        if FILE_ROOT_RE.match(name) is None:
            raise _invalid(path, f"roots.{name}", f"is not a root name ({FILE_ROOT_RE.pattern})")
        if not isinstance(value, str) or not value.startswith(("/", "~/")):
            raise _invalid(path, f"roots.{name}", "must be an absolute path or start with ~/")
        roots[name] = value
    return roots


def format_new_config(
    *, device_id: str, device_name: str | None = None, data_root: str | None = None
) -> bytes:
    """The file ``graph-admin init`` writes; fixed key order (C.8)."""
    device: list[tuple[str, TomlValue]] = [("id", device_id)]
    if device_name is not None:
        device.append(("name", device_name))
    tables: list[TomlTable] = [
        (None, [("format_version", CONFIG_FORMAT)]),
        ("device", device),
        ("runtime", [("mode", "local")]),
    ]
    if data_root is not None:
        tables.append(("paths", [("data_root", data_root)]))
    return dumps(tables).encode()


def _has_type(value: object, kind: type | tuple[type, ...]) -> bool:
    """TOML booleans are no numbers here, and numbers no booleans."""
    if isinstance(value, bool):
        return kind is bool
    return isinstance(value, kind)


def _invalid(path: Path, key: str, message: str) -> GraphCliError:
    where = f"{path}: {key} " if key else f"{path} "
    return GraphCliError("config.invalid", where + message, details={"file": str(path), "key": key})


def _unknown(path: Path, key: str) -> WarningRecord:
    return WarningRecord(
        "config.unknown_key",
        f"{path}: unknown key {key!r} is ignored",
        {"file": str(path), "key": key},
    )
