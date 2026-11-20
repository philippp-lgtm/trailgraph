"""``config.toml``: the per-user configuration (contracts B12.2, B12.7, Decision 17).

Reading follows V.3/V.4: an unknown ``format_version`` is
``format.unsupported_version``, unknown keys are warnings
(``config.unknown_key``), any other defect is ``config.invalid`` naming the
key. A missing file is the default configuration. ``graph-admin init``
writes the file only when it does not exist yet; ``graph connect`` creates
it or sets only ``[runtime] mode`` and ``endpoint`` in it (``remote_config``).

``[files.s3]`` is stricter: any key it does not define is ``config.invalid``, so
a secret pasted into the file stops the command instead of being ignored
(credentials come only from the environment). Here only its shape is checked;
``catalog.object_store`` validates it with the environment.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from graph_cli.catalog.endpoint import parse_endpoint
from graph_cli.catalog.identity import DEFAULT_PROVIDERS
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.ontology_layer.vocabulary import FILE_ROOT_RE
from graph_cli.storage.files.durable import Durability
from graph_cli.storage.files.tomlw import TomlTable, TomlValue, dumps
from graph_cli.storage.s3.settings import Addressing

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
            "extract",
            "activity",
            "server",
            "remote",
        }
    ),
    "device": frozenset({"id", "name"}),
    "runtime": frozenset({"mode", "endpoint"}),
    "paths": frozenset({"data_root"}),
    "identity": frozenset({"providers"}),
    "storage": frozenset({"lock_timeout_s", "durability"}),
    "audit": frozenset({"record_data_root", "access_log"}),
    "output": frozenset({"default", "spill_bytes", "spill_dir"}),
    "files": frozenset({"max_bytes", "store", "s3"}),
    "extract": frozenset({"timeout_seconds", "max_memory_mib"}),
    "activity": frozenset({"signal_reads"}),
    "server": frozenset({"cors_origins", "admin_api", "client_builds", "dist", "public_url"}),
    "remote": frozenset({"token_file", "admin_token_file", "allow_plain_http", "timeout_s"}),
}
_OPEN_TABLES: Final = frozenset({"roots"})  # keys are names chosen by the user
FileStoreKind = Literal["local", "s3"]
# [files.s3]: exactly these keys; any other is config.invalid, not a warning
S3_KEYS: Final = ("bucket", "prefix", "endpoint_url", "region", "addressing", "allow_plain_http")
DEFAULT_MAX_FILE_BYTES: Final = 100 * 1024 * 1024
MAX_MAX_FILE_BYTES: Final = 1024 * 1024 * 1024
DEFAULT_EXTRACT_TIMEOUT_S: Final = 30.0
MAX_EXTRACT_TIMEOUT_S: Final = 3600
DEFAULT_EXTRACT_MEMORY_MIB: Final = 512
MAX_EXTRACT_MEMORY_MIB: Final = 65536
MIN_EXTRACT_MEMORY_MIB: Final = 64
DEFAULT_SPILL_BYTES: Final = 16384  # below the output limit of common agent harnesses
MAX_SPILL_BYTES: Final = 64 * 1024 * 1024
ORIGIN_RE: Final = re.compile(r"^https?://[^/\s?#]+$")  # a browser Origin: scheme://host[:port]
# no credentials (they would be public in every problem type), no query, no fragment
PUBLIC_URL_RE: Final = re.compile(r"^https?://[^/\s?#@]+(/[^\s?#]*)?$")


@dataclass(frozen=True, slots=True)
class S3Table:
    """``[files.s3]`` as written, shape checked only (``catalog.object_store`` validates
    it with the environment); ``None`` where a key is not set."""

    bucket: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    addressing: Addressing | None = None
    allow_plain_http: bool = False


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
    access_log: bool = False  # [audit]: log every graph command to <state_root>/logs/access.jsonl
    output: OutputMode = "text"
    spill_bytes: int = DEFAULT_SPILL_BYTES  # [output]: larger JSON answers go to a file; 0 = off
    spill_dir: str | None = None  # as written; absolute or "~/..."; None = /tmp/graph_cli_<uid>
    file_roots: Mapping[str, str] = field(default_factory=dict)  # name -> path as written
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    files_store: FileStoreKind = "local"  # [files] store: where new graphs keep their files
    files_s3: S3Table = S3Table()  # [files.s3]
    # [extract]: limits of the child process that parses one file (file metadata M11)
    extract_timeout_s: float = DEFAULT_EXTRACT_TIMEOUT_S
    extract_max_memory_mib: int = DEFAULT_EXTRACT_MEMORY_MIB
    signal_reads: bool = False  # [activity]: send reads to a running graph-admin view
    mode: Literal["local", "remote"] = "local"  # [runtime] mode
    endpoint: str | None = None  # [runtime]: the server's URL in remote mode
    token_file: str | None = None  # [remote]: as written; absolute or "~/..."
    admin_token_file: str | None = None  # [remote]: graph-admin's token; absolute or "~/..."
    allow_plain_http: bool = False  # [remote]: http:// to a host that is not loopback
    remote_timeout_s: float = 60.0  # [remote] timeout_s
    cors_origins: tuple[str, ...] = ()  # [server]: browser origins allowed to call graph-server
    admin_api: bool = False  # [server]: graph-server also serves the graph-admin commands
    # [server]: "compatible" refuses a client of another build whose compatibility id differs,
    # "exact" every client of another build, "warn" none
    client_builds: Literal["compatible", "exact", "warn"] = "compatible"
    dist: bool = True  # [server]: graph-server hands out the programs it was built from
    public_url: str | None = None  # [server]: the URL clients use, without a trailing /


def load_config(path: Path) -> tuple[Config, tuple[WarningRecord, ...]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Config(path), ()
    except OSError as exc:
        raise _invalid(path, "", f"cannot be read: {exc.strerror or exc}") from exc
    return parse_config(data, path=path)


def parse_config(data: bytes, *, path: Path) -> tuple[Config, tuple[WarningRecord, ...]]:
    document = _read_document(data, path)
    t, warnings = _read_tables(path, document)
    config = Config(path, exists=True)
    for section in _SECTIONS:
        config = section(t, config)
    return config, tuple(warnings)


@dataclass(frozen=True, slots=True)
class _Tables:
    """The tables of the document that are known by name; reads one typed key at a time."""

    path: Path
    tables: Mapping[str, Mapping[str, Any]]

    def get(self, dotted: str, kind: type | tuple[type, ...], what: str) -> Any:
        table, key = dotted.split(".")
        value = self.tables.get(table, {}).get(key)
        if value is not None and not _has_type(value, kind):
            raise _invalid(self.path, dotted, f"must be {what}")
        return value


def _read_document(data: bytes, path: Path) -> dict[str, Any]:
    """The TOML document, checked for its ``format_version``."""
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
    return document


def _read_tables(path: Path, document: Mapping[str, Any]) -> tuple[_Tables, list[WarningRecord]]:
    """The top-level keys in sorted order: unknown keys become warnings, the others must be
    tables. The sorted walk decides which defect is reported first."""
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
    return _Tables(path, tables), warnings


def _read_runtime(t: _Tables, config: Config) -> Config:
    """``[runtime]``: the mode and the endpoint as written (checked with the remote keys)."""
    mode = t.get("runtime.mode", str, "a string")
    if mode not in (None, "local", "remote"):
        raise _invalid(t.path, "runtime.mode", "must be 'local' or 'remote'")
    endpoint = t.get("runtime.endpoint", str, "a string")
    return replace(config, mode=mode or config.mode, endpoint=endpoint)


def _read_remote(t: _Tables, config: Config) -> Config:
    """``[remote]``: the token files, the plain-http switch and the timeout."""
    token_file = t.get("remote.token_file", str, "a string")
    if token_file is not None and not token_file.startswith(("/", "~/")):
        raise _invalid(t.path, "remote.token_file", "must be an absolute path or start with ~/")
    admin_token_file = t.get("remote.admin_token_file", str, "a string")
    if admin_token_file is not None and not admin_token_file.startswith(("/", "~/")):
        raise _invalid(
            t.path, "remote.admin_token_file", "must be an absolute path or start with ~/"
        )
    allow_plain_http = t.get("remote.allow_plain_http", bool, "a boolean")
    remote_timeout = t.get("remote.timeout_s", (int, float), "a number")
    if remote_timeout is not None and not 1 <= remote_timeout <= 3600:
        raise _invalid(t.path, "remote.timeout_s", "must be between 1 and 3600 seconds")
    return replace(
        config,
        token_file=token_file,
        admin_token_file=admin_token_file,
        allow_plain_http=bool(allow_plain_http),
        remote_timeout_s=float(remote_timeout)
        if remote_timeout is not None
        else config.remote_timeout_s,
    )


def _check_endpoint(t: _Tables, config: Config) -> Config:
    """In remote mode the endpoint is required and must be safe to send a token to. It is
    checked after the keys of ``[remote]`` because ``allow_plain_http`` takes part."""
    if config.mode == "remote":
        if config.endpoint is None:
            raise _invalid(t.path, "runtime.endpoint", "is required with mode 'remote'")
        found = parse_endpoint(config.endpoint)
        if isinstance(found, str):
            raise _invalid(t.path, "runtime.endpoint", found)
        if found.plain_http and not found.loopback and not config.allow_plain_http:
            raise _invalid(
                t.path,
                "runtime.endpoint",
                "is plain http:// to a host that is not loopback: the token would travel"
                " in clear; use https:// (or set [remote] allow_plain_http = true)",
            )
    return config


def _read_paths(t: _Tables, config: Config) -> Config:
    data_root = t.get("paths.data_root", str, "a string")
    if data_root is not None and not (data_root.startswith(("/", "~/"))):
        raise _invalid(t.path, "paths.data_root", "must be an absolute path or start with ~/")
    return replace(config, data_root=data_root)


def _read_identity(t: _Tables, config: Config) -> Config:
    providers = t.get("identity.providers", list, "an array of strings")
    if providers is not None and not all(isinstance(p, str) for p in providers):
        raise _invalid(t.path, "identity.providers", "must be an array of strings")
    return replace(
        config,
        identity_providers=tuple(providers) if providers is not None else config.identity_providers,
    )


def _read_storage(t: _Tables, config: Config) -> Config:
    timeout = t.get("storage.lock_timeout_s", (int, float), "a number")
    if timeout is not None and not 0 < timeout <= 3600:
        raise _invalid(t.path, "storage.lock_timeout_s", "must be between 0 and 3600 seconds")
    durability = t.get("storage.durability", str, "a string")
    if durability not in (None, "full", "fsync"):
        raise _invalid(t.path, "storage.durability", "must be 'full' or 'fsync'")
    return replace(
        config,
        lock_timeout_s=float(timeout) if timeout is not None else config.lock_timeout_s,
        durability=durability or config.durability,
    )


def _read_output(t: _Tables, config: Config) -> Config:
    output = t.get("output.default", str, "a string")
    if output not in (None, "text", "json"):
        raise _invalid(t.path, "output.default", "must be 'text' or 'json'")
    spill_bytes = t.get("output.spill_bytes", int, "an integer")
    if spill_bytes is not None and not 0 <= spill_bytes <= MAX_SPILL_BYTES:
        raise _invalid(t.path, "output.spill_bytes", f"must be between 0 and {MAX_SPILL_BYTES}")
    spill_dir = t.get("output.spill_dir", str, "a string")
    if spill_dir is not None and not spill_dir.startswith(("/", "~/")):
        raise _invalid(t.path, "output.spill_dir", "must be an absolute path or start with ~/")
    return replace(
        config,
        output=output or config.output,
        spill_bytes=spill_bytes if spill_bytes is not None else config.spill_bytes,
        spill_dir=spill_dir,
    )


def _read_audit(t: _Tables, config: Config) -> Config:
    record = t.get("audit.record_data_root", bool, "a boolean")
    access_log = t.get("audit.access_log", bool, "a boolean")
    return replace(
        config,
        record_data_root=record if record is not None else config.record_data_root,
        access_log=access_log if access_log is not None else config.access_log,
    )


def _read_files(t: _Tables, config: Config) -> Config:
    max_bytes = t.get("files.max_bytes", int, "an integer")
    if max_bytes is not None and not 1 <= max_bytes <= MAX_MAX_FILE_BYTES:
        raise _invalid(t.path, "files.max_bytes", f"must be between 1 and {MAX_MAX_FILE_BYTES}")
    files_store = t.get("files.store", str, "a string")
    if files_store not in (None, "local", "s3"):
        raise _invalid(t.path, "files.store", "must be 'local' or 's3'")
    files_s3 = _s3_table(t.path, t.tables.get("files", {}).get("s3"))
    return replace(
        config,
        max_file_bytes=max_bytes if max_bytes is not None else config.max_file_bytes,
        files_store=files_store or config.files_store,
        files_s3=files_s3,
    )


def _read_extract(t: _Tables, config: Config) -> Config:
    timeout = t.get("extract.timeout_seconds", (int, float), "a number")
    if timeout is not None and not 0 < timeout <= MAX_EXTRACT_TIMEOUT_S:
        raise _invalid(
            t.path,
            "extract.timeout_seconds",
            f"must be above 0 and at most {MAX_EXTRACT_TIMEOUT_S} seconds",
        )
    memory = t.get("extract.max_memory_mib", int, "an integer")
    if memory is not None and not (MIN_EXTRACT_MEMORY_MIB <= memory <= MAX_EXTRACT_MEMORY_MIB):
        raise _invalid(
            t.path,
            "extract.max_memory_mib",
            f"must be between {MIN_EXTRACT_MEMORY_MIB} and {MAX_EXTRACT_MEMORY_MIB}",
        )
    return replace(
        config,
        extract_timeout_s=float(timeout) if timeout is not None else config.extract_timeout_s,
        extract_max_memory_mib=memory if memory is not None else config.extract_max_memory_mib,
    )


def _read_activity(t: _Tables, config: Config) -> Config:
    signal_reads = t.get("activity.signal_reads", bool, "a boolean")
    return replace(
        config,
        signal_reads=signal_reads if signal_reads is not None else config.signal_reads,
    )


def _read_server(t: _Tables, config: Config) -> Config:
    cors_origins = t.get("server.cors_origins", list, "an array of origins")
    if cors_origins is not None and not all(
        isinstance(o, str) and ORIGIN_RE.match(o) for o in cors_origins
    ):
        raise _invalid(t.path, "server.cors_origins", "must be origins like https://example.org")
    admin_api = t.get("server.admin_api", bool, "a boolean")
    client_builds = t.get("server.client_builds", str, "a string")
    if client_builds is not None and client_builds not in ("compatible", "exact", "warn"):
        raise _invalid(t.path, "server.client_builds", "must be 'compatible', 'exact' or 'warn'")
    dist = t.get("server.dist", bool, "a boolean")
    public_url = t.get("server.public_url", str, "a string")
    if public_url is not None and not PUBLIC_URL_RE.match(public_url):
        raise _invalid(
            t.path,
            "server.public_url",
            "must be an http(s) URL without credentials, query or fragment",
        )
    return replace(
        config,
        cors_origins=tuple(cors_origins or ()),
        admin_api=bool(admin_api),
        client_builds=client_builds or config.client_builds,
        dist=dist if dist is not None else config.dist,
        public_url=public_url.rstrip("/") if public_url is not None else None,
    )


def _read_roots(t: _Tables, config: Config) -> Config:
    return replace(config, file_roots=_file_roots(t.path, t.tables.get("roots", {})))


def _read_device(t: _Tables, config: Config) -> Config:
    """Last on purpose: a defect in ``[device]`` is reported after every other one."""
    return replace(
        config,
        device_id=t.get("device.id", str, "a string"),
        device_name=t.get("device.name", str, "a string"),
    )


# The order is the order in which defects are reported when a file has several.
_SECTIONS: Final[tuple[Callable[[_Tables, Config], Config], ...]] = (
    _read_runtime,
    _read_remote,
    _check_endpoint,
    _read_paths,
    _read_identity,
    _read_storage,
    _read_output,
    _read_audit,
    _read_files,
    _read_extract,
    _read_activity,
    _read_server,
    _read_roots,
    _read_device,
)


def _s3_table(path: Path, table: object) -> S3Table:
    """``[files.s3]``: only its keys, each of its type. Messages name keys, never values."""
    if table is None:
        return S3Table()
    if not isinstance(table, dict):
        raise _invalid(path, "files.s3", "must be a table")
    for key in sorted(set(table) - set(S3_KEYS)):
        raise _invalid(
            path,
            f"files.s3.{key}",
            f"is not a key of [files.s3] ({', '.join(S3_KEYS)}); credentials come only from"
            " the environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN)",
        )
    for key in ("bucket", "prefix", "endpoint_url", "region", "addressing"):
        if key in table and not isinstance(table[key], str):
            raise _invalid(path, f"files.s3.{key}", "must be a string")
    addressing = table.get("addressing")
    if addressing not in (None, "path", "virtual"):
        raise _invalid(path, "files.s3.addressing", "must be 'path' or 'virtual'")
    allow_plain_http = table.get("allow_plain_http", False)
    if not isinstance(allow_plain_http, bool):
        raise _invalid(path, "files.s3.allow_plain_http", "must be a boolean")
    return S3Table(
        bucket=table.get("bucket"),
        prefix=table.get("prefix"),
        endpoint_url=table.get("endpoint_url"),
        region=table.get("region"),
        addressing=addressing,
        allow_plain_http=allow_plain_http,
    )


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


_RUNTIME_HEADER: Final = re.compile(r"^\s*\[\s*runtime\s*\]\s*(#.*)?$")
_ANY_HEADER: Final = re.compile(r"^\s*\[")
_RUNTIME_KEY: Final = re.compile(r"^\s*(mode|endpoint)\s*=")


def remote_config(data: bytes | None, *, path: Path, endpoint: str, device_id: str) -> bytes:
    """The config file with ``[runtime] mode = "remote"`` and ``endpoint`` (``graph connect``,
    B14.21). Without a file: a new one with ``device_id``. An existing file is edited in place,
    its other lines and comments kept, and the edit is checked by reading the file again:
    when it would change anything else, ``config.invalid`` asks for the edit by hand."""
    if data is None:
        tables: list[TomlTable] = [
            (None, [("format_version", CONFIG_FORMAT)]),
            ("device", [("id", device_id)]),
            ("runtime", [("mode", "remote"), ("endpoint", endpoint)]),
        ]
        return dumps(tables).encode()
    try:
        text = data.decode()
        before = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid(path, "", f"is not valid TOML: {exc}") from None
    runtime = before.get("runtime", {})
    if not isinstance(runtime, dict):
        raise _invalid(path, "runtime", "must be a table")
    wanted = {
        "mode": dumps([(None, [("mode", "remote")])]),
        "endpoint": dumps([(None, [("endpoint", endpoint)])]),
    }
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _RUNTIME_HEADER.match(line)), None)
    if start is None:
        lines.append(("" if not lines or lines[-1].endswith("\n") else "\n") + "\n[runtime]\n")
        lines += [wanted["mode"], wanted["endpoint"]]
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines)
        )
        missing = dict(wanted)
        for index in range(start + 1, end):
            found = _RUNTIME_KEY.match(lines[index])
            if found is not None and found.group(1) in missing:
                lines[index] = missing.pop(found.group(1))
        lines[start + 1 : start + 1] = list(missing.values())
    edited = "".join(lines)
    expected = {**before, "runtime": {**runtime, "mode": "remote", "endpoint": endpoint}}
    try:
        if tomllib.loads(edited) == expected:
            return edited.encode()
    except tomllib.TOMLDecodeError:
        pass
    raise _invalid(
        path,
        "runtime",
        f'cannot be edited safely; set mode = "remote" and endpoint = "{endpoint}" in its'
        " [runtime] table by hand",
    )


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
