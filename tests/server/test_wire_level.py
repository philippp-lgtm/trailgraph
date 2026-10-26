"""The wire between client and server is pinned per ``WIRE_LEVEL`` (plan client_compatibility C2).

The compatibility id covers the inventory and the agent guidance by itself;
the request and answer format is the part a person has to judge. When this
test fails, decide: if an older client could misread a newer server (or the
other way round), raise ``WIRE_LEVEL`` in ``graph_cli.catalog.compat`` and
pin the new wire under the new level; if not (a header nobody has to read),
pin it under the current level.
"""

from typing import Any

from graph_cli.catalog.compat import WIRE_LEVEL
from graph_cli.catalog.endpoint import parse_endpoint
from graph_cli.catalog.remote import RemoteClient
from graph_cli.cli_kit.envelope import envelope
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli_server.app import CORS_EXPOSED, CORS_HEADERS
from graph_cli_server.openapi import API_VERSION

BUILD = "sha256:" + "a" * 64
WIRES: dict[int, dict[str, Any]] = {
    1: {
        "api": "graph_cli.api/1",
        "request_headers": [
            "Accept",
            "Authorization",
            "Content-Length",
            "Content-Type",
            "Graph-Cli-Build",
            "Graph-Cli-Compat",
            "Graph-Cli-Skills",
            "Graph-Cli-Version",
            "User-Agent",
        ],
        "accepted_headers": [
            "Authorization",
            "Content-Type",
            "Graph-Cli-Actor",
            "Graph-Cli-Build",
            "Graph-Cli-Compat",
            "Graph-Cli-Skills",
            "Graph-Cli-Version",
            "Idempotency-Key",
            "If-None-Match",
        ],
        "answer_headers": [
            "Allow",
            "ETag",
            "Graph-Cli-Build",
            "Graph-Cli-Compat",
            "Graph-Cli-Content-Hash",
            "Graph-Cli-Exit-Code",
            "Graph-Cli-Media-Type",
            "Graph-Cli-Version",
            "Retry-After",
        ],
        "envelope": [
            "command",
            "data",
            "error",
            "graph",
            "hints",
            "ok",
            "page",
            "schema",
            "warnings",
        ],
        "envelope_schema": "graph_cli.envelope/1",
        "error": ["category", "code", "details", "hint", "message", "retryable"],
        "warning": ["code", "details", "message"],
    },
}


def _names(value: str) -> list[str]:
    return sorted(name.strip() for name in value.split(","))


def wire() -> dict[str, Any]:
    endpoint = parse_endpoint("http://127.0.0.1:1")
    assert not isinstance(endpoint, str)
    client = RemoteClient(
        endpoint,
        "gct_x",
        timeout_s=1,
        version="0",
        program="graph",
        build=BUILD,
        compat=BUILD,
        skills="graph=x",
    )
    failed = envelope("get", program="graph", error=GraphCliError("internal.error", "x"))
    warned = WarningRecord("remote.version_differs", "x", {})
    error = failed["error"]
    assert isinstance(error, dict)
    return {
        "api": API_VERSION,
        "request_headers": sorted(client._headers(None, b"{}", None)),
        "accepted_headers": _names(CORS_HEADERS),
        "answer_headers": _names(CORS_EXPOSED),
        "envelope": sorted(failed),
        "envelope_schema": failed["schema"],
        "error": sorted(error),
        "warning": sorted(warned.to_json()),
    }


def test_the_wire_is_pinned_for_its_level() -> None:
    assert WIRE_LEVEL in WIRES, f"pin the wire of WIRE_LEVEL {WIRE_LEVEL} in WIRES"
    assert wire() == WIRES[WIRE_LEVEL], (
        "the wire between client and server changed: raise WIRE_LEVEL in"
        " graph_cli.catalog.compat if an older client could misread it, then pin it here"
    )
