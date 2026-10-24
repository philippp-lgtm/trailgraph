"""The client side of the origin's programs: ``catalog.dist`` (contracts B14.18).

A fake transport answers the ``/v1/dist/`` routes and a fake installer
plays ``uv tool install``: it writes the build file the real install would
replace, so the step's own check of what got installed is exercised.
"""

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from graph_cli.catalog.dist import (
    Installer,
    Manifest,
    parse_manifest,
    program_step,
    tool_env,
)
from graph_cli.catalog.endpoint import parse_endpoint
from graph_cli.catalog.remote import Answer
from graph_cli.kernel.errors import GraphCliError

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
GRAPH_WHEEL = b"graph wheel bytes"
ADMIN_WHEEL = b"admin wheel bytes"
CONSTRAINTS = b"click==8.5.0\n"
NOT_FOUND = (
    b'{"schema": "graph_cli.envelope/1", "ok": false, "command": null, "graph": null,'
    b' "data": null, "page": null, "hints": [], "warnings": [], "error": {"code":'
    b' "usage.unknown_command", "category": "usage", "retryable": false, "message":'
    b' "no such endpoint", "hint": null, "details": {}}}'
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest(build: str | None = NEW, *, admin: bool = True) -> dict[str, Any]:
    def item(dist: str, data: bytes, requires: list[str], access: str) -> dict[str, Any]:
        return {
            "distribution": dist,
            "version": "0.1.0.dev0",
            "build": build,
            "requires": requires,
            "access": access,
            "file": f"{dist}-0.1.0.dev0-py3-none-any.whl",
            "hash": digest(data),
            "size": len(data),
        }

    programs = {"graph": item("graph_cli", GRAPH_WHEEL, [], "public")}
    if admin:
        programs["graph-admin"] = item("graph_cli_admin", ADMIN_WHEEL, ["graph"], "admin")
    return {
        "schema": "graph_cli.dist/1",
        "programs": programs,
        "constraints": {
            "file": "constraints.txt",
            "hash": digest(CONSTRAINTS),
            "size": len(CONSTRAINTS),
        },
    }


@dataclass
class FakeClient:
    """``RemoteClient.call`` for GET requests, answered from ``routes``."""

    routes: dict[str, tuple[int, bytes]]
    endpoint: Any = field(default_factory=lambda: parse_endpoint("http://127.0.0.1:8470"))
    seen: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, *, sink: BinaryIO | None = None, **_: Any) -> Answer:
        assert method == "GET"
        self.seen.append(path)
        status, body = self.routes.get(path, (404, NOT_FOUND))
        if sink is not None and status == 200:
            sink.write(body)
            body = b""
        return Answer(status, {}, body)


def client(document: dict[str, Any] | None = None, **files: bytes) -> FakeClient:
    import json

    routes = {"/v1/dist/manifest": (200, json.dumps(document or manifest()).encode())}
    found = {
        "graph_cli-0.1.0.dev0-py3-none-any.whl": GRAPH_WHEEL,
        "graph_cli_admin-0.1.0.dev0-py3-none-any.whl": ADMIN_WHEEL,
        "constraints.txt": CONSTRAINTS,
        **files,
    }
    routes.update({f"/v1/dist/{name}": (200, data) for name, data in found.items()})
    return FakeClient(routes)


@dataclass(frozen=True)
class FakeInstaller(Installer):
    """A uv tool environment at ``prefix``; ``run`` writes ``installs`` into the build file."""

    package: Path = Path()
    installs: str | None = NEW
    exit_code: int = 0
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)

    def uv(self) -> str | None:
        return "/opt/uv/bin/uv"

    def run(
        self, argv: Sequence[str], extra: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(extra)))
        if self.installs is not None:
            (self.package / "_build_id").write_text(self.installs + "\n")
        return subprocess.CompletedProcess(list(argv), self.exit_code, "", "error: boom\n")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """An installed package with build ``OLD`` inside a uv tool environment."""
    prefix = tmp_path / "tools" / "graph-cli"
    package = prefix / "lib" / "site-packages" / "graph_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "_build_id").write_text(OLD + "\n")
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{ name = "graph", install-path = "'
        + str(tmp_path / "bin" / "graph")
        + '", from = "graph-cli" }]\n'
    )
    return package


def installer(package: Path, **kwargs: Any) -> FakeInstaller:
    return FakeInstaller(
        {"PATH": "/opt/uv/bin"}, prefix=package.parents[2], package=package, **kwargs
    )


def step(fake: FakeClient, package: Path, found: Installer, **kwargs: Any) -> dict[str, Any]:
    return program_step(
        fake,  # type: ignore[arg-type]
        kwargs.pop("program", "graph"),
        package_init=str(package / "__init__.py"),
        state_root=package.parents[4] / "state",
        installer=found,
        check=kwargs.pop("check", False),
    )


def test_the_manifest_round_trips() -> None:
    parsed = parse_manifest(manifest(), 200)
    assert isinstance(parsed, Manifest)
    assert parsed.to_json() == manifest()
    for broken in ({}, {**manifest(), "schema": "x"}, {**manifest(), "programs": []}):
        with pytest.raises(GraphCliError) as caught:
            parse_manifest(broken, 200)
        assert caught.value.code == "remote.protocol"
    for name in ("../graph.whl", "a/graph.whl", "a\\graph.whl", ".graph.whl", "c:graph.whl", ""):
        bad = manifest()
        bad["programs"]["graph"]["file"] = name
        with pytest.raises(GraphCliError):
            parse_manifest(bad, 200)


def test_a_tool_environment_is_found_by_its_receipt(package: Path, tmp_path: Path) -> None:
    found = tool_env(package.parents[2])
    assert found is not None
    assert found.tool_dir == tmp_path / "tools" and found.bin_dir == tmp_path / "bin"
    assert tool_env(tmp_path) is None


def test_the_same_build_is_current_and_check_changes_nothing(package: Path) -> None:
    same = client(manifest(OLD))
    found = installer(package)
    assert step(same, package, found)["action"] == "current"
    outdated = step(client(), package, found, check=True)
    assert outdated == {"name": "graph", "build": OLD, "server": NEW, "action": "outdated"}
    assert found.calls == []


def test_another_build_is_installed_into_the_same_environment(
    package: Path, tmp_path: Path
) -> None:
    fake, found = client(), installer(package)
    done = step(fake, package, found)
    assert done == {
        "name": "graph",
        "build": NEW,
        "server": NEW,
        "action": "updated",
        "previous": OLD,
    }
    ((argv, extra),) = found.calls
    dist = tmp_path / "state" / "dist"
    assert argv == [
        "/opt/uv/bin/uv",
        "tool",
        "install",
        "--reinstall",
        "--compile-bytecode",
        "--constraints",
        str(dist / "constraints.txt"),
        str(dist / "graph_cli-0.1.0.dev0-py3-none-any.whl"),
    ]
    assert extra == {
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
    }
    assert (dist / "graph_cli-0.1.0.dev0-py3-none-any.whl").read_bytes() == GRAPH_WHEEL
    assert sorted(p.name for p in dist.iterdir()) == [
        "constraints.txt",
        "graph_cli-0.1.0.dev0-py3-none-any.whl",
    ]


def test_graph_admin_brings_the_graph_wheel_it_needs(package: Path, tmp_path: Path) -> None:
    found = installer(package)
    assert step(client(), package, found, program="graph-admin")["action"] == "updated"
    argv = found.calls[0][0]
    dist = tmp_path / "state" / "dist"
    assert argv[-3:] == [
        str(dist / "graph_cli_admin-0.1.0.dev0-py3-none-any.whl"),
        "--with",
        str(dist / "graph_cli-0.1.0.dev0-py3-none-any.whl"),
    ]


def test_a_wheel_with_another_hash_is_not_installed(package: Path) -> None:
    fake = client(**{"graph_cli-0.1.0.dev0-py3-none-any.whl": b"tampered"})
    found = installer(package)
    with pytest.raises(GraphCliError) as caught:
        step(fake, package, found)
    assert caught.value.code == "update.install_failed"
    assert found.calls == []
    assert not any(p.suffix == ".part" for p in (package.parents[4] / "state" / "dist").iterdir())


def test_a_failed_install_is_reported(package: Path) -> None:
    found = installer(package, installs=None, exit_code=2)
    with pytest.raises(GraphCliError) as caught:
        step(client(), package, found)
    assert caught.value.code == "update.install_failed"
    assert "exit 2" in caught.value.message and "error: boom" in caught.value.message
    assert caught.value.details["installed"] == OLD


def test_a_program_not_installed_by_uv_tool_gets_the_command(package: Path) -> None:
    (package.parents[2] / "uv-receipt.toml").unlink()
    with pytest.raises(GraphCliError) as caught:
        step(client(), package, installer(package))
    error = caught.value
    assert error.code == "update.unsupported_install"
    base = "http://127.0.0.1:8470/v1/dist/"
    assert error.details["command"] == (
        f"uv tool install --reinstall --constraints {base}constraints.txt"
        f" {base}graph_cli-0.1.0.dev0-py3-none-any.whl"
    )
    with pytest.raises(GraphCliError) as caught:
        step(client(), package, installer(package), program="graph-admin")
    assert caught.value.details["command"] is None  # the admin wheel needs a token


def test_a_checkout_is_left_alone(package: Path) -> None:
    (package / "_build_id").unlink()
    found = installer(package)
    done = step(client(), package, found)
    assert done["action"] == "skipped" and done["reason"] == "no_build"
    assert found.calls == []


def test_a_server_that_hands_out_nothing(package: Path) -> None:
    for fake in (FakeClient({}), client(manifest(admin=False))):
        with pytest.raises(GraphCliError) as caught:
            step(fake, package, installer(package), program="graph-admin")
        assert caught.value.code == "update.unavailable"


def test_nothing_is_installed_over_plain_http_beyond_loopback(package: Path) -> None:
    fake, found = client(), installer(package)
    fake.endpoint = parse_endpoint("http://graphs.example.org")
    assert step(fake, package, found, check=True)["action"] == "outdated"  # reading is fine
    with pytest.raises(GraphCliError) as caught:
        step(fake, package, found)
    assert caught.value.code == "update.unsupported_install"
    assert "plain HTTP" in caught.value.message
    assert found.calls == [] and fake.seen == ["/v1/dist/manifest"] * 2
