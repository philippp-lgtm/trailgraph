"""S: graph-admin ontology release with a data migration (migration plan M1-M5, M9).

The ``inventory`` series 1.1.0 -> 2.0.0 on a graph with data: the ``name`` rename is derived
from ``renamed_from``, the removed enum value ``worn`` needs a ``map_values`` step. Without the
file the release is refused; with it the data is rewritten in place and ``audit verify`` is
clean.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.fixtures import ONTOLOGIES

INVENTORY = ONTOLOGIES / "inventory"

MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""

PLACES = [
    {
        "op": "create",
        "type": "inv:Location",
        "local": "room",
        "attrs": {"name": "Room", "code": "R1"},
    },
    {"op": "create", "type": "inv:Category", "local": "tools", "attrs": {"name": "Tools"}},
]


def item(local: str, name: str, condition: str) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"name": name, "condition": condition},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
        ],
    }


SEED = [*PLACES, item("hammer", "Hammer", "worn"), item("saw", "Saw", "good")]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(run_cli: CliRunner, version: str = "1.1.0") -> str:
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / version)
    )
    assert outcome.code == 0, outcome.envelope
    graph_id: str = outcome.data["id"]
    return graph_id


def major(tmp_path: Path, migration: str | None = MIGRATION, name: str = "v2") -> Path:
    """The 2.0.0 version directory of the fixture with (or without) its migration file."""
    target = tmp_path / name
    shutil.copytree(INVENTORY / "2.0.0", target)
    if migration is not None:
        (target / "migration.yaml").write_text(migration)
    return target


def release(run_cli: CliRunner, source: Path, *extra: str) -> Outcome:
    return admin(run_cli, "ontology", "release", "example_graph", str(source), *extra)


def items(run_cli: CliRunner) -> dict[str, dict[str, Any]]:
    """The items by alias, as ``find`` shows them."""
    found = app(run_cli, "find", "--type", "inv:Item", env=ENV)
    assert found.code == 0, found.envelope
    return {i["alias"]: i for i in found.data["items"]}


def audit_errors(run_cli: CliRunner) -> int:
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    errors: int = verified.data["counts"]["error"]
    return errors


def test_the_data_does_not_fit_without_the_file(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Renames that rewrite data need the file: the opt-in carries ``from`` and a summary."""
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    outcome = release(run_cli, major(tmp_path, None))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_required")
    details = outcome.error["details"]
    assert details["file"] == "migration.yaml" and details["migrated"] is False
    assert details["preview"]["entities"] == 2  # the items: the label rename
    assert "migration.yaml" in outcome.error["message"]
    assert outcome.error["hint"] == "graph-admin graph check <graph> --ontology <dir>"
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"


def test_the_renames_alone_leave_the_removed_value(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """An empty file is a migration with no steps; the data is judged after it."""
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    empty = MIGRATION.split("steps:")[0] + "steps: []\n"
    outcome = release(run_cli, major(tmp_path, empty))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_required")
    details = outcome.error["details"]
    assert details["migrated"] is True  # judged after the migration
    assert details["counts"] == {"data.structural.enum_value_unknown": 1}
    assert "after the migration" in outcome.error["message"]


def test_the_release_migrates_the_data_in_place(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    graph_id = create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    before = items(run_cli)
    assert {i["id"] for i in before.values()} and len(before) == 2
    source = major(tmp_path)
    outcome = release(run_cli, source, "--reason", "rename name to label")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["released"], outcome.data["pinned"]) == (True, True)
    block = outcome.data["migration"]
    assert block["file"] is True and block["completing"] is False
    assert block["summary"].startswith("The condition worn is gone")
    assert (block["entities"], block["unchanged"], block["edges"]) == (2, 2, 0)
    assert block["migrated"]["entities"] == 2
    assert block["migrated"]["by_step"] == {
        "rename_attribute inv:Item.name": 2,
        "steps[0].map_values": 1,
    }
    assert block["tx_seq"] == 2 and block["duration_s"] >= 0
    assert outcome.envelope["graph"]["ontology"]["version"] == "2.0.0"
    # the kept release carries the file, listed with its hash
    directory = root / "graphs" / graph_id / "ontology" / "2.0.0"
    assert (directory / "migration.yaml").read_text() == MIGRATION
    assert "migration.yaml" in json.loads((directory / "release.json").read_text())["files"]
    assert not (root / "graphs" / graph_id / "data" / "migration.json").exists()
    # ids, aliases and the data under the new names
    after = items(run_cli)
    assert set(after) == set(before)
    for alias, found in after.items():
        assert found["id"] == before[alias]["id"]
    hammer = app(run_cli, "get", before[next(a for a in before)]["id"], env=ENV)
    assert hammer.code == 0
    assert audit_errors(run_cli) == 0


def test_history_shows_one_migration_revision(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    hammer = next(i for i in items(run_cli).values() if i["attrs"].get("name") == "Hammer")
    assert release(run_cli, major(tmp_path), "--reason", "example reason").code == 0
    shown = app(run_cli, "get", hammer["id"], env=ENV).data
    entity = shown.get("entity", shown)
    assert entity["attrs"]["label"] == "Hammer" and entity["attrs"]["condition"] == "good"
    assert entity["rev"] == 2
    assert entity["alias"] == hammer["alias"] and entity["type"] == "inv:Item"


def test_a_dry_run_writes_nothing(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    graph_id = create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    graph_dir = root / "graphs" / graph_id

    def state() -> tuple[bytes, bytes, list[str]]:
        audit = b"".join(p.read_bytes() for p in sorted(root.rglob("*.jsonl")))
        manifest = (graph_dir / "manifest.toml").read_bytes()
        names = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        return audit, manifest, names

    before = state()
    outcome = release(run_cli, major(tmp_path), "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["released"], outcome.data["pinned"], outcome.data["dry_run"]) == (
        False,
        False,
        True,
    )
    block = outcome.data["migration"]
    assert block["entities"] == 2 and "migrated" not in block and "tx_seq" not in block
    assert [e["kind"] for e in block["steps"]] == ["map_values"]
    assert block["steps"][0]["entities"] == 1 and len(block["steps"][0]["samples"]) == 1
    assert {e["kind"] for e in block["renames"]} == {"rename_attribute"}
    assert state() == before
    assert not list(graph_dir.rglob("migration.json"))
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"
    # the same answer, run for real
    real = release(run_cli, major(tmp_path, name="again"))
    assert real.code == 0
    assert real.data["migration"]["entities"] == block["entities"]


def test_a_rename_only_release_needs_the_file_with_data(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A class renamed with ``renamed_from``: refused with the hint while data of it exists."""
    create(run_cli)
    supplier = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Example supplier"}}
    write_documents(root, "example_graph", [{"ops": [supplier]}])
    source = tmp_path / "renamed"
    source.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    text = text.replace("  Supplier:\n", "  Vendor:\n    renamed_from: Supplier\n", 1)
    text = text.replace("range: Supplier", "range: Vendor", 1)
    (source / "inventory.yaml").write_text(text)
    outcome = release(run_cli, source)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_required")
    assert "graph check" in outcome.error["hint"]
    (source / "migration.yaml").write_text(
        "format: graph_cli.migration/1\nfrom: 1.1.0\nsummary: Suppliers are vendors now.\n"
    )
    done = release(run_cli, source)
    assert done.code == 0, done.envelope
    assert done.data["migration"]["entities"] == 1
    found = app(run_cli, "find", "--type", "inv:Vendor", env=ENV)
    assert [i["alias"] for i in found.data["items"]] == ["SUP-0001"]  # the alias stays
    assert audit_errors(run_cli) == 0


def test_a_rename_without_data_needs_no_file(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": PLACES}])  # data, but none of Item
    source = tmp_path / "v2"
    shutil.copytree(INVENTORY / "2.0.0", source)
    outcome = release(run_cli, source)
    assert outcome.code == 0, outcome.envelope
    assert "migration" not in outcome.data


def test_a_migration_on_a_minor_release_is_refused(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    source = tmp_path / "minor"
    source.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (source / "inventory.yaml").write_text(
        text.replace("  version: 1.1.0\n", "  version: 1.2.0\n", 1)
    )
    (source / "migration.yaml").write_text(MIGRATION)
    outcome = release(run_cli, source)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_unexpected")


def test_from_must_be_the_pinned_version(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    outcome = release(run_cli, major(tmp_path, MIGRATION.replace("from: 1.1.0", "from: 1.0.0")))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_from_mismatch")
    assert outcome.error["details"]["expected"] == "1.1.0"


def test_an_invalid_file_names_the_paths(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    broken = MIGRATION.replace("worn: good", "worn: nonexistent")
    outcome = release(run_cli, major(tmp_path, broken))
    assert (outcome.code, outcome.error["code"]) == (1, "migration.invalid")
    problems = outcome.error["details"]["problems"]
    assert problems and problems[0]["path"].startswith("steps[0]")
    shape = release(run_cli, major(tmp_path, "format: wrong\n", name="shape"))
    assert (shape.code, shape.error["code"]) == (1, "migration.invalid")


def test_a_version_is_identified_with_its_file(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    assert release(run_cli, major(tmp_path)).code == 0
    again = release(run_cli, major(tmp_path, name="same"))  # the same file: nothing to do
    assert again.code == 0 and (again.data["released"], again.data["pinned"]) == (False, False)
    other = release(run_cli, major(tmp_path, MIGRATION + "\n", name="other"))
    assert other.code == 0  # the same document, only formatted differently
    changed = release(
        run_cli, major(tmp_path, MIGRATION.replace("worn: good", "worn: new"), name="changed")
    )
    assert (changed.code, changed.error["code"]) == (1, "admin.release_exists")
    assert "migration_hash" in changed.error["details"]
    without = release(run_cli, major(tmp_path, None, name="without"))
    assert (without.code, without.error["code"]) == (1, "admin.release_exists")


def test_unused_steps_warn(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": [*PLACES, item("saw", "Saw", "good")]}])
    outcome = release(run_cli, major(tmp_path))
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == ["admin.migration_step_unused"]
    assert outcome.warnings[0]["details"] == {"index": 0, "step": "map_values"}
    assert outcome.data["migration"]["unused_steps"] == [0]


def test_graph_check_previews_the_migration(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    source = major(tmp_path)
    plain = admin(run_cli, "graph", "check", "example_graph")
    assert plain.code == 0 and "migration" not in plain.data
    checked = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(source))
    assert checked.code == 0, checked.envelope
    assert checked.data["counts"]["error"] == 0
    block = checked.data["migration"]
    assert block["entities"] == 2 and block["file"] is True
    assert block["steps"][0]["by_type"] == {"inv:Item": 1}
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"
    # without the file the data is checked as it is, under the new version
    plain_source = major(tmp_path, None, name="plain")
    refused = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(plain_source))
    assert (refused.code, refused.error["code"]) == (1, "admin.data_errors")


def test_graph_push_releases_with_the_migration(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    workspace = tmp_path / "workspace"
    assert admin(run_cli, "graph", "pull", "example_graph", str(workspace)).code == 0
    shutil.copytree(INVENTORY / "2.0.0", workspace / "ontology" / "2.0.0")
    (workspace / "ontology" / "2.0.0" / "migration.yaml").write_text(MIGRATION)
    dry = admin(
        run_cli, "graph", "push", "example_graph", str(workspace), "--dry-run", "--reason", "r"
    )
    assert dry.code == 0, dry.envelope
    assert dry.data["ontology"]["migration"]["entities"] == 2 and dry.data["dry_run"] is True
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"
    pushed = admin(run_cli, "graph", "push", "example_graph", str(workspace), "--reason", "r")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["ontology"]["migration"]["migrated"]["entities"] == 2
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "2.0.0"
    assert audit_errors(run_cli) == 0
    # a second push of the same workspace changes nothing
    again = admin(run_cli, "graph", "push", "example_graph", str(workspace))
    assert again.code == 0 and again.data["unchanged"] is True


def test_a_release_without_a_migration_keeps_its_answer_and_events(
    run_cli: CliRunner, root: Path
) -> None:
    """No file and no renames: the answer has no migration keys, the intents keep the key
    sets older verifiers check, and no marker or migration event appears."""
    graph_id = create(run_cli, "1.0.0")
    outcome = release(run_cli, INVENTORY / "1.0.1")
    assert outcome.code == 0, outcome.envelope
    assert set(outcome.data) == {
        "graph_id",
        "from",
        "to",
        "released",
        "pinned",
        "diff",
        "alias_prefixes_added",
    }
    from graph_cli.audit.verify import verify_stream
    from graph_cli.kernel.ports.audit import graph_stream
    from graph_cli.storage.files.audit_log import FileAuditLog

    lines = FileAuditLog(root).read_lines(graph_stream(graph_id))
    report = verify_stream(lines, stream=graph_stream(graph_id))
    changes = {e["actions"][0]: set(e["change"]) for e in report.events if "change" in e}
    assert changes == {"maint.ontology_release": {"release"}, "maint.ontology_pin": {"pin"}}
    assert not (root / "graphs" / graph_id / "data" / "migration.json").exists()
    release_dir = root / "graphs" / graph_id / "ontology" / "1.0.1"
    assert not (release_dir / "migration.yaml").exists()
