"""Access to the tracked fixture ontologies and golden files."""

import os
from dataclasses import replace
from pathlib import Path

from harness import REPO_ROOT

from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import CompileOutcome, compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.semver import Version

FIXTURES = REPO_ROOT / "tests" / "fixtures"
ONTOLOGIES = FIXTURES / "ontologies"
BROKEN = FIXTURES / "broken"
GOLDENS = REPO_ROOT / "tests" / "goldens"

TOY_ONTOLOGIES = ("notes_and_sources", "inventory")
UPDATE_GOLDENS = os.environ.get("GRAPH_CLI_UPDATE_GOLDENS") == "1"


def ontology_versions(name: str) -> list[str]:
    versions = [p.name for p in (ONTOLOGIES / name).iterdir() if p.is_dir()]
    return sorted(versions, key=Version.parse)


def compile_fixture(name: str, version: str | None = None) -> CompileOutcome:
    version = version or ontology_versions(name)[0]
    return compile_bundle(name, LayeredSource(DirectorySource(ONTOLOGIES / name / version)))


def compiled_model(name: str, version: str | None = None) -> CompiledModel:
    outcome = compile_fixture(name, version)
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def assert_golden(path: Path, data: bytes) -> None:
    """Compare with a golden file; ``GRAPH_CLI_UPDATE_GOLDENS=1`` rewrites it."""
    if UPDATE_GOLDENS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert path.exists(), f"missing golden {path}; run with GRAPH_CLI_UPDATE_GOLDENS=1"
    assert data == path.read_bytes(), f"{path} differs; review and regenerate if intended"


def deprecated_model() -> CompiledModel:
    """The inventory model with the class Item, its field name, the relation located_in
    and the rule serial_single_unit deprecated."""
    model = compiled_model("inventory")
    item = model.classes["inv:Item"]
    name = item.attributes["name"]
    item = replace(
        item,
        doc=replace(item.doc, deprecated=True),
        attributes={
            **item.attributes,
            "name": replace(name, doc=replace(name.doc, deprecated=True)),
        },
    )
    located_in = model.relations["inv:located_in"]
    return replace(
        model,
        classes={**model.classes, "inv:Item": item},
        relations={
            **model.relations,
            "inv:located_in": replace(located_in, doc=replace(located_in.doc, deprecated=True)),
        },
        rules=tuple(
            replace(r, doc=replace(r.doc, deprecated=True))
            if r.id == "inv.serial_single_unit"
            else r
            for r in model.rules
        ),
    )
