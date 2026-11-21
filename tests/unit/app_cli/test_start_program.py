"""The program part of ``graph start`` (graph start plan S3): in the remote mode the build of this
program is compared with the server's and never replaced; while it differs from the server's, the
skills are reported and not written (``graph self update`` renders them again after the swap)."""

from collections.abc import Mapping
from typing import Any

import pytest

from graph_cli.app_cli.commands import start as start_command
from graph_cli.app_cli.commands.start import client_part, holds_back, program_state, render
from graph_cli.catalog import dist
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError


class Session:
    """The one thing ``program_state`` takes from a remote session: its client."""

    client = object()


def with_step(monkeypatch: pytest.MonkeyPatch, answer: Mapping[str, Any] | Exception) -> list[Any]:
    """Replace the build comparison; the calls it received are returned."""
    calls: list[Any] = []

    def step(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dist, "program_step", step)
    return calls


def test_a_program_without_a_session_is_local_and_asks_nobody() -> None:
    assert program_state(None)["state"] == "local"


@pytest.mark.parametrize(
    ("action", "state"),
    [("current", "current"), ("outdated", "outdated"), ("skipped", "unchecked")],
)
def test_the_build_comparison_says_current_outdated_or_unchecked(
    monkeypatch: pytest.MonkeyPatch, action: str, state: str
) -> None:
    calls = with_step(monkeypatch, {"action": action, "build": "build-a", "server": "build-b"})
    block = program_state(Session())  # type: ignore[arg-type]
    assert block["state"] == state
    assert block["build"] == "build-a" and block["server_build"] == "build-b"
    assert len(calls) == 1 and calls[0]["check"] is True  # compared, never installed


def test_a_server_without_a_build_leaves_the_program_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_step(monkeypatch, GraphCliError("remote.protocol", "no build"))
    block = program_state(Session())  # type: ignore[arg-type]
    assert block["state"] == "unchecked" and "build" not in block


def test_the_skills_are_only_reported_while_the_program_differs_or_with_check() -> None:
    assert holds_back({"state": "outdated"}, False)
    assert holds_back({"state": "current"}, True)
    assert not holds_back({"state": "current"}, False)
    assert not holds_back({"state": "unchecked"}, False)
    assert not holds_back({"state": "local"}, False)


@pytest.mark.parametrize(
    ("state", "check", "written"),
    [("outdated", False, False), ("current", False, True), ("current", True, False)],
)
def test_an_outdated_program_refreshes_no_skill(
    monkeypatch: pytest.MonkeyPatch, state: str, check: bool, written: bool
) -> None:
    modes: list[bool] = []

    def refreshed(_state: object, *, check: bool, directory: str | None) -> list[JsonValue]:
        modes.append(check)
        return []

    monkeypatch.setattr(start_command, "program_state", lambda session: {"state": state})
    monkeypatch.setattr(start_command, "refreshed", refreshed)
    client_part(object(), check=check, directory=None, session=None)  # type: ignore[arg-type]
    assert modes == [not written]


def test_the_text_for_an_outdated_program_sends_to_self_update_not_to_start() -> None:
    data: dict[str, Any] = {
        "schema": "graph_cli.start/1",
        "program": {"name": "graph", "version": "1.0.0", "state": "outdated"},
        "skills": [{"scope": "project", "skill": "graph-example-graph", "state": "stale"}],
        "identity": {"acting": "an_agent", "project": None, "roles": []},
        "graphs": [],
        "hidden_count": 0,
    }
    text = render(data)
    assert "server build differs: graph self update" in text
    assert "skills to refresh with graph self update: graph-example-graph (stale)" in text
    data["program"]["state"] = "current"
    assert "skills to refresh with graph start: graph-example-graph (stale)" in render(data)
