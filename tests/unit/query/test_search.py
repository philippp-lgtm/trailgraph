"""Text search planning, matching, ranking and excerpts (F51)."""

from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.query_ast import Cmp, Or
from graph_cli.kernel.records import Revision
from graph_cli.query.search import (
    EXCERPT_CHARS,
    SearchPlan,
    SearchSpec,
    excerpt,
    main_label,
    match_revision,
    plan_search,
    rank_key,
)

NAS = compiled_model("notes_and_sources")
INV = compiled_model("inventory", "1.0.0")


def plan(text: str, **changes: Any) -> SearchPlan:
    return plan_search(NAS, SearchSpec(text=text, **changes))


def topic(name: str, label: str, **attrs: Any) -> Revision:
    return revision(name, "nas:Topic", {"label": label, **attrs})


def note(name: str, title: str, body: str) -> Revision:
    return revision(name, "nas:Note", {"title": title, "body": body})


def ranked(p: SearchPlan, *revisions: Revision) -> list[tuple[str, str, str, int]]:
    found = [(r, m) for r in revisions if (m := match_revision(p, r)) is not None]
    found.sort(key=lambda pair: rank_key(p, *pair))
    return [
        (str(main_label(p, r)), m.best.field.attr, m.best.kind, round(m.best.score * 100))
        for r, m in found
    ]


def test_plan_reads_string_and_text_fields_of_every_class() -> None:
    p = plan("physics")
    assert p.query.types == (
        "nas:Book",
        "nas:Note",
        "nas:Source",
        "nas:Topic",
        "nas:WebPage",
        "provenance:Claim",
    )
    assert p.query.where == Or(
        tuple(
            Cmp(a, "exists", True)
            for a in (
                "body",
                "description",
                "isbn",
                "label",
                "misspellings",
                "publisher",
                "statement",
                "synonyms",
                "title",
            )
        )
    )
    assert p.query.record_status == {"active"}
    assert p.labels["nas:Topic"] == "label" and "provenance:Claim" not in p.labels
    assert plan("x1", include_inactive=True).query.record_status == {
        "active",
        "superseded",
        "retracted",
    }


def test_plan_keys() -> None:
    p = plan("  Light- and  Refraction ")
    assert p.key == "light and refraction"
    assert p.words == ("light", "and", "refraction")


def test_enum_values_are_not_searched() -> None:
    # inventory 1.0.0: Item.condition is an enum; only name and serial_number are text.
    p = plan_search(INV, SearchSpec(text="new", types=("inv:Item",)))
    assert [f.attr for f in p.fields["inv:Item"]] == ["name", "serial_number"]


@pytest.mark.parametrize("text", ["", " - ", "x", "é"])
def test_short_text_is_refused(text: str) -> None:
    with pytest.raises(GraphCliError) as caught:
        plan(text)
    assert caught.value.code == "usage.invalid_argument"
    assert caught.value.details["argument"] == "text"


def test_type_and_field_filters() -> None:
    p = plan("x1", types=("nas:Source",), fields=("title",))
    assert p.query.types == ("nas:Book", "nas:Source", "nas:WebPage")
    assert all([f.attr for f in found] == ["title"] for found in p.fields.values())
    assert plan("x1", types=("Source",), fields=("title",)).query.types == p.query.types
    with pytest.raises(GraphCliError) as caught:
        plan("x1", fields=("titel",))
    assert caught.value.code == "term.unknown"
    assert caught.value.details["did_you_mean"] == ["title"]
    with pytest.raises(GraphCliError) as caught:
        plan("x1", types=("nas:Book",), fields=("body",))  # a field of another class
    assert caught.value.code == "term.unknown"


def test_types_without_text_fields_are_refused() -> None:
    with pytest.raises(GraphCliError) as caught:
        plan("x1", types=("nas:Topic", "provenance:Claim"), fields=("label",))
    assert caught.value.code == "term.no_text_fields"
    assert caught.value.details == {"types": ["provenance:Claim"]}


def test_kinds() -> None:
    p = plan("Lightrefraction")
    assert ranked(
        p,
        topic("a", "Light and refraction"),
        topic("b", "Sunlightrefraction"),
        topic("c", "lightrefraction"),
        topic("d", "Optics"),
    ) == [
        ("lightrefraction", "label", "exact", 100),
        ("Sunlightrefraction", "label", "phrase", 100),
        ("Light and refraction", "label", "similar", 88),
    ]


def test_words_in_any_order() -> None:
    p = plan("refraction light")
    assert ranked(p, topic("a", "Light and refraction"))[0][2] == "words"
    # A missing word is no match, not even a similar one (each word must pass).
    assert ranked(plan("refraction prism"), topic("a", "Refraction")) == []
    assert ranked(plan("refracton light"), topic("a", "Light and refraction"))[0][2] == "similar"


def test_typos_and_accents() -> None:
    assert ranked(plan("Lightrefration"), topic("a", "Light and refraction"))[0][2] == "similar"
    assert ranked(plan("cafe"), topic("a", "Café"))[0][2] == "exact"
    assert ranked(plan("Lightrefraction"), topic("a", "Total internal refraction")) == []


def test_similar_never_on_long_text() -> None:
    p = plan("Lightrefraction")
    long_text = note("n", "Unrelated", "The light and refraction chapter ...")
    assert ranked(p, long_text) == []  # 'body' is text: no similar match
    assert ranked(plan("refraction"), long_text)[0][1:3] == ("body", "phrase")


def test_ranking_prefers_names_then_kind_then_place() -> None:
    p = plan("optics")
    found = ranked(
        p,
        topic("desc", "Light", description="All about optics."),
        topic("hidden", "Seeing", misspellings=["optics"]),
        topic("alt", "Lenses", synonyms=["optics"]),
        topic("main", "optics"),
        topic("prefix", "optics of lenses"),
    )
    assert found == [
        ("optics", "label", "exact", 100),
        ("Lenses", "synonyms", "exact", 100),
        ("Seeing", "misspellings", "exact", 100),
        ("optics of lenses", "label", "phrase", 100),
        ("Light", "description", "phrase", 100),
    ]
    # A close name beats a phrase in a long text.
    p = plan("Lightrefraction")
    assert [
        r[1:3]
        for r in ranked(
            p,
            topic("text", "Other", description="All about lightrefraction ..."),
            topic("name", "Light and refraction"),
        )
    ] == [("label", "similar"), ("description", "phrase")]


def test_ties_break_on_length_label_and_id() -> None:
    p = plan("optic")
    assert [r[0] for r in ranked(p, topic("b", "optic b"), topic("a", "optic a"))] == [
        "optic a",
        "optic b",
    ]


def test_entity_match_lists_every_matching_field_best_first() -> None:
    p = plan("optics")
    found = match_revision(
        p, topic("t", "Optics", synonyms=["Optics of light"], description="optics")
    )
    assert found is not None
    assert found.fields == ("label", "synonyms", "description")  # names first
    assert found.to_json(p) == {
        "attr": "label",
        "role": "label",
        "kind": "exact",
        "score": 100,
        "excerpt": "Optics",
    }


def test_hidden_labels_are_never_shown() -> None:
    p = plan("optiks")
    found = match_revision(p, topic("t", "Optics", misspellings=["optiks"]))
    assert found is not None and found.to_json(p)["excerpt"] is None
    assert found.to_json(p)["role"] == "hidden_label"


def test_main_label_is_shown_when_fields_skip_it() -> None:
    p = plan("lens", fields=("synonyms",))
    t = topic("t", "Optics", synonyms=["lens"])
    assert main_label(p, t) == "Optics"


def test_excerpt_keeps_short_values_on_one_line() -> None:
    p = plan("optics")
    assert excerpt("two\n lines  of optics", p, "phrase") == "two lines of optics"


def test_excerpt_cuts_long_values_around_the_hit() -> None:
    before = " ".join(f"word{i}" for i in range(60))
    after = " ".join(f"tail{i}" for i in range(60))
    value = f"{before} Light-Refraction {after}"
    cut = excerpt(value, plan("light refraction"), "phrase")
    assert cut.startswith("…") and cut.endswith("…")
    assert "Light-Refraction" in cut
    assert len(cut) <= EXCERPT_CHARS + 2
    start = excerpt(f"Light {after}", plan("light"), "phrase")
    assert start.startswith("Light") and start.endswith("…")
