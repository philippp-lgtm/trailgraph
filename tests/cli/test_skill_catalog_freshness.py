"""S: installed graph skills and the catalogs (graph start plan S5 and S6).

A function, action or view that is published, changed or removed without being featured leaves
every installed copy fresh: for ``skills check`` and for the freshness check of every command.
Featuring or unfeaturing one makes the copies stale on the next read and refuses a write until
the copy is refreshed; the same rule holds for a marker from before the digest was recorded (not
judged by the command, reported by ``skills check``, refreshed by ``start``).
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import FIXTURES

from graph_cli.kernel.skill_marker import MARKER_FILE

SOURCES = {
    kind: FIXTURES / kind / "inventory" / "valid" for kind in ("functions", "actions", "views")
}
TEMPLATE = FIXTURES / "skills" / "inventory_actions" / "graph-example-graph"
POINTER = 'format_version = 1\ngraphs = ["example_graph"]\n'
FIND = ("find", "--type", "inv:Location", "--graph", "example_graph")
WRITE = (
    "create",
    "inv:Location",
    "--set",
    "name=Shelf",
    "--set",
    "code=S1",
    "--graph",
    "example_graph",
)


def publish(run_cli: CliRunner, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def edited(tmp_path: Path, kind: str, name: str, change: Any) -> Path:
    """A copy of the fixture folder of ``kind``, with ``change(files)`` applied to its texts."""
    folder = tmp_path / f"{kind}_{name}"
    shutil.copytree(SOURCES[kind], folder)
    files = {p.name: p.read_text() for p in folder.glob("*.yaml")}
    change(files)
    for stale in folder.glob("*.yaml"):
        stale.unlink()
    for file, text in files.items():
        (folder / file).write_text(text)
    return folder


@pytest.fixture
def project(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind, folder in SOURCES.items():
        publish(run_cli, kind, folder)
    publish(run_cli, "skill", TEMPLATE)
    directory = tmp_path / "project"
    (directory / ".claude").mkdir(parents=True)
    (directory / ".graph_cli.toml").write_text(POINTER)
    installed = app(run_cli, "skills", "install", "--scope", "project", "--dir", str(directory))
    assert installed.code == 0, installed.envelope
    done = app(
        run_cli,
        "skills",
        "install",
        "--scope",
        "project",
        "--dir",
        str(directory),
        "--project-graphs",
    )
    assert done.code == 0, done.envelope
    return directory


def states(run_cli: CliRunner, project: Path) -> dict[str, str]:
    checked = app(
        run_cli, "skills", "check", "--scope", "project", "--dir", str(project), cwd=project
    )
    return {s["skill"]: s["status"] for s in checked.data["skills"]}


def marker_of(project: Path) -> Path:
    return project / ".claude" / "skills" / "graph-example-graph" / MARKER_FILE


def assert_everything_fresh(run_cli: CliRunner, project: Path) -> None:
    assert states(run_cli, project) == {"graph-cli": "fresh", "graph-example-graph": "fresh"}
    read = app(run_cli, *FIND, cwd=project)
    assert read.code == 0 and read.warning_codes == [], read.envelope
    written = app(run_cli, *WRITE, "--dry-run", cwd=project)
    assert written.code == 0, written.envelope


def test_the_marker_records_what_the_render_shows_of_the_catalogs(
    run_cli: CliRunner, project: Path
) -> None:
    source = json.loads(marker_of(project).read_text())["source"]
    assert source["featured"].startswith("sha256:") and len(source["featured"]) == 71
    assert_everything_fresh(run_cli, project)


def test_publishing_what_is_not_featured_leaves_every_copy_fresh(
    run_cli: CliRunner, project: Path, tmp_path: Path
) -> None:
    """A new, a changed, and a removed function, action and view; none of them featured."""
    before = marker_of(project).read_bytes()

    def functions(files: dict[str, str]) -> None:
        files["items_by_condition.yaml"] = files["items_by_condition.yaml"].replace(
            "Items in a given physical condition.", "Items by their physical condition."
        )
        files["items_extra.yaml"] = files["items_by_condition.yaml"].replace(
            "name: items_by_condition", "name: items_extra"
        )

    def actions(files: dict[str, str]) -> None:
        files["retire_item.yaml"] = files["retire_item.yaml"].replace(
            "summary: ", "summary: Also ", 1
        )
        files["retire_again.yaml"] = files["retire_item.yaml"].replace(
            "name: retire_item", "name: retire_again"
        )

    def views(files: dict[str, str]) -> None:
        files["item_catalog_copy.yaml"] = files["item_catalog.yaml"].replace(
            "name: item_catalog", "name: item_catalog_copy"
        )
        del files["items_in_location.yaml"]

    for kind, change in (("functions", functions), ("actions", actions), ("views", views)):
        publish(run_cli, kind, edited(tmp_path, kind, "plain", change))
        assert_everything_fresh(run_cli, project)
    assert marker_of(project).read_bytes() == before  # not even the marker is different


def test_featuring_an_entry_makes_the_copies_stale_and_a_refresh_makes_them_fresh(
    run_cli: CliRunner, project: Path, tmp_path: Path
) -> None:
    def feature(files: dict[str, str]) -> None:
        files["retire_item.yaml"] = files["retire_item.yaml"].replace(
            "\nname: retire_item", "\nfeatured: true\nname: retire_item"
        )

    publish(run_cli, "actions", edited(tmp_path, "actions", "featured", feature))
    assert states(run_cli, project)["graph-example-graph"] == "stale"
    read = app(run_cli, *FIND, cwd=project)
    assert read.code == 0 and read.warning_codes == ["skills.stale"], read.envelope
    (entry,) = read.envelope["warnings"][0]["details"]["skills"]
    assert entry["installed"]["featured"] != entry["current"]["featured"]
    refused = app(run_cli, *WRITE, cwd=project)
    assert (refused.code, refused.error["code"]) == (1, "skills.outdated")
    refreshed = app(run_cli, "start", cwd=project)
    assert {(s["skill"], s["state"]) for s in refreshed.data["skills"]} == {
        ("graph-cli", "fresh"),
        ("graph-example-graph", "refreshed"),
    }
    assert_everything_fresh(run_cli, project)
    skill = (project / ".claude/skills/graph-example-graph/SKILL.md").read_text()
    assert "- `retire_item(" in skill  # the new featured line


def test_unfeaturing_a_function_the_template_does_not_list_still_says_stale(
    run_cli: CliRunner, project: Path, tmp_path: Path
) -> None:
    """The digest covers the whole featured projection, whichever blocks a template uses."""

    def unfeature(files: dict[str, str]) -> None:
        files["items_in_location.yaml"] = files["items_in_location.yaml"].replace(
            "featured: true", "featured: false"
        )

    publish(run_cli, "functions", edited(tmp_path, "functions", "unfeatured", unfeature))
    assert states(run_cli, project)["graph-example-graph"] == "stale"
    read = app(run_cli, *FIND, cwd=project)
    assert read.warning_codes == ["skills.stale"]


def test_a_marker_from_before_the_digest_is_not_judged_but_is_reported_and_refreshed(
    run_cli: CliRunner, project: Path, tmp_path: Path
) -> None:
    document = json.loads(marker_of(project).read_text())
    del document["source"]["featured"]
    marker_of(project).write_text(json.dumps(document))

    def feature(files: dict[str, str]) -> None:
        files["retire_item.yaml"] = files["retire_item.yaml"].replace(
            "\nname: retire_item", "\nfeatured: true\nname: retire_item"
        )

    publish(run_cli, "actions", edited(tmp_path, "actions", "featured", feature))
    read = app(run_cli, *FIND, cwd=project)
    assert read.code == 0 and read.warning_codes == [], read.envelope  # nothing to judge it by
    assert states(run_cli, project)["graph-example-graph"] == "stale"  # one rule for check
    refreshed = app(run_cli, "start", cwd=project)
    assert ("graph-example-graph", "refreshed") in {
        (s["skill"], s["state"]) for s in refreshed.data["skills"]
    }
    assert "featured" in json.loads(marker_of(project).read_text())["source"]
    assert_everything_fresh(run_cli, project)


def test_a_legacy_slot_without_the_stored_digest_is_judged_through_the_compiled_catalog(
    run_cli: CliRunner, project: Path, isolated_env: dict[str, str]
) -> None:
    """A slot published before the digest was stored has no ``featured`` key: the freshness check
    computes it from the compiled catalog and gets the same digest."""
    slots = sorted(Path(isolated_env["XDG_DATA_HOME"]).glob("**/functions/slot.json"))
    assert len(slots) == 1
    document = json.loads(slots[0].read_text())
    assert document.pop("featured").startswith("sha256:")
    slots[0].write_text(json.dumps(document))
    assert_everything_fresh(run_cli, project)


def test_the_legacy_fallback_is_computed_once_per_graph_and_kind_in_a_process(
    run_cli: CliRunner,
    project: Path,
    isolated_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graph_cli.catalog import file_catalog
    from graph_cli.kernel.ids import GraphId, parse_id

    (slot,) = sorted(Path(isolated_env["XDG_DATA_HOME"]).glob("**/functions/slot.json"))
    stored = json.loads(slot.read_text())
    document = {key: value for key, value in stored.items() if key != "featured"}
    slot.write_text(json.dumps(document))
    graph = GraphId(parse_id(slot.parents[1].name))
    catalog = file_catalog.FileCatalog(slot.parents[3])
    reads: list[str] = []
    real = file_catalog.read_slot

    def counting(layout: Any, kind: str, *, sources: bool) -> Any:
        reads.append(kind)
        return real(layout, kind, sources=sources)

    monkeypatch.setattr(file_catalog, "read_slot", counting)
    first = catalog.skill_featured(graph)
    assert reads == ["functions"]  # only the slot without the stored digest is computed
    assert [catalog.skill_featured(graph) for _ in range(3)] == [first] * 3
    assert reads == ["functions"]  # and once
    slot.write_text(json.dumps(stored))  # the stored digest gives the same combined digest
    assert file_catalog.FileCatalog(slot.parents[3]).skill_featured(graph) == first
