"""Label lookup planning, matching and ranking (Q18 stage 1, D8)."""

from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.query_ast import Cmp, Or
from graph_cli.kernel.records import Revision
from graph_cli.query.lookup import (
    LookupPlan,
    LookupSpec,
    main_label,
    match_revision,
    plan_lookup,
    rank_key,
)

INV = compiled_model("inventory", "1.0.0")
INV_LABELS = compiled_model("inventory", "1.1.0")
NAS = compiled_model("notes_and_sources")


def plan(text: str, **changes: Any) -> LookupPlan:
    return plan_lookup(NAS, LookupSpec(text=text, **changes))


def topic(name: str, label: str, **attrs: Any) -> Revision:
    return revision(name, "nas:Topic", {"label": label, **attrs})


def ranked(p: LookupPlan, *revisions: Revision) -> list[tuple[str, str, str]]:
    found = [(r, m) for r in revisions if (m := match_revision(p, r)) is not None]
    found.sort(key=lambda pair: rank_key(p, *pair))
    return [(str(r.attrs.get("label") or r.attrs.get("title")), m.role, m.kind) for r, m in found]


def test_plan_selects_label_bearing_classes() -> None:
    p = plan("physics")
    assert p.query.types == ("nas:Book", "nas:Note", "nas:Source", "nas:Topic", "nas:WebPage")
    assert "provenance:Claim" not in p.labels  # no label roles there
    assert p.query.where == Or(
        tuple(Cmp(a, "exists", True) for a in ("label", "misspellings", "synonyms", "title"))
    )
    assert p.query.record_status == {"active"}
    assert p.key == "physics" and p.folded_key is None
    assert plan("x", include_inactive=True).query.record_status == {
        "active",
        "superseded",
        "retracted",
    }


def test_type_family_includes_inherited_roles() -> None:
    p = plan("x", types=("nas:Source",))
    assert p.query.types == ("nas:Book", "nas:Source", "nas:WebPage")
    assert p.labels["nas:Book"] == (("title", "label"),)


def test_types_without_labels_are_refused() -> None:
    with pytest.raises(GraphCliError) as caught:
        plan("x", types=("provenance:Claim",))
    assert caught.value.code == "term.no_labels"
    assert caught.value.details == {"types": ["provenance:Claim"]}
    # A named family with labels next to one without: still refused.
    with pytest.raises(GraphCliError) as caught:
        plan("x", types=("nas:Topic", "provenance:Claim"))
    assert caught.value.details == {"types": ["provenance:Claim"]}
    with pytest.raises(GraphCliError) as caught:
        plan_lookup(INV, LookupSpec(text="x"))  # no label roles before 1.1.0
    assert caught.value.code == "term.no_labels"
    assert caught.value.details == {"types": []}
    assert plan_lookup(INV_LABELS, LookupSpec(text="x")).labels == {
        "inv:Item": (("name", "label"),)
    }


@pytest.mark.parametrize(
    ("changes", "code", "argument"),
    [
        ({"text": " \t "}, "usage.invalid_argument", "text"),
        ({"text": "x", "match": "contains"}, "usage.invalid_argument", "--match"),
        ({"text": "x", "limit": 0}, "usage.invalid_argument", "--limit"),
        ({"text": "x", "offset": -1}, "usage.invalid_argument", "--offset"),
        ({"text": "x", "types": ("nas:Topik",)}, "term.unknown", None),
    ],
)
def test_bad_arguments(changes: dict[str, Any], code: str, argument: str | None) -> None:
    with pytest.raises(GraphCliError) as caught:
        plan_lookup(NAS, LookupSpec(**changes))
    assert caught.value.code == code
    if argument is not None:
        assert caught.value.details["argument"] == argument


def test_matches_any_role_after_normalization() -> None:
    p = plan("CHEM")
    match = match_revision(p, topic("c", "Chemistry", synonyms=["Chem"]))
    assert match is not None
    assert match.to_json() == {
        "attr": "synonyms",
        "role": "alt_label",
        "kind": "exact",
        "value": "Chem",
    }
    assert match_revision(p, topic("p", "Physics")) is None
    assert match_revision(p, topic("q", "Chemistry")) is None  # exact is the default


def test_hidden_label_value_is_never_shown() -> None:
    match = match_revision(plan("chemestry"), topic("c", "Chemistry", misspellings=["Chemestry"]))
    assert match is not None
    assert match.to_json() == {
        "attr": "misspellings",
        "role": "hidden_label",
        "kind": "exact",
        "value": None,
    }


def test_best_match_per_entity() -> None:
    # The same text as main label and synonym: the main label wins.
    match = match_revision(plan("chem"), topic("c", "Chem", synonyms=["chem"]))
    assert match is not None and match.role == "label"


def test_accent_folding_ranks_after_unfolded_matches() -> None:
    p = plan("cafe", ignore_accents=True)
    assert p.folded_key == "cafe"
    exact = topic("a", "Cafe")
    folded = topic("b", "Café")
    assert ranked(p, folded, exact) == [("Cafe", "label", "exact"), ("Café", "label", "exact")]
    assert match_revision(plan("cafe"), folded) is None


def test_rank_order_and_tie_breaks() -> None:
    p = plan("qu", match="prefix")
    revisions = [
        topic("hidden", "Zeta", misspellings=["Qu"]),
        topic("alt", "Alpha", synonyms=["Qu"]),
        topic("long", "Quantum chemistry"),
        topic("short", "Quark"),
        topic("main", "Qu"),
        topic("same_b", "Quiz"),
        topic("same_a", "Quip"),
    ]
    assert ranked(p, *revisions) == [
        ("Qu", "label", "exact"),
        ("Alpha", "alt_label", "exact"),
        ("Zeta", "hidden_label", "exact"),
        ("Quip", "label", "prefix"),  # same length as Quiz: main label decides
        ("Quiz", "label", "prefix"),
        ("Quark", "label", "prefix"),
        ("Quantum chemistry", "label", "prefix"),
    ]


def test_missing_main_label_sorts_last_among_equals() -> None:
    p = plan("qc")
    with_label = topic("a", "Quantum chemistry", synonyms=["QC"])
    without = revision("b", "nas:Topic", {"synonyms": ["QC"]})
    assert main_label(p, without) is None
    keys = [rank_key(p, r, m) for r in (without, with_label) if (m := match_revision(p, r))]
    assert sorted(keys)[0][-2] == "quantum chemistry"
