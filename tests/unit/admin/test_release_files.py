"""The files of a release directory (contracts B7.4, B7.5; plan M7)."""

import json

import pytest
from support.clock import FixedClock
from support.fixtures import BROKEN, GOLDENS, ONTOLOGIES, assert_golden, compiled_model
from support.ids import SeededIdProvider

from graph_cli.kernel.canonical import hash_bytes, hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin import error_codes
from graph_cli_admin.services_admin.ontology import compile_source, release_files

INVENTORY = ONTOLOGIES / "inventory"


@pytest.fixture(autouse=True)
def admin_codes() -> None:
    error_codes.register()


def build(version: str, previous: str | None) -> dict[str, bytes]:
    clock = FixedClock("2026-10-15T08:00:00Z")
    compiled = compile_source(INVENTORY / version)
    old = compiled_model("inventory", previous) if previous else None
    return release_files(
        compiled, previous=old, release_id=SeededIdProvider(clock).new_id(), released_at=clock.now()
    )


def test_release_layout_and_metadata_golden() -> None:
    files = build("1.0.1", "1.0.0")
    exports = sorted(name for name in files if name.startswith("exports/jsonschema/"))
    assert exports and "exports/jsonschema/inv.Item.json" in exports
    assert sorted(set(files) - set(exports)) == [
        "compiled.json",
        "diff.json",
        "release.json",
        "source/inventory.yaml",
    ]
    release = json.loads(files["release.json"])
    assert release["files"] == {
        name: hash_bytes(data) for name, data in files.items() if name != "release.json"
    }
    assert release["artifact_hash"] == hash_value(parse_json(files["compiled.json"]))
    assert release["classification"] == {"declared": "patch", "computed": "patch"}
    release["compiler"]["version"] = "<version>"  # the package version must not churn the golden
    assert_golden(
        GOLDENS / "admin" / "release_inventory_1.0.1.json",
        (json.dumps(release, indent=2, sort_keys=True) + "\n").encode(),
    )


def test_first_release_has_no_diff() -> None:
    files = build("1.0.0", None)
    assert "diff.json" not in files
    release = json.loads(files["release.json"])
    assert "previous" not in release and "classification" not in release
    assert files["source/inventory.yaml"] == (INVENTORY / "1.0.0" / "inventory.yaml").read_bytes()


def test_release_files_are_deterministic() -> None:
    assert build("1.1.0", "1.0.1") == build("1.1.0", "1.0.1")


def test_compile_source_refuses_error_findings() -> None:
    with pytest.raises(GraphCliError) as caught:
        compile_source(BROKEN / "model.structural.duplicate_term")
    assert caught.value.code == "admin.model_invalid"
