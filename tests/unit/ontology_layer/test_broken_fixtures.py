"""Broken ontologies: exactly one per model.* finding code, each triggering only its code (R.6).

The data.* change-set fixtures are checked in tests/unit/services/test_broken_changesets.py.
"""

import json
from pathlib import Path

import pytest
from support.fixtures import BROKEN

from graph_cli.kernel.findings import sort_findings
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.finding_codes import FINDING_CODES

MAIN_MODULE = "broken"
FIXTURE_DIRS = sorted(p for p in BROKEN.iterdir() if p.is_dir() and p.name.startswith("model."))


def test_registry_and_fixture_directories_match() -> None:
    codes = {spec.code for spec in FINDING_CODES if spec.fixture == "broken_ontology"}
    assert {p.name for p in FIXTURE_DIRS} == codes


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=lambda p: p.name)
def test_fixture_triggers_only_its_code(fixture: Path) -> None:
    expected = json.loads((fixture / "expected.json").read_text())
    assert expected["schema"] == "graph_cli.fixture_expected/1"
    assert expected["code"] == fixture.name
    outcome = compile_bundle(MAIN_MODULE, LayeredSource(DirectorySource(fixture)))
    found = [(f.code, dict(f.target)) for f in outcome.findings]
    assert found == [(expected["code"], expected["target"])], [
        f.to_json() for f in outcome.findings
    ]
    severity = outcome.findings[0].severity.value
    assert (outcome.model is None) == (severity == "error")


def test_findings_are_sorted() -> None:
    outcome = compile_bundle(
        MAIN_MODULE, LayeredSource(DirectorySource(BROKEN / "model.structural.unknown_key"))
    )
    assert list(outcome.findings) == sort_findings(outcome.findings)
