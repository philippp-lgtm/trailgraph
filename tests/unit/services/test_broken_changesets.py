"""Broken change sets: exactly one per data.* finding code, each triggering only its code (R.6).

Each fixture directory holds ``changeset.json`` (the defect), an optional
``setup.jsonl`` that must apply cleanly first, an optional ``ontology/``
(own module when the toy ontologies lack the feature) and ``expected.json``
(``graph_cli.fixture_expected/1`` with ``ontology`` and ``phase``; targets
name entities by ``local``). Write-phase fixtures go through the
``WriteService``; batch-phase fixtures apply cleanly and are then found by
``check_graph``.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from support.fixtures import BROKEN
from support.services import ServiceEnv

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, load_document, read_documents
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer import finding_codes as ontology_codes
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.write import WriteRequest
from graph_cli.temporal_knowledge import finding_codes as temporal_codes

REQUEST = RequestMeta("apply", "sha256:" + "0" * 64)

DATA_DIRS = sorted(p for p in BROKEN.iterdir() if p.is_dir() and p.name.startswith("data."))


def test_registry_and_fixture_directories_match() -> None:
    specs = (*ontology_codes.FINDING_CODES, *temporal_codes.FINDING_CODES)
    codes = {s.code for s in specs if s.fixture == "broken_changeset"}
    assert {p.name for p in DATA_DIRS} == codes
    assert all(s.code.startswith("data.") for s in specs if s.fixture == "broken_changeset")


def _own_model(fixture: Path) -> CompiledModel:
    outcome = compile_bundle("broken", LayeredSource(DirectorySource(fixture / "ontology")))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def _portable(target: Mapping[str, JsonValue], names: Mapping[str, str]) -> dict[str, JsonValue]:
    """Targets as fixtures write them: entities named by ``local``, never by id."""
    result = dict(target)
    entity = result.get("id")
    if isinstance(entity, str) and entity in names:
        result["local"] = names[entity]
    if "local" in result:
        result.pop("id", None)
    return result


@pytest.mark.parametrize("fixture", DATA_DIRS, ids=lambda p: p.name)
def test_fixture_triggers_only_its_code(fixture: Path, tmp_path: Path) -> None:
    expected = json.loads((fixture / "expected.json").read_text())
    assert expected["schema"] == "graph_cli.fixture_expected/1"
    assert expected["code"] == fixture.name
    own = (fixture / "ontology").is_dir()
    assert own == ("ontology" not in expected)
    env = ServiceEnv(
        "memory",
        expected.get("ontology", "inventory"),
        tmp_path,
        model_override=_own_model(fixture) if own else None,
    )
    bindings: dict[str, EntityId] = {}
    setup = fixture / "setup.jsonl"
    if setup.exists():
        requests = [
            _request(env, env.service.parse(doc), {}) for doc in read_documents(setup.read_bytes())
        ]
        outcome = env.service.apply_batch(requests)
        assert outcome.error is None, outcome.error.details if outcome.error else None
        for result in outcome.results:
            bindings.update(result.bindings)
    changeset = env.service.parse(load_document((fixture / "changeset.json").read_bytes()))
    request = _request(env, changeset, bindings)
    findings: list[Any]
    try:
        result = env.service.apply(request)
        bindings.update(result.bindings)
        findings = [f.to_json() for f in result.findings]
    except GraphCliError as exc:
        assert exc.code == "write.invalid", exc.message
        findings = list(exc.details["findings"])  # type: ignore[arg-type]
    if expected["phase"] == "batch":
        assert findings == []
        findings = [f.to_json() for f in check_graph(env.model, env.snapshot())]
    names = {str(entity): name for name, entity in bindings.items()}
    found = [(f["code"], _portable(f["target"], names)) for f in findings]
    assert found == [(expected["code"], expected["target"])], findings


def _request(
    env: ServiceEnv, changeset: ChangeSet, bindings: Mapping[str, EntityId]
) -> WriteRequest:
    return WriteRequest(env.actor, env.name, changeset, False, REQUEST, bindings=dict(bindings))
