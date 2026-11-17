"""D: the field tables of skill 1's ``references/output.md`` against real answers.

A seeded graph answers ``find``, ``get``, ``lookup``, ``history`` and a write; every
field the answer holds must be listed, and every listed top-level field must be in
the answer. Nested fields (``edges[].rel``, ``match.kind``) are compared one level
deep and may be absent (a qualifier only shows when set).
"""

import re
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.roles import load_roles
from support.skills import committed_files

from graph_cli.kernel.changeset import read_documents

ONTOLOGY = "notes_and_sources"
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


def documented() -> dict[str, set[str]]:
    """``{section heading: listed fields}``, read without the renderer's code."""
    text = committed_files()["references/output.md"]
    parts = re.split(r"^## (.*)$", text, flags=re.MULTILINE)
    fields: dict[str, set[str]] = {}
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        rows = [line for line in body.splitlines() if line.startswith("| `")]
        fields[heading] = {re.match(r"\| `([^`]+)`", row).group(1) for row in rows}  # type: ignore[union-attr]
    return fields


def flat(objects: list[dict[str, Any]], listed: set[str]) -> set[str]:
    """Field names as the reference writes them: ``key``, ``key[].sub``, ``key.sub``."""
    names: set[str] = set()
    for obj in objects:
        for key, value in obj.items():
            if isinstance(value, list) and any(f.startswith(f"{key}[].") for f in listed):
                names |= {f"{key}[].{sub}" for item in value for sub in item}
            elif isinstance(value, dict) and any(f.startswith(f"{key}.") for f in listed):
                names |= {f"{key}.{sub}" for sub in value}
            else:
                names.add(key)
    return names


def check(
    section: str,
    objects: list[dict[str, Any]],
    skip: frozenset[str] = frozenset(),
    when_set: frozenset[str] = frozenset(),
) -> None:
    """``when_set``: listed fields an answer has only when they hold a value."""
    listed = documented()[section]
    found = flat(objects, listed) - skip
    assert found <= listed, (section, found - listed)
    top = {f for f in listed if "." not in f} - when_set
    assert top <= {key for obj in objects for key in obj}, (section, top)


@pytest.fixture
def seeded(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ONTOLOGY)
    seeds = read_documents((FIXTURES / "seeds" / ONTOLOGY / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


def test_output_reference_lists_the_fields_of_real_answers(
    run_cli: CliRunner, seeded: Path
) -> None:
    roles = load_roles(ONTOLOGY)
    note_type = roles.term("retractable_type")
    found = app(run_cli, "find", "--type", note_type, "--limit", "1", env=ENV)
    assert found.code == 0, found.envelope
    check("Pages", [found.envelope["page"]])
    every = app(run_cli, "find", "--limit", "500", env=ENV)
    check("Entities", every.data["items"])
    lean = documented()["Entities"]
    full = app(run_cli, "find", "--limit", "500", "--full", env=ENV)
    stored = [
        {
            **{k: v for k, v in item.items() if k not in lean and k != "edges"},
            "edges": [{"edge_id": edge["edge_id"]} for edge in item["edges"]],
        }
        for item in full.data["items"]
    ]
    check("Entities with --full", stored)
    assert all({"valid_from", "valid_to"} <= set(item) for item in full.data["items"])

    entity = found.data["items"][0]
    got = app(run_cli, "get", entity["id"], env=ENV)
    check("Entities", [got.data["entity"]], when_set=frozenset({"valid_from", "valid_to"}))
    check("Get", [got.data], skip=frozenset({"entity"}))

    superseded = app(
        run_cli, "find", "--where", "record_status=superseded", "--include-inactive", env=ENV
    )
    warned = app(run_cli, "get", superseded.data["items"][0]["id"], env=ENV)
    hints_and_warnings = {"hints": found.envelope["hints"], "warnings": warned.envelope["warnings"]}
    assert all(hints_and_warnings.values())
    check("Hints and warnings", [hints_and_warnings])

    label_type, _, label_attr = str(roles.get("label_attribute")).rpartition(".")
    labelled = app(run_cli, "find", "--type", label_type, "--limit", "1", env=ENV)
    name = labelled.data["items"][0]["attrs"][label_attr]
    lookup = app(run_cli, "lookup", name, env=ENV)
    check("Lookup", lookup.data["items"])

    retracted = app(run_cli, "retract", entity["id"], "--reason", "test", env=ENV)
    assert retracted.code == 0, retracted.envelope
    check("Write results", [retracted.data])
    history = app(run_cli, "history", entity["id"], env=ENV)
    assert set(history.data) == {"id", "current_rev", "items"}
    check("History", history.data["items"], when_set=frozenset({"migration"}))


def paths(data: dict[str, Any], prefix: str = "") -> set[str]:
    """Field paths as the Changes section writes them: ``key``, ``list[].key``. Objects below
    a key (``changed``, ``action``, ``migration``) are one row, not walked."""
    found: set[str] = set()
    for key, value in data.items():
        found.add(prefix + key)
        if isinstance(value, list):
            for element in value:
                if isinstance(element, dict):
                    found |= paths(element, f"{prefix}{key}[].")
    return found


def test_output_reference_lists_the_fields_of_a_change_feed_answer(
    run_cli: CliRunner, seeded: Path
) -> None:
    answer = app(run_cli, "changes", "--since", "tx:0", "--limit", "500", env=ENV)
    assert answer.code == 0, answer.envelope
    assert answer.data["items"] and any(e["entities"] for e in answer.data["items"])
    listed = documented()["Changes"]
    found = paths(answer.data)
    assert found <= listed, found - listed
    # a field of an answer only some transactions or entities have
    when_set = {
        "items[].action",  # a write by ``do``
        "items[].migration",  # an ontology release that migrated data
        "items[].partial",  # a transaction cut by the page size
        "items[].entities[].targets_more",  # more than 50 targets
    }
    assert listed - when_set <= found, listed - when_set - found
