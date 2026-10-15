"""S: graph-admin graph create, list and show (contracts B1.11, B5.2, B12.4, W.f; plan M7)."""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, data_root, init
from support.fixtures import BROKEN, ONTOLOGIES, compiled_model
from support.trees import tree_hash

from graph_cli.audit.verify import check_anchors, verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory" / "1.0.0"
NOTES = ONTOLOGIES / "notes_and_sources" / "1.0.0"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(
    run_cli: CliRunner, name: str, source: Path = INVENTORY, *extra: str
) -> dict[str, object]:
    outcome = admin(run_cli, "graph", "create", name, "--ontology", str(source), *extra)
    assert outcome.code == 0, outcome.envelope
    data: dict[str, object] = outcome.data
    return data


@pytest.mark.parametrize(
    ("ontology", "source"), [("inventory", INVENTORY), ("notes_and_sources", NOTES)]
)
def test_create_publishes_a_graph_with_its_events(
    run_cli: CliRunner, root: Path, ontology: str, source: Path
) -> None:
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(source), "--title", "Example"
    )
    assert outcome.code == 0
    model = compiled_model(ontology)
    data = outcome.data
    assert (data["name"], data["title"], data["state"]) == ("example_graph", "Example", "active")
    assert data["ontology"] == {
        "bundle": model.bundle_id,
        "version": model.bundle_version,
        "artifact_hash": model.artifact_hash,
    }
    assert outcome.envelope["graph"]["seq"] == 0
    graph_dir = root / "graphs" / data["id"]
    assert (graph_dir / "ontology" / model.bundle_version / "release.json").is_file()
    audit = FileAuditLog(root)
    graph_report = verify_stream(
        audit.read_lines(graph_stream(data["id"])), stream=graph_stream(data["id"])
    )
    installation = verify_stream(audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM)
    assert graph_report.findings == () and installation.findings == ()
    (genesis,) = graph_report.events
    (announced,) = installation.events
    assert genesis["actions"] == announced["actions"] == ["maint.graph_create"]
    assert genesis["actor"]["client"]["entrypoint"] == "maint"
    assert announced["anchors"] == [{"graph_id": data["id"], "seq": 1, "hash": genesis["hash"]}]
    assert check_anchors(installation.events, {data["id"]: graph_report.events}) == []
    assert announced["env"] == {"root_source": "default", "data_root": str(root)}


def test_alias_prefixes_are_seeded_from_display_prefixes(run_cli: CliRunner, root: Path) -> None:
    data = create(run_cli, "example_graph")
    model = compiled_model("inventory")
    expected = {
        c: d.display_prefix for c, d in model.classes.items() if d.display_prefix and not d.abstract
    }
    assert data["alias_prefixes"] == expected


def test_list_and_show(run_cli: CliRunner, root: Path) -> None:
    assert admin(run_cli, "graph", "list").data == {"items": []}
    first = create(run_cli, "zeta")
    create(run_cli, "alpha", NOTES, "--title", "Example notes")
    items = admin(run_cli, "graph", "list").data["items"]
    assert [(i["name"], i["title"]) for i in items] == [("alpha", "Example notes"), ("zeta", None)]
    assert [i["language"] for i in items] == [None, None]
    for ref in ("zeta", str(first["id"])):
        shown = admin(run_cli, "graph", "show", ref)
        assert shown.data["id"] == first["id"]
        assert (shown.data["seq"], shown.data["releases"]) == (0, ["1.0.0"])
        assert shown.envelope["graph"]["name"] == "zeta"
    missing = admin(run_cli, "graph", "show", "nope")
    assert (missing.code, missing.error["code"]) == (1, "graph.not_found")


def test_language_is_stored_in_canonical_case(run_cli: CliRunner, root: Path) -> None:
    data = create(run_cli, "example_graph", INVENTORY, "--language", "de-ch")
    assert data["language"] == "de-CH"
    manifest = root / "graphs" / str(data["id"]) / "manifest.toml"
    assert 'language = "de-CH"' in manifest.read_text()
    assert admin(run_cli, "graph", "show", "example_graph").data["language"] == "de-CH"
    (item,) = admin(run_cli, "graph", "list").data["items"]
    assert item["language"] == "de-CH"
    graph_report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(str(data["id"]))),
        stream=graph_stream(str(data["id"])),
    )
    (genesis,) = graph_report.events
    model = compiled_model("inventory")
    pin = OntologyPin(model.bundle_id, model.bundle_version, model.artifact_hash).to_json()
    args = {"name": "example_graph", "title": None, "language": "de-CH", "ontology": pin}
    assert genesis["request"]["args_digest"] == RequestMeta.of("graph create", args).args_digest


@pytest.mark.parametrize("tag", ["de_CH", "x-foo", "i-klingon", ""])
def test_invalid_language_is_a_usage_error(run_cli: CliRunner, root: Path, tag: str) -> None:
    before = tree_hash(root)
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY), "--language", tag
    )
    assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument")
    assert outcome.error["details"]["argument"] == "language"
    assert tree_hash(root) == before


def test_name_taken(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "example_graph")
    outcome = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.graph_name_taken")
    assert outcome.error["hint"] == "graph-admin graph list"


@pytest.mark.parametrize(
    ("name", "source", "exit_code", "code"),
    [
        ("Bad-Name", INVENTORY, 2, "usage.invalid_argument"),
        ("example_graph", BROKEN / "model.structural.duplicate_term", 1, "admin.model_invalid"),
    ],
)
def test_refused_creates_leave_nothing(
    run_cli: CliRunner, root: Path, name: str, source: Path, exit_code: int, code: str
) -> None:
    before = tree_hash(root)
    outcome = admin(run_cli, "graph", "create", name, "--ontology", str(source))
    assert (outcome.code, outcome.error["code"]) == (exit_code, code)
    assert tree_hash(root) == before


def test_missing_data_root_needs_init(run_cli: CliRunner, isolated_env: dict[str, str]) -> None:
    for args in (("graph", "list"), ("graph", "create", "g", "--ontology", str(INVENTORY))):
        outcome = admin(run_cli, *args)
        assert (outcome.code, outcome.error["code"]) == (5, "backend.unavailable")
        assert outcome.error["hint"] == "graph-admin init"
    assert not data_root(isolated_env).exists()


def test_reads_leave_the_data_root_unchanged(run_cli: CliRunner, root: Path) -> None:
    graph = create(run_cli, "example_graph")
    before = tree_hash(root)
    newer = ONTOLOGIES / "inventory" / "1.0.1"
    for args in (
        ("graph", "list"),
        ("graph", "show", "example_graph"),
        ("ontology", "validate", str(newer)),
        ("ontology", "diff", str(newer), "--graph", str(graph["id"])),
    ):
        assert admin(run_cli, *args).code == 0, args
    assert tree_hash(root) == before


def test_diff_against_a_graph(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "example_graph")
    newer = ONTOLOGIES / "inventory" / "1.1.0"
    outcome = admin(run_cli, "ontology", "diff", str(newer), "--graph", "example_graph")
    assert (outcome.data["from"], outcome.data["to"], outcome.data["level"]) == (
        "1.0.0",
        "1.1.0",
        "minor",
    )
    assert outcome.envelope["graph"]["name"] == "example_graph"
