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

from graph_cli.kernel.canonical import canonical_bytes, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.bundle import LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import load_compiled, model_from_json

ALL_VERSIONS = [(name, v) for name in TOY_ONTOLOGIES for v in ontology_versions(name)]


@pytest.mark.parametrize(("name", "version"), ALL_VERSIONS)
def test_positive_control_has_zero_findings(name: str, version: str) -> None:
    outcome = compile_fixture(name, version)
    assert [f.to_json() for f in outcome.findings] == []
    assert outcome.model is not None


@pytest.mark.parametrize("module", ["core", "provenance"])
def test_builtin_modules_compile_cleanly(module: str) -> None:
    outcome = compile_bundle(module, LayeredSource())
    assert outcome.findings == ()
    assert outcome.model is not None


@pytest.mark.parametrize(("name", "version"), ALL_VERSIONS)
def test_artifact_hash_is_stable(name: str, version: str) -> None:
    first, second = compiled_model(name, version), compiled_model(name, version)
    assert first.artifact_hash == second.artifact_hash
    assert canonical_bytes(first.to_json()) == canonical_bytes(second.to_json())


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_compiled_golden(name: str) -> None:
    version = ontology_versions(name)[0]
    data = pretty_bytes(compiled_model(name, version).to_json())
    assert_golden(GOLDENS / "compiled" / f"{name}_{version}.json", data)


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_typed_view_round_trip(name: str) -> None:
    model = compiled_model(name)
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
