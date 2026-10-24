"""Programs the origin hands out, and how a client installs them (contracts B14.18).

graph-server serves the wheels its image was built from under ``/v1/dist/``:
``manifest`` names each program's wheel (distribution, version, build id,
hash) and the pinned third-party requirements (``constraints.txt``). A
client updates itself (``graph self update``, ``graph-admin self update``) when the
server's build of its program differs from its own: it fetches the wheel
and the wheels it needs into ``<state_root>/dist/``, checks each hash and
runs ``uv tool install --reinstall`` into the tool environment it runs
from, so the launcher stays where it is. Only a program installed by ``uv
tool`` is replaced; any other install gets ``update.unsupported_install``
with the command to run. A program without a build id (a checkout) is never
replaced. After an install the old process only reads files and runs the
new program: it never imports code of the new one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote

from graph_cli.catalog.build import BUILD_RE, read_build, short
from graph_cli.catalog.remote import (
    ENVELOPE_SCHEMA,
    Answer,
    RemoteClient,
    envelope_of,
    error_of,
    protocol,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

MANIFEST_SCHEMA: Final = "graph_cli.dist/1"
DIST_ROUTE: Final = "/v1/dist/"
MANIFEST: Final = "manifest"
CONSTRAINTS: Final = "constraints.txt"
DIST_DIR: Final = "dist"  # below the state root: the wheels last fetched
RECEIPT: Final = "uv-receipt.toml"  # what uv writes into every tool environment
DIGEST_PREFIX: Final = "sha256:"


@dataclass(frozen=True, slots=True)
class Program:
    """A program the origin can hand out: its distribution, and the programs whose wheels
    its install needs (a workspace dependency the index does not have)."""

    name: str
    distribution: str
    requires: tuple[str, ...] = ()
    admin: bool = False  # the wheel needs a token with the admin scope


PROGRAMS: Final[Mapping[str, Program]] = {
    "graph": Program("graph", "graph_cli"),
    "graph-admin": Program("graph-admin", "graph_cli_admin", ("graph",), admin=True),
}


@dataclass(frozen=True, slots=True)
class DistFile:
    file: str
    hash: str  # sha256:<hex> of the bytes
    size: int


@dataclass(frozen=True, slots=True)
class Wheel:
    program: str
    distribution: str
    version: str
    build: str | None
    requires: tuple[str, ...]
    access: str  # "public" or "admin"
    dist: DistFile

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "build": self.build,
            "requires": list(self.requires),
            "access": self.access,
            "file": self.dist.file,
            "hash": self.dist.hash,
            "size": self.dist.size,
        }


@dataclass(frozen=True, slots=True)
class Manifest:
    programs: Mapping[str, Wheel]
    constraints: DistFile | None

    def to_json(self) -> dict[str, JsonValue]:
        constraints: JsonValue = None
        if self.constraints is not None:
            constraints = {
                "file": self.constraints.file,
                "hash": self.constraints.hash,
                "size": self.constraints.size,
            }
        return {
            "schema": MANIFEST_SCHEMA,
            "programs": {name: wheel.to_json() for name, wheel in sorted(self.programs.items())},
            "constraints": constraints,
        }


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return DIGEST_PREFIX + digest.hexdigest()


def parse_manifest(value: JsonValue, status: int) -> Manifest:
    """The manifest the server answered; ``remote.protocol`` when it is not one."""
    try:
        assert isinstance(value, Mapping) and value.get("schema") == MANIFEST_SCHEMA
        programs, constraints = value["programs"], value["constraints"]
        assert isinstance(programs, Mapping)
        wheels = {}
        for name, item in programs.items():
            assert isinstance(item, Mapping)
            requires, build = item.get("requires"), item.get("build")
            assert isinstance(requires, list)
            assert build is None or (isinstance(build, str) and BUILD_RE.match(build))
            wheels[str(name)] = Wheel(
                str(name),
                str(item["distribution"]),
                str(item["version"]),
                build,
                tuple(str(r) for r in requires),
                str(item["access"]),
                _dist_file(item),
            )
        found = None if constraints is None else _dist_file(constraints)
    except (AssertionError, KeyError, TypeError, ValueError):
        raise protocol(status, "the answer is not a distribution manifest") from None
    return Manifest(wheels, found)


def _dist_file(value: JsonValue) -> DistFile:
    assert isinstance(value, Mapping)
    file, digest, size = value["file"], value["hash"], value["size"]
    assert isinstance(file, str) and file and "/" not in file and not file.startswith(".")
    assert isinstance(digest, str) and digest.startswith(DIGEST_PREFIX)
    assert isinstance(size, int) and not isinstance(size, bool) and size >= 0
    return DistFile(file, digest, size)


def fetch_manifest(client: RemoteClient) -> Manifest:
    answer = client.call("GET", DIST_ROUTE + MANIFEST)
    if answer.status != 200:
        raise refused(client, answer)
    return parse_manifest(answer.json(), answer.status)


def refused(client: RemoteClient, answer: Answer) -> GraphCliError:
    """The error of a ``/v1/dist/`` answer; a server that hands out nothing (an older one,
    or ``[server] dist = false``) answers 404: ``update.unavailable``."""
    error = error_of(envelope_of(answer))
    if answer.status != 404:
        return error
    return GraphCliError(
        "update.unavailable",
        f"the server at {client.endpoint.display} hands out no programs: {error.message}",
        details={"endpoint": client.endpoint.display},
    )


def fetch_file(client: RemoteClient, item: DistFile, directory: Path) -> Path:
    """``item`` fetched into ``directory``, its hash checked; replaced atomically."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / item.file
    partial = directory / f".{item.file}.{os.getpid()}.part"
    try:
        with partial.open("wb") as sink:
            answer = client.call("GET", DIST_ROUTE + quote(item.file, safe=""), sink=sink)
        if answer.status != 200:
            raise refused(client, answer)
        found = file_hash(partial)
        if found != item.hash:
            raise GraphCliError(
                "update.install_failed",
                f"{item.file} arrived with hash {found}, the manifest says {item.hash};"
                " nothing was installed",
                details={"file": item.file, "expected": item.hash, "found": found},
            )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


@dataclass(frozen=True, slots=True)
class ToolEnv:
    """The uv tool environment this process runs from: where uv installs it again."""

    tool_dir: Path  # UV_TOOL_DIR: the parent of the environment
    bin_dir: Path | None  # UV_TOOL_BIN_DIR: where its launchers are, if the receipt says


def tool_env(prefix: Path) -> ToolEnv | None:
    """The uv tool environment at ``prefix`` (``sys.prefix``), else ``None``."""
    try:
        receipt = tomllib.loads((prefix / RECEIPT).read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    bin_dir = None
    for entry in receipt.get("tool", {}).get("entrypoints", []):
        path = entry.get("install-path") if isinstance(entry, dict) else None
        if isinstance(path, str) and path:
            bin_dir = Path(path).parent
            break
    return ToolEnv(prefix.parent, bin_dir)


@dataclass(frozen=True)
class Installer:
    """What ``program_step`` reads from the process: its environment, the prefix it runs
    from and where its build file is. Tests replace them."""

    env: Mapping[str, str]
    prefix: Path = Path(sys.prefix)
    timeout_s: float = 600.0

    def uv(self) -> str | None:
        return shutil.which("uv", path=self.env.get("PATH"))

    def run(
        self, argv: Sequence[str], extra: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )


def program_step(
    client: RemoteClient,
    program: str,
    *,
    package_init: str,
    state_root: Path,
    installer: Installer,
    check: bool,
) -> dict[str, JsonValue]:
    """Compare this program's build with the server's; unless ``check``, install the
    server's when they differ. Answers ``{name, action, build, server, reason?, previous?}``;
    a program without a build id (a checkout) is ``skipped`` without asking the server."""
    current = read_build(package_init)
    if current is None:  # a checkout: nothing to compare, nothing to replace
        return {
            "name": program,
            "build": None,
            "server": None,
            "action": "skipped",
            "reason": "no_build",
        }
    manifest = fetch_manifest(client)
    wheel = manifest.programs.get(program)
    if wheel is None or wheel.build is None:
        raise GraphCliError(
            "update.unavailable",
            f"the server at {client.endpoint.display} hands out no {program}",
            details={"program": program, "endpoint": client.endpoint.display},
        )
    step: dict[str, JsonValue] = {"name": program, "build": current, "server": wheel.build}
    if current == wheel.build:
        return {**step, "action": "current"}
    if check:
        return {**step, "action": "outdated"}
    endpoint = client.endpoint
    if endpoint.plain_http and not endpoint.loopback:
        raise GraphCliError(
            "update.unsupported_install",
            f"{endpoint.display} is plain HTTP: a program fetched from it could have been"
            " changed on the way, so it is not installed; use an https endpoint",
            details={"program": program, "command": None, "uv_missing": False},
        )
    env, uv = tool_env(installer.prefix), installer.uv()
    if env is None or uv is None:
        raise unsupported(client, program, manifest, uv_missing=env is not None)
    directory = state_root / DIST_DIR
    wheels = [fetch_file(client, wheel.dist, directory)]
    for name in wheel.requires:
        required = manifest.programs.get(name)
        if required is None:
            raise GraphCliError(
                "update.unavailable",
                f"the server hands out {program}, but not {name}, which it needs",
                details={"program": name, "endpoint": client.endpoint.display},
            )
        wheels.append(fetch_file(client, required.dist, directory))
    argv = [uv, "tool", "install", "--reinstall", "--compile-bytecode"]
    if manifest.constraints is not None:
        argv += ["--constraints", str(fetch_file(client, manifest.constraints, directory))]
    argv.append(str(wheels[0]))
    for path in wheels[1:]:
        argv += ["--with", str(path)]
    extra = {"UV_TOOL_DIR": str(env.tool_dir)}
    if env.bin_dir is not None:
        extra["UV_TOOL_BIN_DIR"] = str(env.bin_dir)
    done = installer.run(argv, extra)
    installed = read_build(package_init)
    if done.returncode != 0 or installed != wheel.build:
        output = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
        raise GraphCliError(
            "update.install_failed",
            f"uv tool install did not install build {short(wheel.build)} of {program}"
            f" (exit {done.returncode}): {' / '.join(output) or 'no output'}",
            details={
                "program": program,
                "exit_code": done.returncode,
                "installed": installed,
                "expected": wheel.build,
            },
        )
    return {**step, "action": "updated", "build": installed, "previous": current}


def unsupported(
    client: RemoteClient, program: str, manifest: Manifest, *, uv_missing: bool
) -> GraphCliError:
    """This process is no uv tool (or uv is not on ``PATH``): the command that installs
    the server's build by hand. The admin wheel needs a token, so it gets no URL."""
    wheel = manifest.programs[program]
    base = client.endpoint.display.rstrip("/") + DIST_ROUTE
    command = None
    if not wheel.requires and wheel.access == "public":
        command = "uv tool install --reinstall"
        if manifest.constraints is not None:
            command += f" --constraints {base}{manifest.constraints.file}"
        command += f" {base}{wheel.dist.file}"
    why = "uv is not on PATH" if uv_missing else f"this {program} was not installed by uv tool"
    return GraphCliError(
        "update.unsupported_install",
        f"{why}, so {program} cannot replace itself; install the server's build"
        + (f" with: {command}" if command else " as a uv tool from its wheels"),
        details={"program": program, "command": command, "uv_missing": uv_missing},
    )


def run_program(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path
) -> tuple[Mapping[str, JsonValue], int]:
    """``(envelope, exit code)`` of a program run with ``--json`` (large answers stay on
    stdout); ``internal.error`` when it answers no envelope."""
    done = subprocess.run(
        [*argv, "--json"],
        env={**env, "GRAPH_CLI_SPILL_BYTES": "0"},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        document = json.loads(done.stdout)
        assert isinstance(document, Mapping) and document.get("schema") == ENVELOPE_SCHEMA
    except (json.JSONDecodeError, AssertionError):
        tail = (done.stderr or "").strip().splitlines()[-3:]
        raise GraphCliError(
            "internal.error",
            f"{argv[0]} answered no envelope (exit {done.returncode}): {' / '.join(tail)}",
        ) from None
    return document, done.returncode
