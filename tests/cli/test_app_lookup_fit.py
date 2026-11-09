"""S: ``graph lookup --for`` / ``--for-source`` and facts through the CLI (context-aware
lookup, step 3): options, ``data.fit``, the hints about the intended use, text output."""

import shlex
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES

from graph_cli.kernel.changeset import read_documents

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    seeds = read_documents(
        (FIXTURES / "seeds" / "notes_and_sources" / "changes.jsonl").read_bytes()
    )
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    # a book of the same name as a topic
    created = app(run_cli, "create", "nas:Book", "--set", "title=Physics", env=ENV)
    assert created.code == 0, created.envelope
    return root


def test_several_exact_candidates_hint_at_for(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "lookup", "Physics", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["texts"] == [{"text": "Physics", "total": 2, "exact": 2, "excluded": 0}]
    assert outcome.data["fit"] is None
    assert all(isinstance(i["facts"], list) for i in outcome.data["items"])
    (hint,) = outcome.envelope["hints"]
    assert "--for <relation>" in hint["text"] and hint["command"] is None
    text = run_cli("graph", "lookup", "Physics", env=ENV)
    assert text.returncode == 0
    assert "--for <relation>" in text.stderr and ": None" not in text.stderr


def test_for_keeps_the_candidates_that_fit(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "lookup", "Physics", "--for", "about", env=ENV)
    assert outcome.code == 0, outcome.envelope
    (item,) = outcome.data["items"]
    assert (item["type"], item["facts"]) == ("nas:Topic", None)
    assert outcome.data["texts"] == [{"text": "Physics", "total": 1, "exact": 1, "excluded": 1}]
    assert outcome.data["fit"] == {"for": "about", "for_source": None, "types": ["nas:Topic"]}
    assert outcome.hints == []
    book = app(run_cli, "lookup", "Physics", "--for", "nas:cites", env=ENV)
    assert [i["type"] for i in book.data["items"]] == ["nas:Book"]
    topic = app(run_cli, "lookup", "Physics", "--for-source", "broader", env=ENV)
    assert [i["type"] for i in topic.data["items"]] == ["nas:Topic"]


def test_a_use_that_drops_every_exact_match_says_so(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "lookup", "Physics", "--for-source", "cites", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["texts"] == [{"text": "Physics", "total": 0, "exact": 0, "excluded": 2}]
    prefix, dropped = outcome.envelope["hints"]
    assert prefix["command"] == (
        "graph lookup Physics --graph example_graph --for-source cites --match prefix"
    )
    assert "--for-source cites" in dropped["text"]
    assert dropped["command"] == "graph lookup Physics --graph example_graph"
    again = app(run_cli, *shlex.split(dropped["command"])[1:])
    assert again.data["texts"][0]["exact"] == 2


def test_wrong_uses_are_refused(run_cli: CliRunner, root: Path) -> None:
    unknown = app(run_cli, "lookup", "Physics", "--for", "cited", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    function = app(run_cli, "lookup", "Physics", "--for", "no_such.param", env=ENV)
    assert function.error["code"] == "function.not_found"
    clash = app(run_cli, "lookup", "Physics", "--type", "nas:Note", "--for", "about", env=ENV)
    assert clash.error["code"] == "usage.invalid_argument"
    assert clash.error["details"]["argument"] == "--for"
