"""The committed skill 1 and the live inventory it is generated from (item 7 S2), and the
inputs of generated graph skills for the toy ontologies."""

from pathlib import Path
from typing import Any
from uuid import UUID

from harness import REPO_ROOT, CliRunner

from graph_cli.kernel.ids import GraphId, TermRef
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.describe import describe_model
from graph_cli.skills.app_skill import SKILL_NAME, render_app_skill
from graph_cli.skills.graph_skill import graph_block
from support.app import app
from support.fixtures import UPDATE_GOLDENS

SKILL_DIR = REPO_ROOT / "skills" / SKILL_NAME


def live_inventory(run_cli: CliRunner) -> dict[str, Any]:
    """``graph describe commands --json``: the public contract the skill is made from."""
    outcome = app(run_cli, "describe", "commands")
    assert outcome.code == 0, outcome.envelope
    data: dict[str, Any] = outcome.data
    return data


def committed_files(directory: Path = SKILL_DIR) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): path.read_text()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def assert_skill_is_fresh(inventory: dict[str, Any], directory: Path = SKILL_DIR) -> None:
    """The committed files equal a fresh render; ``GRAPH_CLI_UPDATE_GOLDENS=1`` rewrites them."""
    rendered = render_app_skill(inventory)
    if UPDATE_GOLDENS:
        for stale in set(committed_files(directory)) - set(rendered):
            (directory / stale).unlink()
        for name, text in rendered.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text)
    committed = committed_files(directory)
    assert sorted(committed) == sorted(rendered), "run with GRAPH_CLI_UPDATE_GOLDENS=1"
    for name, text in rendered.items():
        assert committed[name] == text, f"{name} is stale; run with GRAPH_CLI_UPDATE_GOLDENS=1"


# ---- generated graph skills ------------------------------------------------------------

GRAPH_NAME = "example_graph"
GRAPH_SKILL_NAME = "graph-example-graph"


def graph_entry(model: CompiledModel, *, prefixes: bool = True) -> GraphEntry:
    """A graph entry for ``model`` as graph creation seeds it (alias prefixes from the
    ``display_prefix`` of concrete classes)."""
    seeded = {
        TermRef(curie): cls.display_prefix
        for curie, cls in sorted(model.classes.items())
        if prefixes and cls.display_prefix is not None and not cls.abstract
    }
    pin = OntologyPin(model.bundle_id, model.bundle_version, model.artifact_hash)
    graph_id = GraphId(UUID("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
    return GraphEntry(graph_id, GRAPH_NAME, "active", pin, seeded, (), "Example graph", "en")


def render_inputs(
    model: CompiledModel, *, prefixes: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(model documents, graph block)`` of ``render_graph_skill`` for a fixture model."""
    entry = graph_entry(model, prefixes=prefixes)
    block = graph_block(
        id=str(entry.id),
        name=entry.name,
        title=entry.title,
        language=entry.language,
        alias_prefixes=entry.alias_prefixes,
    )
    return describe_model(entry, model), block
