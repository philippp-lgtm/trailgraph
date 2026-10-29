"""T: published skill and function catalog against their change events (K6, R.6).

``integrity.audit.slot_mismatch`` lives here because it needs published slots.
Both are published with the real ``graph-admin`` commands and changed by hand
afterwards, as a same-user agent could.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.fixtures import FIXTURES

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.canonical import canonical_bytes, hash_value

SKILL = FIXTURES / "skills" / "graph-example-graph"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    return root


def codes(run_cli: CliRunner) -> list[str]:
    outcome = admin(run_cli, "audit", "verify")
    if outcome.code == 0:
        return [f["code"] for f in outcome.data["findings"]]
    return [f["code"] for f in outcome.error["details"]["findings"]]


def publish_skill(run_cli: CliRunner, source: Path = SKILL) -> None:
    outcome = admin(run_cli, "graph", "skill", "publish", "example_graph", str(source))
    assert outcome.code == 0, outcome.envelope


def publish_functions(run_cli: CliRunner, source: Path = FUNCTIONS) -> None:
    outcome = admin(run_cli, "graph", "functions", "publish", "example_graph", str(source))
    assert outcome.code == 0, outcome.envelope


def graph_dir(run_cli: CliRunner, root: Path) -> Path:
    graph_id = admin(run_cli, "graph", "show", "example_graph").data["id"]
    return root / "graphs" / graph_id


def first_file(directory: Path) -> Path:
    return sorted(p for p in directory.rglob("*") if p.is_file())[0]


def test_published_slots_verify_clean(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    publish_skill(run_cli)
    publish_functions(run_cli)
    one = tmp_path / "one"
    one.mkdir()
    shutil.copy(FUNCTIONS / "items_in_location.yaml", one)
    publish_functions(run_cli, one)  # a second hash, then the empty catalog
    empty = tmp_path / "empty"
    empty.mkdir()
    publish_functions(run_cli, empty)
    assert codes(run_cli) == []


def test_a_graph_without_slots_verifies_clean(run_cli: CliRunner, root: Path) -> None:
    assert codes(run_cli) == []


def edit_skill_file(directory: Path) -> None:
    first_file(directory / "skill" / "files").write_text("edited by hand\n")


def remove_skill(directory: Path) -> None:
    shutil.rmtree(directory / "skill")


def edit_function_source(directory: Path) -> None:
    first_file(directory / "functions" / "files").write_text("edited by hand\n")


def remove_functions(directory: Path) -> None:
    shutil.rmtree(directory / "functions")


def edit_compiled(directory: Path) -> None:
    path = first_file(directory / "functions" / "compiled")
    path.write_bytes(path.read_bytes().replace(b"items_in_location", b"items_anywhere"))


def place_functions_by_hand(directory: Path) -> None:
    """A catalog nobody published: only its slot is rewritten to another hash."""
    slot = directory / "functions" / "slot.json"
    slot.write_bytes(slot.read_bytes().replace(b'"hash": "sha256:', b'"hash": "sha256:0'))


TAMPERS: dict[str, Callable[[Path], None]] = {
    "edited skill file": edit_skill_file,
    "removed skill": remove_skill,
    "edited function source": edit_function_source,
    "removed functions": remove_functions,
    "edited compiled catalog": edit_compiled,
    "rewritten slot": place_functions_by_hand,
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_a_changed_slot_is_a_slot_mismatch(run_cli: CliRunner, root: Path, tamper: str) -> None:
    publish_skill(run_cli)
    publish_functions(run_cli)
    TAMPERS[tamper](graph_dir(run_cli, root))
    assert codes(run_cli) == [audit_codes.SLOT_MISMATCH]


def test_a_slot_placed_without_a_publish_is_a_slot_mismatch(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A copy of another graph's published catalog, put in place by hand."""
    publish_functions(run_cli)
    other = create_graph(run_cli, "other_graph")
    shutil.copytree(graph_dir(run_cli, root) / "functions", root / "graphs" / other / "functions")
    assert codes(run_cli) == [audit_codes.SLOT_MISMATCH]


def test_a_rollback_to_an_earlier_catalog_is_a_slot_mismatch(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    publish_functions(run_cli)
    directory = graph_dir(run_cli, root) / "functions"
    saved = tmp_path / "saved"
    shutil.copytree(directory, saved)
    one = tmp_path / "one"
    one.mkdir()
    shutil.copy(FUNCTIONS / "items_in_location.yaml", one)
    publish_functions(run_cli, one)
    assert codes(run_cli) == []
    shutil.rmtree(directory)
    shutil.copytree(saved, directory)
    assert codes(run_cli) == [audit_codes.SLOT_MISMATCH]


def swap_compiled_and_slot(directory: Path) -> None:
    """Replace the compiled catalog by another one and make slot.json name it, as a
    same-user agent could; the sources and their hash stay untouched."""
    functions = directory / "functions"
    slot_path = functions / "slot.json"
    slot = json.loads(slot_path.read_text())
    old = functions / "compiled" / f"{slot['compiled_hash'].removeprefix('sha256:')}.json"
    doc = json.loads(old.read_text())
    doc["functions"] = doc["functions"][:-1]
    new_hash = hash_value(doc)
    new = old.with_name(f"{new_hash.removeprefix('sha256:')}.json")
    new.write_bytes(canonical_bytes(doc))
    old.unlink()
    slot["compiled_hash"] = new_hash
    slot_path.write_text(json.dumps(slot, indent=2) + "\n")


def test_a_replaced_compiled_catalog_with_an_adjusted_slot_is_detected(
    run_cli: CliRunner, root: Path
) -> None:
    publish_functions(run_cli)
    assert codes(run_cli) == []
    swap_compiled_and_slot(graph_dir(run_cli, root))
    outcome = admin(run_cli, "audit", "verify")
    assert outcome.code != 0
    findings = outcome.error["details"]["findings"]
    assert [f["code"] for f in findings] == [audit_codes.SLOT_MISMATCH]
    assert "compiled" in findings[0]["message"]


def test_untouched_compiled_catalog_verifies_after_republish(
    run_cli: CliRunner, root: Path
) -> None:
    publish_functions(run_cli)
    publish_functions(run_cli)
    assert codes(run_cli) == []
