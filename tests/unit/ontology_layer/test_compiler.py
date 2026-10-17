"""IR compiler on the toy ontologies (contracts B7.5, B7.7, B7.13, test_fixtures.md)."""

import pytest
from support.fixtures import (
    GOLDENS,
    TOY_ONTOLOGIES,
    assert_golden,
    compile_fixture,
    compiled_model,
    ontology_versions,
)

from graph_cli.kernel.canonical import canonical_bytes, hash_value, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel, load_compiled, model_from_json

ALL_VERSIONS = [(name, v) for name in TOY_ONTOLOGIES for v in ontology_versions(name)]


@pytest.mark.parametrize(("name", "version"), ALL_VERSIONS)
def test_positive_control_has_zero_findings(name: str, version: str) -> None:
    outcome = compile_fixture(name, version)
    assert [f.to_json() for f in outcome.findings] == []
    assert outcome.model is not None


@pytest.mark.parametrize("module", ["core", "provenance", "files"])
def test_builtin_modules_compile_cleanly(module: str) -> None:
    outcome = compile_bundle(module, LayeredSource())
    assert outcome.findings == ()
    assert outcome.model is not None


@pytest.mark.parametrize(("name", "version"), ALL_VERSIONS)
def test_artifact_hash_is_stable(name: str, version: str) -> None:
    first, second = compiled_model(name, version), compiled_model(name, version)
    assert first.artifact_hash == second.artifact_hash
    assert canonical_bytes(first.to_json()) == canonical_bytes(second.to_json())


@pytest.mark.parametrize(
    ("name", "version"),
    [
        *((name, ontology_versions(name)[0]) for name in TOY_ONTOLOGIES),
        ("notes_and_sources", "1.1.0"),
    ],
)
def test_compiled_golden(name: str, version: str) -> None:
    data = pretty_bytes(compiled_model(name, version).to_json())
    assert_golden(GOLDENS / "compiled" / f"{name}_{version}.json", data)


@pytest.mark.parametrize(("name", "version"), ALL_VERSIONS)
def test_typed_view_round_trip(name: str, version: str) -> None:
    model = compiled_model(name, version)
    assert model_from_json(model.to_json()) == model


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_load_compiled_checks_the_pin(name: str) -> None:
    model = compiled_model(name)
    data = pretty_bytes(model.to_json())
    assert load_compiled(data, expected_hash=model.artifact_hash) == model
    tampered = data.replace(b'"summary": "', b'"summary": "x', 1)
    for bad, expected_hash in ((tampered, model.artifact_hash), (b"{", model.artifact_hash)):
        with pytest.raises(GraphCliError) as info:
            load_compiled(bad, expected_hash=expected_hash)
        assert info.value.code == "ontology.pin_mismatch"


@pytest.mark.parametrize("where", ["rule", "guard"])
def test_unknown_expression_node_is_an_unsupported_version(where: str) -> None:
    # A model released for a newer graph: a stale program refuses it clearly at load.
    document = compiled_model("inventory").to_json()
    newer = {"ge": [{"avg": {"rel": "inv:part_of", "attr": "weight"}}, {"const": 1}]}
    if where == "rule":
        document["rules"][0]["expr"] = {"not": newer}  # type: ignore[index]
    else:
        lifecycle = document["lifecycles"]["inv:ItemLifecycle"]  # type: ignore[index,call-overload]
        guarded = next(t for t in lifecycle["transitions"] if t["guards"])
        guarded["guards"][0]["expr"] = newer
    with pytest.raises(GraphCliError) as info:
        load_compiled(pretty_bytes(document), expected_hash=hash_value(document))
    assert info.value.code == "format.unsupported_version"
    assert info.value.details["node"] == "avg"
    assert "update graph" in info.value.message


def test_claim_is_opt_in() -> None:
    with_claims = compiled_model("notes_and_sources")
    without = compiled_model("inventory")
    assert "provenance:Claim" in with_claims.classes
    assert [m.id for m in with_claims.modules] == ["core", "provenance", "notes_and_sources"]
    assert [m.id for m in without.modules] == ["core", "inventory"]
    assert not any(curie.startswith("provenance:") for curie in without.term_curies())


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_core_relations_are_always_present(name: str) -> None:
    relations = compiled_model(name).relations
    assert {"core:supersedes", "core:retracts", "core:derived_from", "core:source"} <= set(
        relations
    )
    assert relations["core:supersedes"].domain == ()  # any class


def test_inherited_attributes_keep_their_declaring_class() -> None:
    model = compiled_model("notes_and_sources")
    page = model.classes["nas:WebPage"]
    assert page.extends == "nas:Source"
    assert page.ancestors == ("nas:Source",)
    assert page.attributes["title"].declared_in == "nas:Source"
    assert page.attributes["url"].declared_in == "nas:WebPage"
    assert list(page.attributes)[:2] == ["title", "retrieved_at"]


def test_transitions_are_expanded_and_guards_lowered() -> None:
    lifecycle = compiled_model("inventory").lifecycles["inv:ItemLifecycle"]
    retire = [t for t in lifecycle.transitions if t.to_state == "retired"]
    assert [t.from_state for t in retire] == ["ordered", "in_stock", "in_use"]
    (use,) = [t for t in lifecycle.transitions if t.to_state == "in_use"]
    assert use.guards[0].id == "inv.usable"
    assert use.guards[0].expr == {"not": {"eq": [{"attr": "condition"}, {"const": "damaged"}]}}


def test_rules_are_lowered_with_implies() -> None:
    rules = {r.id: r for r in compiled_model("inventory").rules}
    assert rules["inv.serial_single_unit"].expr == {
        "implies": [
            {"exists": {"attr": "serial_number"}},
            {"eq": [{"attr": "quantity"}, {"const": 1}]},
        ]
    }
    assert list(rules) == sorted(rules)


_IMPORTS_PROVENANCE = """\
format: graph_cli.ir/1
module:
  id: example_module
  prefix: ex
  version: 1.0.0
  summary: Imports provenance and refers to its terms.
  imports:
    - {module: provenance, version: 1.0.0}

classes:
  Finding:
    summary: A claim found while working.
    is_a: Claim
    attributes:
      label:
        summary: Display label.
        type: strnig

rules:
  - id: is_supported
    summary: A finding is supported.
    severity: warning
    when: batch
    scope: Finding
    then:
      - {count: {rel: supported_by}, minimum_value: 1}
"""


def test_unknown_references_name_their_likely_candidates() -> None:
    source = LayeredSource(MappingSource({"example_module": _IMPORTS_PROVENANCE.encode()}))
    outcome = compile_bundle("example_module", source)
    found = {f.code: f for f in outcome.findings}
    relation = found["model.referential.unknown_relation"]
    assert relation.expected == ["provenance:supported_by"]
    assert relation.message.endswith("did you mean 'provenance:supported_by'?")
    assert found["model.referential.unknown_parent"].expected == ["provenance:Claim"]
    assert found["model.structural.unknown_datatype"].expected == ["string"]


_ROLES = """\
format: graph_cli.ir/1
module:
  id: example_module
  prefix: ex
  version: 1.0.0
  summary: Label roles on string attributes.

datatypes:
  ShortText:
    summary: One line of text.
    base: string
    single_line: true

classes:
  Base:
    summary: Common base.
    attributes:
      name:
        summary: Main name.
        type: ShortText
        role: label
  Thing:
    summary: Something that is tracked.
    is_a: Base
    attributes:
      synonyms:
        summary: Other names.
        type: string
        many: true
        role: alt_label
      misspellings:
        summary: Searchable, never shown.
        type: string
        many: true
        role: hidden_label
      code:
        summary: Not a label.
        type: string
"""


def roles_model() -> CompiledModel:
    source = LayeredSource(MappingSource({"example_module": _ROLES.encode()}))
    outcome = compile_bundle("example_module", source)
    assert outcome.model is not None and not outcome.findings, outcome.findings
    return outcome.model


def test_label_roles_compile_and_are_inherited() -> None:
    thing = roles_model().classes["ex:Thing"]
    roles = {name: a.role for name, a in thing.attributes.items()}
    assert roles == {
        "name": "label",
        "synonyms": "alt_label",
        "misspellings": "hidden_label",
        "code": None,
    }
    assert thing.attributes["name"].declared_in == "ex:Base"
    compiled = roles_model().to_json()["classes"]
    assert compiled["ex:Thing"]["attributes"]["code"]["role"] is None  # B7.7: always written


def test_unknown_role_is_an_invalid_value() -> None:
    text = _ROLES.replace("role: hidden_label", "role: nickname")
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.structural.invalid_value", "classes.Thing.attributes.misspellings.role")
    ]


@pytest.mark.parametrize("type_ref", ["text", "integer", "iri", "json", "external_ref"])
def test_roles_need_a_string_type(type_ref: str) -> None:
    text = _ROLES.replace(
        "type: string\n        many: true\n        role: alt_label",
        f"type: {type_ref}\n        many: true\n        role: alt_label",
    )
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.semantic.role_not_applicable", "classes.Thing.attributes.synonyms.role")
    ]


def test_document_without_role_reads_as_null() -> None:
    # Artifacts released before label roles have no "role" key; they still load.
    document = roles_model().to_json()
    for cls in document["classes"].values():  # type: ignore[union-attr]
        for attribute in cls["attributes"].values():  # type: ignore[index]
            del attribute["role"]  # type: ignore[arg-type]
    old = model_from_json(document)
    assert all(a.role is None for c in old.classes.values() for a in c.attributes.values())
    assert load_compiled(pretty_bytes(document), expected_hash=hash_value(document)) == old


def test_unknown_role_in_a_document_is_a_shape_error() -> None:
    document = roles_model().to_json()
    document["classes"]["ex:Thing"]["attributes"]["code"]["role"] = "nickname"  # type: ignore[call-overload,index]
    with pytest.raises(JsonShapeError, match="unknown label role"):
        model_from_json(document)


def test_second_label_in_one_class_is_reported_at_the_later_one() -> None:
    # Base gets a second label; Thing inherits both and is not reported again.
    main = "        role: label\n"
    text = _ROLES.replace(
        main, main + "      title:\n        summary: Title.\n        type: string\n" + main, 1
    )
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.semantic.duplicate_label", "classes.Base.attributes.title.role")
    ]


def test_label_attributes_are_ordered_by_role_then_name() -> None:
    model = roles_model()
    assert model.label_attributes("ex:Thing") == (
        ("name", "label"),
        ("synonyms", "alt_label"),
        ("misspellings", "hidden_label"),
    )
    assert model.label_attributes("ex:Base") == (("name", "label"),)
    assert compiled_model("notes_and_sources").label_attributes("nas:Book") == (("title", "label"),)
