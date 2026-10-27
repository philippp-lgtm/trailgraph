"""S: a graph whose ontology is authored as several modules (the ``company`` fixture).

Characterizes what exists with more than one authored module: release and pin of a
graph, a later release that adds a module under a bumped main module, the diff, a
pull and push round trip that writes every module file, the batch check with the
seeds, and ``describe`` with equal local names in two modules.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.fixtures import FIXTURES, ONTOLOGIES, compiled_model
from support.trees import tree_hash

from graph_cli.kernel.changeset import read_documents

COMPANY = ONTOLOGIES / "company"
SEEDS = FIXTURES / "seeds" / "company"
BASE = ".graph_cli_source.json"
V100_MODULES = ["company", "organization", "people"]
V110_MODULES = ["company", "operations", "organization", "people"]


def seed_documents(name: str) -> list[dict[str, Any]]:
    documents = read_documents((SEEDS / name).read_bytes())
    return [dict(document) for document in documents]  # type: ignore[arg-type]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(run_cli: CliRunner, version: str) -> Outcome:
    outcome = admin(
        run_cli,
        "graph",
        "create",
        "example_graph",
        "--ontology",
        str(COMPANY / version),
        "--module",
        "company",
    )
    assert outcome.code == 0, outcome.envelope
    return outcome


def release(run_cli: CliRunner, version: str) -> Outcome:
    return admin(
        run_cli,
        "ontology",
        "release",
        "example_graph",
        str(COMPANY / version),
        "--module",
        "company",
    )


def test_each_version_validates_with_all_its_modules(run_cli: CliRunner) -> None:
    for version, modules in (("1.0.0", V100_MODULES), ("1.1.0", V110_MODULES)):
        outcome = admin(
            run_cli, "ontology", "validate", str(COMPANY / version), "--module", "company"
        )
        assert outcome.code == 0, outcome.envelope
        model = compiled_model("company", version)
        assert outcome.data["findings"] == []
        assert outcome.data["bundle"] == "company"
        assert outcome.data["version"] == version
        assert outcome.data["artifact_hash"] == model.artifact_hash
        assert sorted(outcome.data["modules"]) == sorted(["core", *modules])


def test_the_main_module_has_to_be_named(run_cli: CliRunner) -> None:
    outcome = admin(run_cli, "ontology", "validate", str(COMPANY / "1.0.0"))
    assert (outcome.code, outcome.error["code"]) == (2, "usage.missing_argument")


def test_create_pins_the_main_module_and_release_adds_the_new_module(
    run_cli: CliRunner, root: Path
) -> None:
    created = create(run_cli, "1.0.0")
    graph_id = created.data["id"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["bundle"] == "company"
    assert shown["ontology"]["version"] == "1.0.0"
    assert shown["ontology"]["artifact_hash"] == compiled_model("company", "1.0.0").artifact_hash
    stored = root / "graphs" / graph_id / "ontology" / "1.0.0" / "source"
    assert sorted(p.stem for p in stored.glob("*.yaml")) == V100_MODULES

    released = release(run_cli, "1.1.0")
    assert released.code == 0, released.envelope
    assert (released.data["released"], released.data["pinned"]) == (True, True)
    assert released.data["diff"]["level"] == "minor"
    assert released.data["to"]["artifact_hash"] == compiled_model("company", "1.1.0").artifact_hash
    stored = root / "graphs" / graph_id / "ontology" / "1.1.0" / "source"
    assert sorted(p.stem for p in stored.glob("*.yaml")) == V110_MODULES
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"
    assert admin(run_cli, "audit", "verify").code == 0
    again = release(run_cli, "1.1.0")
    assert (again.data["released"], again.data["pinned"]) == (False, False)


def test_the_diff_of_a_module_added_under_a_bumped_main_module(run_cli: CliRunner) -> None:
    outcome = admin(
        run_cli,
        "ontology",
        "diff",
        str(COMPANY / "1.1.0"),
        "--against",
        str(COMPANY / "1.0.0"),
        "--module",
        "company",
    )
    assert outcome.code == 0, outcome.envelope
    report = {k: v for k, v in outcome.data.items() if k != "bump"}
    assert (report["from"], report["to"], report["level"]) == ("1.0.0", "1.1.0", "minor")
    kinds = {(c["kind"], c["path"]) for c in report["changes"]}
    assert ("add_import", "modules.operations") in kinds
    assert {path for kind, path in kinds if kind == "add_class"} == {
        "classes.ops:Guideline",
        "classes.ops:Process",
        "classes.ops:Training",
    }
    assert {c["level"] for c in report["changes"]} == {"minor"}
    assert outcome.data["bump"] == "ok"
    golden = json.loads((COMPANY / "expected_diffs.json").read_text())["diffs"]
    assert golden == [report]


def test_seeds_apply_and_the_batch_check_passes(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.0")
    write_documents(root, "example_graph", seed_documents("changes.jsonl"))
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.data["findings"] == []
    assert checked.data["ontology"] == {"bundle": "company", "version": "1.0.0"}

    types = app(run_cli, "describe", "types", env=ENV)
    curies = {t["curie"] for t in types.data["types"]}
    assert {"ppl:Person", "org:Position", "org:Team", "org:OrgUnit"} <= curies

    holders = app(
        run_cli,
        "query",
        "MATCH (p:Person)-[h:holds]->(q:Position) RETURN count(h) AS n",
        env=ENV,
    )
    assert holders.code == 0, holders.envelope
    assert holders.data["items"] == [{"n": 4}]


def test_a_graph_upgraded_to_the_new_module_takes_its_seeds(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.0")
    base = app(run_cli, "apply", "-", env=ENV, input=(SEEDS / "changes.jsonl").read_text())
    assert base.code == 0, base.envelope
    assert release(run_cli, "1.1.0").code == 0
    # The operations seeds name the positions and the person by alias.
    more = app(
        run_cli, "apply", "-", env=ENV, input=(SEEDS / "changes_operations.jsonl").read_text()
    )
    assert more.code == 0, more.envelope
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.data["findings"] == []
    assert checked.data["ontology"] == {"bundle": "company", "version": "1.1.0"}


def test_a_team_outside_a_department_is_refused(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.0")
    write_documents(root, "example_graph", seed_documents("changes.jsonl"))
    refused = app(
        run_cli,
        "create",
        "org:Team",
        "--set-json",
        'name="Stray team"',
        "--edge",
        "org:part_of=TEAM-0001",
        env=ENV,
    )
    assert refused.code == 1, refused.envelope
    assert refused.error["code"] == "write.invalid"
    [finding] = refused.error["details"]["findings"]
    assert finding["rule_id"] == "org.team_in_department"


def pulled(run_cli: CliRunner, tmp_path: Path, *args: str) -> Path:
    target = tmp_path / "work"
    outcome = admin(run_cli, "graph", "pull", "example_graph", str(target), *args)
    assert outcome.code == 0, outcome.envelope
    return target


def test_pull_writes_every_module_file_and_an_unchanged_push_is_a_no_op(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.0.0")
    before = tree_hash(root)
    target = tmp_path / "work"
    outcome = admin(run_cli, "graph", "pull", "example_graph", str(target))
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["files"] == [
        BASE,
        *[f"ontology/1.0.0/{m}.yaml" for m in V100_MODULES],
    ]
    assert sorted(outcome.data["ontology"]["modules"]) == V100_MODULES
    for module in V100_MODULES:
        assert (target / "ontology" / "1.0.0" / f"{module}.yaml").read_bytes() == (
            COMPANY / "1.0.0" / f"{module}.yaml"
        ).read_bytes()
    assert tree_hash(root) == before

    pushed = admin(run_cli, "graph", "push", "example_graph", str(target))
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["unchanged"] is True
    assert tree_hash(root) == before


def test_push_releases_a_version_with_a_new_module(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.0.0")
    target = pulled(run_cli, tmp_path)
    shutil.copytree(COMPANY / "1.1.0", target / "ontology" / "1.1.0")
    pushed = admin(run_cli, "graph", "push", "example_graph", str(target))
    assert pushed.code == 0, pushed.envelope
    assert (pushed.data["ontology"]["released"], pushed.data["ontology"]["pinned"]) == (True, True)
    assert pushed.data["ontology"]["to"]["version"] == "1.1.0"
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"

    fresh = tmp_path / "fresh"
    again = admin(run_cli, "graph", "pull", "example_graph", str(fresh))
    assert again.code == 0, again.envelope
    assert sorted(again.data["ontology"]["modules"]) == V110_MODULES
    for module in V110_MODULES:
        assert (fresh / "ontology" / "1.1.0" / f"{module}.yaml").is_file()
    assert json.loads((fresh / BASE).read_text())["ontology"]["version"] == "1.1.0"


EQUAL_NAMES = {
    "alpha": """\
format: graph_cli.ir/1
module:
  id: alpha
  prefix: al
  version: 1.0.0
  summary: The first area, with an Entry and a link relation.
classes:
  Entry:
    summary: An entry of the first area.
    attributes:
      title: {summary: Title of the entry., type: string, required: true, role: label}
relations:
  follows:
    summary: The entry follows another entry of the first area.
    domain: Entry
    range: Entry
""",
    "beta": """\
format: graph_cli.ir/1
module:
  id: beta
  prefix: bt
  version: 1.0.0
  summary: The second area, with an Entry and a link relation of the same names.
classes:
  Entry:
    summary: An entry of the second area.
    attributes:
      title: {summary: Title of the entry., type: string, required: true, role: label}
relations:
  follows:
    summary: The entry follows another entry of the second area.
    domain: Entry
    range: Entry
""",
    "both": """\
format: graph_cli.ir/1
module:
  id: both
  prefix: both
  version: 1.0.0
  summary: The main module that brings the two areas together.
  imports:
    - {module: alpha, version: "^1.0"}
    - {module: beta, version: "^1.0"}
relations:
  mirrors:
    summary: The entry of the first area has a counterpart in the second area.
    domain: al:Entry
    range: bt:Entry
""",
}


def test_equal_local_names_in_two_modules_stay_apart(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "equal_names"
    source.mkdir()
    for module, text in EQUAL_NAMES.items():
        (source / f"{module}.yaml").write_text(text)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(source), "--module", "both"
    )
    assert created.code == 0, created.envelope

    types = app(run_cli, "describe", "types", env=ENV)
    assert types.code == 0, types.envelope
    assert {t["curie"] for t in types.data["types"]} >= {"al:Entry", "bt:Entry"}
    relations = app(run_cli, "describe", "relations", env=ENV)
    assert {r["curie"] for r in relations.data["relations"]} >= {
        "al:follows",
        "bt:follows",
        "both:mirrors",
    }

    schema = app(run_cli, "describe", "schema", env=ENV)
    assert schema.code == 0, schema.envelope
    patterns = [r["pattern"] for r in schema.data["relationships"]]
    # Equal local names are told apart by their prefix; unique ones stay bare.
    assert "(:`al:Entry`)-[:`al:follows`]->(:`al:Entry`)" in patterns
    assert "(:`bt:Entry`)-[:`bt:follows`]->(:`bt:Entry`)" in patterns
    assert "(:`al:Entry`)-[:mirrors]->(:`bt:Entry`)" in patterns
    labels = {item["label"] for item in schema.data["labels"]}
    assert {"`al:Entry`", "`bt:Entry`"} <= labels and "Entry" not in labels
    for item in schema.data["relationships"]:
        query = app(run_cli, "query", f"MATCH {item['pattern']} RETURN count(*) AS n", env=ENV)
        assert query.code == 0, (item["pattern"], query.envelope)
