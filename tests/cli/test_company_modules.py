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
from support.app import ENV, app, create_graph
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


@pytest.fixture
def company(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.0")
    assert release(run_cli, "1.1.0").code == 0


def test_describe_modules_lists_the_subject_areas(run_cli: CliRunner, company: None) -> None:
    outcome = app(run_cli, "describe", "modules", env=ENV)
    assert outcome.code == 0, outcome.envelope
    modules = {m["id"]: m for m in outcome.data["modules"]}
    assert list(modules) == sorted(modules)
    assert set(modules) == {"core", *V110_MODULES}
    assert [m["id"] for m in outcome.data["modules"] if m["builtin"]] == ["core"]
    assert modules["company"]["types"] == 0 and modules["company"]["relations"] == 0
    assert modules["organization"]["prefix"] == "org"
    assert {"module": "people", "out": 0, "in": 1} in modules["organization"]["links"]

    text = run_cli("graph", "describe", "modules", env=ENV)
    assert text.returncode == 0
    assert "organization [org] 1.0.0  5 types, 5 relations" in text.stdout
    assert "core [core] 1.0.0 (built-in)" in text.stdout
    assert "links: operations (0 out, 2 in), people (0 out, 1 in)" in text.stdout


def test_describe_module_by_id_and_by_prefix(run_cli: CliRunner, company: None) -> None:
    by_id = app(run_cli, "describe", "module", "organization", env=ENV)
    by_prefix = app(run_cli, "describe", "module", "org", env=ENV)
    assert by_id.code == 0, by_id.envelope
    assert by_id.data["module"] == by_prefix.data["module"]
    module = by_id.data["module"]
    assert module["id"] == "organization" and module["builtin"] is False
    assert {t["curie"] for t in module["types"]} == {
        "org:CostCenter",
        "org:Department",
        "org:OrgUnit",
        "org:Position",
        "org:Team",
    }
    assert {r["curie"] for r in module["relations"]} == {
        "org:charged_to",
        "org:in_unit",
        "org:part_of",
        "org:reports_to",
    }
    assert {(k["relation"], k["module"]) for k in module["links_in"]} == {
        ("org:holds", "organization"),
        ("ops:owned_by", "operations"),
        ("ops:required_for", "operations"),
    }
    assert [r["id"] for r in module["rules"]] == ["org.team_in_department"]

    text = run_cli("graph", "describe", "module", "org", env=ENV)
    assert text.returncode == 0
    assert "org:Team extends org:OrgUnit" in text.stdout
    assert "ppl:Person -org:holds-> org:Position  [people]" in text.stdout
    assert "org.team_in_department (error)" in text.stdout
    people = run_cli("graph", "describe", "module", "people", env=ENV)
    assert "Links from other modules:\n  -\n" in people.stdout  # an empty section shows "-"
    assert "ppl:Person -org:holds-> org:Position  [organization]" in people.stdout
    assert "1 types, 0 relations" not in people.stdout
    modules = run_cli("graph", "describe", "modules", env=ENV)
    assert "1 type, 0 relations" in modules.stdout


def test_an_unknown_module_suggests_the_known_ones(run_cli: CliRunner, company: None) -> None:
    for args in (("module", "orgs"), ("types", "--module", "orgs"), ("schema", "--module", "orgs")):
        refused = app(run_cli, "describe", *args, env=ENV)
        assert (refused.code, refused.error["code"]) == (1, "term.unknown"), args
        assert refused.error["details"]["kind"] == "module"
        assert "org" in refused.error["details"]["did_you_mean"]
        assert refused.envelope["error"]["hint"] == "graph describe modules"


def test_the_module_filter_limits_types_relations_and_schema(
    run_cli: CliRunner, company: None
) -> None:
    types = app(run_cli, "describe", "types", "--module", "ops", env=ENV)
    assert types.code == 0, types.envelope
    assert types.data["module"] == "operations"
    assert {t["curie"] for t in types.data["types"]} == {
        "ops:Guideline",
        "ops:Process",
        "ops:Training",
    }
    relations = app(run_cli, "describe", "relations", "--module", "people", env=ENV)
    assert relations.data["module"] == "people"
    assert {r["curie"] for r in relations.data["relations"]} == {
        "org:holds",
        "ops:completed",
    }
    schema = app(run_cli, "describe", "schema", "--module", "operations", "--compact", env=ENV)
    assert schema.code == 0, schema.envelope
    assert schema.data["module"] == "operations"
    assert all("properties" not in item for item in schema.data["labels"])
    patterns = {r["pattern"] for r in schema.data["relationships"]}
    assert "(:Process)-[:owned_by]->(:Position)" in patterns
    assert {item["label"] for item in schema.data["labels"]} == {
        "Guideline",
        "Person",
        "Position",
        "Process",
        "Training",
    }
    everything = app(run_cli, "describe", "types", env=ENV)
    assert "module" not in everything.data

    text = run_cli("graph", "describe", "schema", "--module", "ops", "--compact", env=ENV)
    assert text.returncode == 0
    assert "(:Process)-[:owned_by]->(:Position)" in text.stdout
    assert "(:Team)" not in text.stdout


def test_a_single_module_graph_has_one_authored_module(run_cli: CliRunner, root: Path) -> None:
    create_graph(run_cli, "example_graph", "inventory")
    modules = app(run_cli, "describe", "modules", env=ENV).data["modules"]
    authored = [m for m in modules if not m["builtin"]]
    assert len(authored) == 1 and authored[0]["links"] == []
    only = app(run_cli, "describe", "module", authored[0]["prefix"], env=ENV)
    assert only.code == 0, only.envelope
    assert {t["curie"] for t in only.data["module"]["types"]} == {
        t["curie"] for t in app(run_cli, "describe", "types", env=ENV).data["types"]
    }
    assert only.data["module"]["links_out"] == only.data["module"]["links_in"] == []


def test_describe_search_finds_terms_of_the_model(run_cli: CliRunner, company: None) -> None:
    found = app(run_cli, "describe", "search", "position", "--limit", "3", env=ENV)
    assert found.code == 0, found.envelope
    assert found.data["text"] == "position"
    assert found.data["items"][0]["curie"] == "org:Position"
    assert found.envelope["page"]["total"] == 12 and found.envelope["page"]["next_offset"] == 3
    hint = found.envelope["hints"][0]
    assert hint["command"] == (
        "graph describe search position --graph example_graph --limit 3 --offset 3"
    )

    narrowed = app(
        run_cli,
        *("describe", "search", "name", "--kind", "attribute", "--kind", "state"),
        *("--module", "ppl"),
        env=ENV,
    )
    assert narrowed.code == 0, narrowed.envelope
    assert narrowed.data["module"] == "people"
    assert {i["kind"] for i in narrowed.data["items"]} == {"attribute"}
    assert {i["module"] for i in narrowed.data["items"]} == {"people"}

    text = run_cli("graph", "describe", "search", "position", env=ENV)
    assert text.returncode == 0
    first = text.stdout.splitlines()[0]
    assert first.startswith("type  org:Position  [organization]  A slot in the org chart")
    assert first.endswith("<name: exact>")


def test_describe_search_without_hits_hints_at_the_wider_search(
    run_cli: CliRunner, company: None
) -> None:
    empty = app(
        run_cli, "describe", "search", "position", "--kind", "state", "--module", "ppl", env=ENV
    )
    assert empty.code == 0 and empty.data["items"] == []
    assert empty.envelope["page"]["total"] == 0
    assert [h["command"] for h in empty.envelope["hints"]] == [
        "graph describe search position --graph example_graph"
    ]
    text = run_cli("graph", "describe", "search", "zzzqqq", env=ENV)
    assert text.returncode == 0 and text.stdout == "-\n"


def test_describe_search_errors(run_cli: CliRunner, company: None) -> None:
    short = app(run_cli, "describe", "search", "a", env=ENV)
    assert (short.code, short.error["code"]) == (2, "usage.invalid_argument")
    module = app(run_cli, "describe", "search", "name", "--module", "orgs", env=ENV)
    assert (module.code, module.error["code"]) == (1, "term.unknown")
    kind = app(run_cli, "describe", "search", "name", "--kind", "field", env=ENV)
    assert kind.code == 2


PERSON_TO_DEPARTMENT = {
    "direct": [],
    "paths": [
        [
            {"relation": "org:holds", "from": "ppl:Person", "to": "org:Position"},
            {"relation": "org:in_unit", "from": "org:Position", "to": "org:Department"},
        ]
    ],
}


def test_a_refused_link_names_the_relations_that_connect_the_types(
    run_cli: CliRunner, root: Path
) -> None:
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", seed_documents("changes.jsonl"))
    person = app(run_cli, "get", "PER-0005", env=ENV)
    department = app(run_cli, "get", "DEPT-0001", env=ENV)
    assert person.code == department.code == 0, (person.envelope, department.envelope)
    args = ("link", "PER-0005", "org:in_unit", "DEPT-0001")

    refused = app(run_cli, *args, env=ENV)
    assert refused.code == 1, refused.envelope
    assert refused.error["code"] == "write.invalid"
    [finding] = refused.error["details"]["findings"]
    assert finding["code"] == "data.referential.domain_violation"
    assert finding["severity"] == "error"
    assert finding["connecting"] == PERSON_TO_DEPARTMENT

    text = run_cli("graph", *args, env=ENV)
    assert text.returncode == 1
    assert (
        "    connect with: ppl:Person -org:holds-> org:Position -org:in_unit-> org:Department\n"
        in text.stderr
    )


def test_a_refused_range_names_the_relations_that_connect_the_types(
    run_cli: CliRunner, root: Path
) -> None:
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", seed_documents("changes.jsonl"))
    refused = app(run_cli, "link", "POS-0001", "org:reports_to", "PER-0005", env=ENV)
    assert refused.code == 1, refused.envelope
    [finding] = refused.error["details"]["findings"]
    assert finding["code"] == "data.referential.range_violation"
    assert finding["actual"] == "ppl:Person"
    assert finding["connecting"] == {
        "direct": [{"relation": "org:holds", "from": "ppl:Person", "to": "org:Position"}],
        "paths": [],
    }


def test_the_batch_check_names_the_relations_that_connect_the_types(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Under a stricter authored version (``part_of`` only leaves departments) the existing
    team-to-department edges violate the domain; the department -> team direction fits."""
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", seed_documents("changes.jsonl"))
    stricter = tmp_path / "stricter"
    shutil.copytree(COMPANY / "1.1.0", stricter)
    organization = stricter / "organization.yaml"
    text = organization.read_text()
    narrowed = text.replace(
        "    domain: OrgUnit\n    range: OrgUnit\n",
        "    domain: Department\n    range: OrgUnit\n",
        1,
    )
    assert narrowed != text
    organization.write_text(narrowed)

    outcome = admin(
        run_cli,
        "graph",
        "check",
        "example_graph",
        "--ontology",
        str(stricter),
        "--module",
        "company",
    )
    assert outcome.error is not None, outcome.envelope
    findings = [
        f
        for f in outcome.error["details"]["findings"]
        if f["code"] == "data.referential.domain_violation"
    ]
    assert findings
    assert all(f["connecting"] is not None for f in findings)
    team = next(f for f in findings if f["target"]["type"] == "org:Team")
    assert team["connecting"]["direct"] == [
        {"relation": "org:part_of", "from": "org:Department", "to": "org:Team"}
    ]


def test_a_prefixed_relationship_type_in_a_query_gets_the_hint(
    run_cli: CliRunner, root: Path
) -> None:
    create(run_cli, "1.0.0")
    refused = app(run_cli, "query", "MATCH (a)-[:org:holds]->(b) RETURN count(a) AS n", env=ENV)
    assert refused.code == 2 and refused.error["code"] == "query.syntax"
    assert refused.error["hint"] == "graph describe schema"
    assert "in backticks" in refused.error["message"]
    quoted = app(run_cli, "query", "MATCH (a)-[:`org:holds`]->(b) RETURN count(a) AS n", env=ENV)
    assert quoted.code == 0, quoted.envelope
