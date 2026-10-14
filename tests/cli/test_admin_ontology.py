"""S: graph-admin ontology validate and diff (contracts B7.12, B7.13; plan M7)."""

import json
import shutil
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin
from support.fixtures import BROKEN, ONTOLOGIES, compiled_model

INVENTORY = ONTOLOGIES / "inventory"
EXPECTED_DIFFS = json.loads((INVENTORY / "expected_diffs.json").read_text())["diffs"]


def test_validate_reports_the_compiled_identity(run_cli: CliRunner) -> None:
    outcome = admin(run_cli, "ontology", "validate", str(INVENTORY / "1.1.0"))
    assert outcome.code == 0
    model = compiled_model("inventory", "1.1.0")
    assert outcome.data["bundle"] == "inventory"
    assert (outcome.data["version"], outcome.data["artifact_hash"]) == (
        "1.1.0",
        model.artifact_hash,
    )
    assert outcome.data["modules"] == ["core", "inventory"]
    assert outcome.envelope["command"] == "ontology validate"


def test_validate_fails_with_model_invalid(run_cli: CliRunner) -> None:
    code = "model.referential.unknown_range"
    outcome = admin(run_cli, "ontology", "validate", str(BROKEN / code))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.model_invalid")
    assert code in [f["code"] for f in outcome.error["details"]["findings"]]


def test_validate_needs_the_main_module_when_ambiguous(run_cli: CliRunner, tmp_path: Path) -> None:
    source = tmp_path / "two"
    source.mkdir()
    shutil.copy(INVENTORY / "1.0.0" / "inventory.yaml", source / "inventory.yaml")
    shutil.copy(ONTOLOGIES / "notes_and_sources" / "1.0.0" / "notes_and_sources.yaml", source)
    outcome = admin(run_cli, "ontology", "validate", str(source))
    assert (outcome.code, outcome.error["code"]) == (2, "usage.missing_argument")
    assert admin(run_cli, "ontology", "validate", str(source), "--module", "inventory").code == 0
    missing = admin(run_cli, "ontology", "validate", str(source), "--module", "nope")
    assert (missing.code, missing.error["code"]) == (2, "usage.invalid_argument")


@pytest.mark.parametrize("expected", EXPECTED_DIFFS, ids=lambda d: f"{d['from']}-{d['to']}")
def test_diff_matches_the_classifier_goldens(
    run_cli: CliRunner, expected: dict[str, object]
) -> None:
    outcome = admin(
        run_cli,
        "ontology",
        "diff",
        str(INVENTORY / str(expected["to"])),
        "--against",
        str(INVENTORY / str(expected["from"])),
    )
    assert outcome.code == 0
    assert {k: v for k, v in outcome.data.items() if k != "bump"} == expected
    assert outcome.data["bump"] == "ok"


def test_diff_reports_an_insufficient_bump(run_cli: CliRunner) -> None:
    outcome = admin(
        run_cli, "ontology", "diff", str(INVENTORY / "1.0.0"), "--against", str(INVENTORY / "1.0.1")
    )
    assert (outcome.code, outcome.data["bump"]) == (0, "insufficient")


def test_diff_needs_exactly_one_baseline(run_cli: CliRunner) -> None:
    outcome = admin(run_cli, "ontology", "diff", str(INVENTORY / "1.0.1"))
    assert (outcome.code, outcome.error["code"]) == (2, "usage.missing_argument")
