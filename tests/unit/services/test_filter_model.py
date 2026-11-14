"""``filter_model``: the compiled model as a limited reader sees it (type read limits plan V5).

Each rule of the filter is shown on the toy ontologies: ``notes_and_sources`` has a concrete
parent with a subclass (``files:File``, ``nas:Scan``), an abstract type with two subtypes
(``nas:Source``), relations to both and rules, an enum and a lifecycle; ``company`` has guards
that name relations.
"""

import json

import pytest
from support.fixtures import compiled_model

from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.term_model import concrete_types
from graph_cli.services.visibility import filter_model

NAS = compiled_model("notes_and_sources", "1.1.0")
COMPANY = compiled_model("company")
ALL_NAS = concrete_types(NAS.classes)
ALL_COMPANY = concrete_types(COMPANY.classes)
DOC_KEYS = {"summary", "description", "annotations"}


def seeing(model: CompiledModel, *hidden: str) -> CompiledModel:
    return filter_model(model, concrete_types(model.classes) - set(hidden))


def test_everything_visible_keeps_the_model_except_unused_terms() -> None:
    same = filter_model(NAS, ALL_NAS)
    assert same.classes == NAS.classes
    assert same.relations == NAS.relations
    assert same.rules == NAS.rules
    assert same.modules == NAS.modules
    assert same.prefixes == NAS.prefixes


def test_a_hidden_concrete_class_is_gone() -> None:
    model = seeing(NAS, "nas:Topic")
    assert "nas:Topic" not in model.classes
    assert "nas:Note" in model.classes


def test_a_hidden_concrete_parent_with_a_visible_subclass_is_gone_and_its_attributes_move() -> None:
    model = seeing(NAS, "files:File")
    assert "files:File" not in model.classes
    scan = model.classes["nas:Scan"]
    assert (scan.extends, scan.ancestors) == (None, ())
    original = NAS.classes["nas:Scan"].attributes
    assert list(scan.attributes) == list(original)  # the effective attributes stay
    assert {a.declared_in for n, a in scan.attributes.items() if n in ("title", "content")} == {
        "nas:Scan"
    }
    assert scan.attributes["scanned_on"].declared_in == "nas:Scan"
    assert "files:File" not in json.dumps(strip_docs(model.to_json()))


def test_an_attribute_moves_to_the_top_most_kept_class_that_is_a_subclass_of_its_declarer() -> None:
    # nas:Book extends nas:Source (abstract, kept while a subtype is visible): it declares
    # title, retrieved_at, so they stay there; Book's own attributes stay on Book
    model = seeing(NAS, "nas:WebPage")
    book = model.classes["nas:Book"]
    assert book.ancestors == ("nas:Source",)
    assert book.attributes["title"].declared_in == "nas:Source"
    assert book.attributes["isbn"].declared_in == "nas:Book"


def test_an_abstract_class_with_only_hidden_subtypes_is_gone() -> None:
    model = seeing(NAS, "nas:Book", "nas:WebPage")
    assert "nas:Source" not in model.classes
    # the subclasses are gone too, so nothing refers to it
    assert "nas:cites" not in model.relations
    assert "nas:source_cited" not in {r.id for r in model.rules}


def test_an_abstract_class_with_a_visible_subtype_stays() -> None:
    model = seeing(NAS, "nas:Book")
    assert "nas:Source" in model.classes
    assert model.relations["nas:cites"].range == ("nas:Source",)


def test_a_relation_end_of_a_hidden_class_names_its_maximal_kept_subclasses() -> None:
    model = seeing(NAS, "files:File")
    assert NAS.relations["files:from_file"].range == ("files:File",)
    assert model.relations["files:from_file"].range == ("nas:Scan",)


def test_a_relation_whose_named_end_is_left_empty_is_gone() -> None:
    model = seeing(NAS, "nas:Topic")
    assert "nas:about" not in model.relations  # range is nas:Topic
    assert "nas:broader" not in model.relations
    assert "nas:cites" in model.relations
    domain_hidden = seeing(NAS, "nas:Note")
    assert "nas:cites" not in domain_hidden.relations  # domain is nas:Note


def test_an_open_end_stays_open() -> None:
    model = seeing(NAS, "provenance:Claim", "nas:Topic")
    assert model.relations["core:supersedes"].domain == ()
    assert model.relations["core:supersedes"].range == ()
    assert "provenance:supported_by" not in model.relations  # its domain is the hidden claim


def test_rules_of_a_hidden_target_or_naming_a_removed_term_are_dropped() -> None:
    ids = {r.id for r in seeing(NAS, "nas:Topic").rules}
    assert "nas.topic_not_orphan" not in ids  # target is hidden
    assert ids == {"nas.source_cited", "nas.summary_cites_source"}
    # a kept target whose expression names a removed relation (nas:cites goes with Source)
    ids = {r.id for r in seeing(NAS, "nas:Book", "nas:WebPage").rules}
    assert ids == {"nas.topic_not_orphan"}


def test_guards_that_name_a_removed_relation_are_dropped_from_the_transition() -> None:
    def guards(model: CompiledModel) -> list[str]:
        lifecycle = model.lifecycles["org:PositionLifecycle"]
        return [g.id for t in lifecycle.transitions for g in t.guards]

    before = guards(COMPANY)
    assert "org.not_held" in before and "org.has_unit" in before
    after = seeing(COMPANY, "ppl:Person")  # org:holds goes with its domain
    assert "org:holds" not in after.relations
    kept = guards(after)
    assert "org.not_held" not in kept
    assert "org.has_unit" in kept
    transitions = after.lifecycles["org:PositionLifecycle"].transitions
    assert len(transitions) == len(COMPANY.lifecycles["org:PositionLifecycle"].transitions)


def test_unused_enums_lifecycles_and_datatypes_are_dropped() -> None:
    model = seeing(NAS, "nas:Note")
    assert "nas:NoteKind" not in model.enums
    assert "nas:NoteLifecycle" not in model.lifecycles
    assert "provenance:Confidence" in model.enums  # a claim still uses it
    assert "nas:Isbn" in model.datatypes
    without_books = seeing(NAS, "nas:Book")
    assert "nas:Isbn" not in without_books.datatypes
    assert "nas:NoteKind" in without_books.enums


def test_a_module_without_a_term_left_goes_with_its_prefix() -> None:
    model = seeing(NAS, "provenance:Claim")
    assert "provenance" not in {m.id for m in model.modules}
    assert "provenance" not in model.prefixes
    assert {"core", "files", "nas"} <= {m.prefix for m in model.modules}
    everything_but_core = filter_model(NAS, frozenset())
    assert {m.prefix for m in everything_but_core.modules} == {"core"}
    assert everything_but_core.classes == {}


def test_a_main_module_without_terms_stays_and_an_area_that_lost_them_goes() -> None:
    """``company``: the main module ``co`` only imports the areas. Nothing is hidden from it, so
    it stays (the functions and actions that name it keep their module); an area whose terms
    are all hidden still goes with its prefix."""
    assert not any(c.startswith("co:") for c in COMPANY.classes)
    org = {c for c in ALL_COMPANY if c.startswith("org:")}
    assert org
    view = filter_model(COMPANY, ALL_COMPANY - org)
    prefixes = {m.prefix for m in view.modules}
    assert "co" in prefixes and "ppl" in prefixes
    assert "org" not in prefixes and "org" not in view.prefixes
    assert "co" in view.prefixes
    assert {m.id for m in filter_model(COMPANY, ALL_COMPANY).modules} == {
        m.id for m in COMPANY.modules
    }


def test_bundle_identity_documentation_and_mappings_stay() -> None:
    model = seeing(NAS, "nas:Topic")
    assert (model.bundle_id, model.bundle_version) == (NAS.bundle_id, NAS.bundle_version)
    assert model.classes["nas:Note"].doc == NAS.classes["nas:Note"].doc
    assert model.classes["nas:Note"].mappings == NAS.classes["nas:Note"].mappings


def test_the_result_is_a_working_compiled_model() -> None:
    model = seeing(NAS, "nas:Topic", "files:File")
    assert model.is_a("nas:Book", "nas:Source")
    assert model.subtypes("nas:Source") == ("nas:Book", "nas:Source", "nas:WebPage")
    assert model.label_attributes("nas:Scan")
    assert model.to_json()["classes"]


def strip_docs(value: object) -> object:
    if isinstance(value, dict):
        return {k: strip_docs(v) for k, v in value.items() if k not in DOC_KEYS}
    if isinstance(value, list):
        return [strip_docs(v) for v in value]
    return value


def hidden_sets(model: CompiledModel) -> list[tuple[str, ...]]:
    names = sorted(concrete_types(model.classes))
    singles = [(n,) for n in names]
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
    return [*singles, *pairs, tuple(names)]


@pytest.mark.parametrize("model", [NAS, COMPANY], ids=["notes_and_sources", "company"])
def test_no_removed_term_survives_in_the_filtered_document(model: CompiledModel) -> None:
    for hidden in hidden_sets(model):
        filtered = seeing(model, *hidden)
        removed = model.term_curies() - filtered.term_curies()
        kept = filtered.term_curies()
        assert set(hidden) <= removed
        text = json.dumps(strip_docs(filtered.to_json()))
        for curie in removed:
            start = text.find(curie)
            while start != -1:
                end = start + len(curie)
                # allowed only as part of a longer, kept CURIE (e.g. nas:Note in nas:NoteKind)
                assert any(
                    k != curie
                    and curie in k
                    and text.find(k, max(0, end - len(k)), start + len(k)) != -1
                    for k in kept
                ), (hidden, curie, text[max(0, start - 30) : end + 30])
                start = text.find(curie, end)


@pytest.mark.parametrize("model", [NAS, COMPANY], ids=["notes_and_sources", "company"])
def test_the_filtered_model_is_internally_consistent(model: CompiledModel) -> None:
    for hidden in hidden_sets(model):
        filtered = seeing(model, *hidden)
        for name, cls in filtered.classes.items():
            assert all(a in filtered.classes for a in cls.ancestors), (hidden, name)
            assert cls.extends == (cls.ancestors[0] if cls.ancestors else None)
            for attribute in cls.attributes.values():
                assert attribute.declared_in in filtered.classes, (hidden, name)
                assert model.is_a(name, attribute.declared_in)
        for name, relation in filtered.relations.items():
            for end in (*relation.domain, *relation.range):
                assert end in filtered.classes, (hidden, name, end)
        for rule in filtered.rules:
            assert rule.target in filtered.classes
