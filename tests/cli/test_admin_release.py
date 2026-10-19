"""S: graph-admin ontology release (contracts B7.4, B7.12, B7.14, W.e; plan M7).

One scenario per refusal code plus the inventory version series.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.fixtures import ONTOLOGIES, compiled_model

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(run_cli: CliRunner, version: str = "1.0.0") -> str:
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / version)
    )
    assert outcome.code == 0, outcome.envelope
    graph_id: str = outcome.data["id"]
    return graph_id


def release(run_cli: CliRunner, source: Path) -> Outcome:
    return admin(run_cli, "ontology", "release", "example_graph", str(source))


def variant(tmp_path: Path, base: str, version: str, *, summary: str | None = None) -> Path:
    """An authored copy of a fixture version under another version number."""
    text = (INVENTORY / base / "inventory.yaml").read_text()
    text = text.replace(f"  version: {base}\n", f"  version: {version}\n", 1)
    if summary is not None:
        text = text.replace("  summary: ", f"  summary: {summary} ", 1)
    target = tmp_path / f"{base}_as_{version}"
    target.mkdir()
    (target / "inventory.yaml").write_text(text)
    return target


def test_the_inventory_series(run_cli: CliRunner, root: Path) -> None:
    graph_id = create(run_cli)
    series = (("1.0.1", "patch"), ("1.1.0", "minor"), ("2.0.0", "major"), ("2.1.0", "minor"))
    for version, level in series:
        outcome = release(run_cli, INVENTORY / version)
        assert outcome.code == 0, outcome.envelope
        assert (outcome.data["released"], outcome.data["pinned"]) == (True, True)
        assert outcome.data["diff"]["level"] == level
        model = compiled_model("inventory", version)
        assert outcome.data["to"]["artifact_hash"] == model.artifact_hash
        assert outcome.envelope["graph"]["ontology"]["version"] == version
        assert (root / "graphs" / graph_id / "ontology" / version / "diff.json").is_file()
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["releases"] == ["1.0.0", "1.0.1", "1.1.0", "2.0.0", "2.1.0"]
    assert shown["ontology"]["version"] == "2.1.0"
    audit = FileAuditLog(root)
    graph_report = verify_stream(
        audit.read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    installation = verify_stream(audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM)
    assert graph_report.findings == () and installation.findings == ()
    assert [e["actions"][0] for e in graph_report.events] == [
        "maint.graph_create",
        *["maint.ontology_release", "maint.ontology_pin"] * 4,
    ]
    assert len(installation.events) == 9  # create plus one companion per operation


def test_rerunning_a_pinned_release_changes_nothing(run_cli: CliRunner, root: Path) -> None:
    create(run_cli)
    assert release(run_cli, INVENTORY / "1.0.1").code == 0
    before = FileAuditLog(root).head(INSTALLATION_STREAM)
    outcome = release(run_cli, INVENTORY / "1.0.1")
    assert outcome.code == 0
    assert (outcome.data["released"], outcome.data["pinned"]) == (False, False)
    assert FileAuditLog(root).head(INSTALLATION_STREAM) == before


def test_a_release_seeds_the_alias_prefixes_of_added_classes(
    run_cli: CliRunner, root: Path
) -> None:
    """Classes new in a release get their display prefix; existing prefixes stay."""
    create(run_cli)
    before = admin(run_cli, "graph", "show", "example_graph").data["alias_prefixes"]
    outcome = release(run_cli, INVENTORY / "1.1.0")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["alias_prefixes_added"] == {"inv:Supplier": "SUP"}
    after = admin(run_cli, "graph", "show", "example_graph").data["alias_prefixes"]
    assert after == {**before, "inv:Supplier": "SUP"}
    supplier = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Example supplier"}}
    write_documents(root, "example_graph", [{"ops": [supplier]}])
    found = app(run_cli, "find", "--type", "inv:Supplier", env=ENV)
    assert [item["alias"] for item in found.data["items"]] == ["SUP-0001"]


def test_release_exists(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    other = variant(tmp_path, "1.0.0", "1.0.0", summary="Changed.")
    outcome = release(run_cli, other)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.release_exists")


def test_version_bump_insufficient(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    minor_as_patch = variant(tmp_path, "1.1.0", "1.0.2")
    outcome = release(run_cli, minor_as_patch)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.version_bump_insufficient")
    assert outcome.error["details"] == {"from": "1.0.0", "to": "1.0.2", "level": "minor"}


def test_downgrades_are_insufficient(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.1")
    outcome = release(run_cli, INVENTORY / "1.0.0")
    assert (outcome.code, outcome.error["code"]) == (1, "admin.version_bump_insufficient")


def test_version_bump_excessive_is_a_warning(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    patch_as_minor = variant(tmp_path, "1.0.1", "1.1.0")
    outcome = release(run_cli, patch_as_minor)
    assert outcome.code == 0
    assert outcome.warning_codes == ["admin.version_bump_excessive"]


LOCATIONS = [
    {
        "op": "create",
        "type": "inv:Location",
        "local": "room",
        "attrs": {"name": "Room", "code": "R1"},
    },
    {
        "op": "create",
        "type": "inv:Location",
        "local": "shelf",
        "attrs": {"name": "Shelf", "code": "R1-S1"},
        "edges": [{"rel": "inv:within", "target": "@room"}],
    },
]
ITEM = {
    "op": "create",
    "type": "inv:Item",
    "attrs": {"name": "Hammer"},
    "edges": [{"rel": "inv:located_in", "target": "@shelf"}],
}


def test_migration_required(run_cli: CliRunner, root: Path) -> None:
    """B7.14: data with error findings under the new major refuses the release."""
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", [{"ops": [*LOCATIONS, ITEM]}])
    outcome = release(run_cli, INVENTORY / "2.0.0")
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_required")
    details = outcome.error["details"]
    assert details["seq"] == 1
    assert details["counts"] == {
        "data.referential.cardinality_out": 1,  # in_category tightened to 1..n
        "data.structural.missing_required": 1,  # label
        "data.structural.unknown_attribute": 1,  # name, renamed to label
    }
    assert {f["code"] for f in details["findings"]} == set(details["counts"])
    show = admin(run_cli, "graph", "show", "example_graph")
    assert show.data["ontology"]["version"] == "1.1.0"


def test_major_release_on_data_that_checks_clean(run_cli: CliRunner, root: Path) -> None:
    """B7.14: no migration needed when check_graph finds no errors under the new major."""
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", [{"ops": LOCATIONS}])
    outcome = release(run_cli, INVENTORY / "2.0.0")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["pinned"] is True and outcome.data["to"]["version"] == "2.0.0"


def test_archived_graph(run_cli: CliRunner, root: Path) -> None:
    graph_id = create(run_cli)
    manifest = root / "graphs" / graph_id / "manifest.toml"
    manifest.write_text(manifest.read_text().replace('state = "active"', 'state = "archived"'))
    outcome = release(run_cli, INVENTORY / "1.0.1")
    assert (outcome.code, outcome.error["code"]) == (1, "graph.archived")
