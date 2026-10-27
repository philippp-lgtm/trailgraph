"""describe: the module index, ``describe_modules`` / ``describe_module`` and the ``module``
filter (B11.5, plan ``modules_for_agents`` step 2)."""

import json
from typing import Any

import pytest
from support.fixtures import (
    GOLDENS,
    MODULAR_ONTOLOGIES,
    TOY_ONTOLOGIES,
    assert_golden,
    compiled_model,
    ontology_versions,
)
from support.skills import graph_entry

from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES
from graph_cli.services.describe import (
    ModuleIndex,
    describe_module,
    describe_modules,
    describe_relations,
    describe_schema,
    describe_type,
    describe_types,
)


def index_of(name: str, version: str) -> tuple[Any, CompiledModel, ModuleIndex]:
    model = compiled_model(name, version)
    return graph_entry(model), model, ModuleIndex(model)


def by_id(modules: Any) -> dict[str, Any]:
    return {m["id"]: m for m in modules}


def links(module: dict[str, Any], key: str) -> list[str]:
    return [item.get("relation", item.get("curie")) for item in module[key]]


def test_builtin_modules_are_the_package_data() -> None:
    from importlib import resources

    shipped = resources.files("graph_cli.ontology_layer") / "modules"
    assert {
        p.name.removesuffix(".yaml") for p in shipped.iterdir() if p.is_file()
    } == BUILTIN_MODULES


def test_owner_follows_the_prefix_and_relations_are_split() -> None:
    _, model, index = index_of("company", "1.1.0")
    assert index.owner("org:Position") == "organization"
    assert index.owner("ppl:Person") == "people"
    assert index.owner("core:source") == "core"
    assert index.resolve("ops") == "operations" == index.resolve("operations")
    assert index.types("organization") == [
        "org:CostCenter",
        "org:Department",
        "org:OrgUnit",
        "org:Position",
        "org:Team",
    ]
    assert index.types("company") == []
    assert index.relations("people") == []
    assert index.internal("organization") == [
        "org:charged_to",
        "org:in_unit",
        "org:part_of",
        "org:reports_to",
    ]
    # holds and completed are declared in the module of their range, with the person on the left
    assert index.is_cross_module("org:holds") and index.is_cross_module("ops:completed")
    assert [link.relation for link in index.links_out("people")] == ["ops:completed", "org:holds"]
    assert [link.relation for link in index.links_in("people")] == []
    assert not any(index.is_cross_module(r) for r in index.internal("operations"))
    assert set(model.relations) >= set(index.relations("operations"))


def test_an_open_end_is_listed_once_under_the_declaring_module() -> None:
    _, _, index = index_of("company", "1.1.0")
    assert [link.relation for link in index.links_out("core")] == [
        "core:derived_from",
        "core:retracts",
        "core:source",
        "core:supersedes",
    ]
    assert index.links_in("core") == []
    assert index.links_out("people") != [] and "core:source" not in {
        link.relation for link in index.links_out("people")
    }


def test_unknown_module_suggests_names() -> None:
    entry, model, _ = index_of("company", "1.0.0")
    with pytest.raises(GraphCliError) as caught:
        describe_module(entry, model, "organisation")
    assert caught.value.code == "term.unknown"
    assert "organization" in caught.value.details["did_you_mean"]
    with pytest.raises(GraphCliError) as again:
        describe_types(entry, model, module="orgs")
    assert again.value.code == "term.unknown"


def test_modules_of_company_1_0_0() -> None:
    entry, model, _ = index_of("company", "1.0.0")
    answer = describe_modules(entry, model)
    modules = by_id(answer["modules"])
    assert list(modules) == sorted(modules) == ["company", "core", "organization", "people"]
    assert {m["id"] for m in answer["modules"] if m["builtin"]} == {"core"}
    assert answer["ontology"] == {"bundle": "company", "version": "1.0.0"}
    org = modules["organization"]
    assert (org["prefix"], org["version"], org["types"], org["relations"]) == ("org", "1.0.0", 5, 5)
    assert org["links"] == [{"module": "people", "out": 0, "in": 1}]
    assert modules["people"]["links"] == [{"module": "organization", "out": 1, "in": 0}]
    assert (modules["company"]["types"], modules["company"]["relations"]) == (0, 0)
    assert modules["company"]["links"] == []


def test_modules_of_company_1_1_0_add_operations() -> None:
    entry, model, _ = index_of("company", "1.1.0")
    modules = by_id(describe_modules(entry, model)["modules"])
    assert sorted(modules) == ["company", "core", "operations", "organization", "people"]
    assert modules["operations"]["links"] == [
        {"module": "organization", "out": 2, "in": 0},
        {"module": "people", "out": 0, "in": 1},
    ]
    assert modules["organization"]["links"] == [
        {"module": "operations", "out": 0, "in": 2},
        {"module": "people", "out": 0, "in": 1},
    ]


def test_module_organization_1_1_0() -> None:
    entry, model, _ = index_of("company", "1.1.0")
    for name in ("org", "organization"):
        module = describe_module(entry, model, name)["module"]
        assert (module["id"], module["prefix"], module["builtin"]) == ("organization", "org", False)
        assert [t["curie"] for t in module["types"]][:2] == ["org:CostCenter", "org:Department"]
        assert links(module, "relations") == [
            "org:charged_to",
            "org:in_unit",
            "org:part_of",
            "org:reports_to",
        ]
        assert links(module, "links_out") == []
        assert links(module, "links_in") == ["ops:owned_by", "ops:required_for", "org:holds"]
        assert [r["id"] for r in module["rules"]] == ["org.team_in_department"]
        assert module["rules"][0]["severity"] == "error"
        holds = next(i for i in module["links_in"] if i["relation"] == "org:holds")
        assert (holds["module"], holds["from"], holds["to"]) == (
            "organization",
            ["ppl:Person"],
            ["org:Position"],
        )
    people = describe_module(entry, model, "ppl")["module"]
    assert links(people, "links_out") == ["ops:completed", "org:holds"]
    assert people["rules"] == [] and people["relations"] == []
    core = describe_module(entry, model, "core")["module"]
    assert core["builtin"] is True
    assert core["links_out"][0]["from"] == ["any"] == core["links_out"][0]["to"]


def test_every_term_with_a_summary_carries_deprecated() -> None:
    entry, model, _ = index_of("company", "1.1.0")
    for module in describe_modules(entry, model)["modules"]:
        assert isinstance(module["builtin"], bool)
        detail = describe_module(entry, model, module["id"])["module"]
        for key in ("types", "relations", "links_out", "links_in", "rules"):
            assert all(isinstance(i["deprecated"], bool) for i in detail[key])


def test_type_names_its_module() -> None:
    entry, model, _ = index_of("company", "1.1.0")
    assert describe_type(entry, model, "Position")["type"]["module"] == "organization"
    assert describe_type(entry, model, "ppl:Person")["type"]["module"] == "people"


def test_filters_on_company() -> None:
    entry, model, _ = index_of("company", "1.1.0")
    whole = describe_types(entry, model)
    assert "module" not in whole
    types = describe_types(entry, model, module="ppl")
    assert types["module"] == "people"
    assert [t["curie"] for t in types["types"]] == ["ppl:Person"]
    relations = describe_relations(entry, model, module="people")
    assert [r["curie"] for r in relations["relations"]] == ["ops:completed", "org:holds"]
    org = describe_relations(entry, model, module="org")
    assert [r["curie"] for r in org["relations"]] == [
        "ops:owned_by",
        "ops:required_for",
        "org:charged_to",
        "org:holds",
        "org:in_unit",
        "org:part_of",
        "org:reports_to",
    ]
    schema = describe_schema(entry, model, module="people")
    assert schema["module"] == "people"
    assert [i["type"] for i in schema["labels"]] == ["ops:Training", "org:Position", "ppl:Person"]
    assert [i["relation"] for i in schema["relationships"]] == ["ops:completed", "org:holds"]
    # the filtered names are those of the whole model
    whole_schema = describe_schema(entry, model)
    names = {i["type"]: i["label"] for i in whole_schema["labels"]}
    assert {i["type"]: i["label"] for i in schema["labels"]} == {
        k: names[k] for k in ("ops:Training", "org:Position", "ppl:Person")
    }
    # an unfiltered answer equals the filtered one for the module that sees everything it has
    assert describe_schema(entry, model, module=None) == whole_schema


EQUAL_NAMES = {
    "alpha": """\
format: graph_cli.ir/1
module: {id: alpha, prefix: al, version: 1.0.0, summary: The first area.}
classes:
  Entry:
    summary: An entry of the first area.
    attributes:
      title: {summary: Title of the entry., type: string, required: true, role: label}
relations:
  follows: {summary: Follows in the first area., domain: Entry, range: Entry}
""",
    "beta": """\
format: graph_cli.ir/1
module: {id: beta, prefix: bt, version: 1.0.0, summary: The second area.}
classes:
  Entry:
    summary: An entry of the second area.
    attributes:
      title: {summary: Title of the entry., type: string, required: true, role: label}
relations:
  follows: {summary: Follows in the second area., domain: Entry, range: Entry}
""",
    "both": """\
format: graph_cli.ir/1
module:
  id: both
  prefix: both
  version: 1.0.0
  summary: The two areas together.
  imports:
    - {module: alpha, version: "^1.0"}
    - {module: beta, version: "^1.0"}
relations:
  mirrors: {summary: Counterpart in the second area., domain: al:Entry, range: bt:Entry}
""",
}


def test_filter_keeps_the_names_of_the_whole_schema() -> None:
    outcome = compile_bundle(
        "both", LayeredSource(MappingSource({k: v.encode() for k, v in EQUAL_NAMES.items()}))
    )
    model = outcome.model
    assert model is not None, [f.to_json() for f in outcome.findings]
    entry = graph_entry(model)
    schema = describe_schema(entry, model, module="alpha")
    assert [i["label"] for i in schema["labels"]] == ["`al:Entry`", "`bt:Entry`"]
    assert [i["pattern"] for i in schema["relationships"]] == [
        "(:`al:Entry`)-[:`al:follows`]->(:`al:Entry`)",
        "(:`al:Entry`)-[:mirrors]->(:`bt:Entry`)",
    ]
    only_beta = describe_schema(entry, model, module="bt")
    assert [i["label"] for i in only_beta["labels"]] == ["`al:Entry`", "`bt:Entry`"]
    both = describe_schema(entry, model, module="both")
    assert [i["relation"] for i in both["relationships"]] == ["both:mirrors"]
    assert describe_module(entry, model, "both")["module"]["relations"] == []
    assert describe_modules(entry, model)["modules"][0]["id"] == "alpha"
    assert describe_type(entry, model, "bt:Entry")["type"]["module"] == "beta"


def test_a_single_module_graph() -> None:
    entry, model, _ = index_of("inventory", ontology_versions("inventory")[0])
    modules = by_id(describe_modules(entry, model)["modules"])
    authored = [m for m in modules.values() if not m["builtin"]]
    assert len(authored) == 1
    assert authored[0]["links"] == []
    assert modules["core"]["builtin"] is True
    detail = describe_module(entry, model, authored[0]["prefix"])["module"]
    assert [t["curie"] for t in detail["types"]] == sorted(
        c for c in model.classes if c.startswith(authored[0]["prefix"] + ":")
    )
    assert describe_types(entry, model, module=authored[0]["id"])["types"] == [
        t
        for t in describe_types(entry, model)["types"]
        if t["curie"] in model.classes and t["curie"].startswith(authored[0]["prefix"] + ":")
    ]


def golden(name: str, document: dict[str, Any]) -> None:
    data = json.loads(json.dumps(document))
    del data["graph"]["id"]
    assert_golden(
        GOLDENS / "describe" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode()
    )


@pytest.mark.parametrize("ontology", [*TOY_ONTOLOGIES, *MODULAR_ONTOLOGIES])
def test_modules_goldens(ontology: str) -> None:
    for version in ontology_versions(ontology):
        entry, model, _ = index_of(ontology, version)
        golden(f"modules_{ontology}_{version}", describe_modules(entry, model))


@pytest.mark.parametrize(
    ("module", "version"),
    [
        ("organization", "1.0.0"),
        ("organization", "1.1.0"),
        ("operations", "1.1.0"),
        ("core", "1.1.0"),
    ],
)
def test_module_goldens_on_company(module: str, version: str) -> None:
    entry, model, _ = index_of("company", version)
    golden(f"module_company_{module}_{version}", describe_module(entry, model, module))


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_module_goldens_of_the_toys(ontology: str) -> None:
    entry, model, _ = index_of(ontology, ontology_versions(ontology)[0])
    authored = next(m for m in model.modules if m.id not in BUILTIN_MODULES)
    golden(f"module_{ontology}_{authored.id}", describe_module(entry, model, authored.id))
