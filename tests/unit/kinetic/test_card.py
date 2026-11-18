"""The generated entity card on the toy notes ontology (T17): fields, groups, files, affordances."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from support.cards import with_attribute, with_sections
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kinetic.card import (
    BYTE_LIMIT,
    FieldsSpec,
    ViewSpec,
    affordance_summary,
    dumps,
    generated_spec,
    render,
)
from graph_cli.ontology_layer.affordances import relations, transitions
from graph_cli.ontology_layer.model import Bound, CompiledModel

MODEL = compiled_model("notes_and_sources", "1.2.0")

type Card = dict[str, Any]


def note(
    name: str,
    title: str | None = None,
    edges: Sequence[EdgeRecord] = (),
    *,
    state: str = "draft",
    alias: str | None = None,
    attrs: Mapping[str, JsonValue] | None = None,
    status: Any = "active",
) -> Revision:
    base: dict[str, JsonValue] = {"body": "Text.", "kind": "observation", "status": state}
    if title is not None:
        base["title"] = title
    base.update(attrs or {})
    return revision(name, "nas:Note", base, edges, alias=alias, status=status)


def topic(name: str, label: str | None = None, **attrs: JsonValue) -> Revision:
    base: dict[str, JsonValue] = dict(attrs)
    if label is not None:
        base["label"] = label
    return revision(name, "nas:Topic", base, alias=f"TOPIC-{name}")


def scan(name: str, title: str, *edges: EdgeRecord, alias: str | None = None) -> Revision:
    attrs: dict[str, JsonValue] = {
        "title": title,
        "content": f"sha256:{name}",
        "media_type": "application/pdf",
        "file_name": f"{name}.pdf",
    }
    return revision(name, "nas:Scan", attrs, edges, alias=alias)


def card(
    entity: Revision,
    *others: Revision,
    model: CompiledModel = MODEL,
    spec: ViewSpec | None = None,
    **kwargs: Any,
) -> Card:
    chosen = spec or generated_spec(model, str(entity.type))
    result = render(chosen, entity, DictSnapshot(entity, *others), model, **kwargs)
    return dict(result.to_json())


def section(data: Card, kind: str, variant: str | None = None) -> Card:
    found = [s for s in data["sections"] if s["kind"] == kind and s.get("variant") == variant]
    assert len(found) == 1, [s["kind"] for s in data["sections"]]
    return dict(found[0])


# ---- the spec and the fields section ---------------------------------------------------------


def test_the_generated_spec_lists_label_state_required_and_unique_in_order() -> None:
    spec = generated_spec(MODEL, "nas:Note")
    fields = next(s for s in spec.sections if isinstance(s, FieldsSpec))
    assert [(c.name, c.role) for c in fields.choices] == [
        ("title", "label"),
        ("status", "state"),
        ("body", None),
        ("kind", None),
    ]
    assert (spec.name, spec.origin, spec.byte_limit) == ("card", "generated", BYTE_LIMIT)


def test_fields_show_label_state_and_required_values_and_name_the_rest() -> None:
    entity = note("n1", "Orbitals", attrs={"retrieved": 1}, alias="NOTE-0001")
    data = card(entity)
    assert data["view"] == {"name": "card", "origin": "generated"}
    assert data["entity"] == {
        "id": str(entity.id),
        "alias": "NOTE-0001",
        "type": "nas:Note",
        "rev": 1,
        "record_status": "active",
        "label": "Orbitals",
    }
    fields = section(data, "fields")
    assert fields["items"] == [
        {"name": "title", "role": "label", "value": "Orbitals"},
        {"name": "status", "role": "state", "value": "draft"},
        {"name": "body", "value": "Text."},
        {"name": "kind", "value": "observation"},
    ]
    assert "other" not in fields  # nothing else has a value in the model


def test_alt_labels_come_after_the_label_and_hidden_labels_are_never_named() -> None:
    entity = topic(
        "t1",
        "Physics",
        synonyms=["Natural philosophy", "Physical science", "Mechanics", "Optics"],
        misspellings=["Fisics"],
        description="What it covers.",
    )
    fields = section(card(entity), "fields")
    assert fields["items"] == [
        {"name": "label", "role": "label", "value": "Physics"},
        {
            "name": "synonyms",
            "role": "alt_label",
            "value": ["Natural philosophy", "Physical science", "Mechanics"],
            "more": 1,
        },
    ]
    assert fields["other"] == ["description"]  # misspellings: never named


def test_a_blob_attribute_is_never_a_key_field_and_a_file_shows_its_media_type() -> None:
    fields = section(card(scan("s1", "Notebook")), "fields")
    assert [i["name"] for i in fields["items"]] == ["title", "media_type"]
    assert fields["other"] == ["content", "file_name"]


def test_the_label_is_shown_in_the_graph_language_else_the_smallest_tag() -> None:
    heading: list[JsonValue] = [
        {"value": "Zucker", "lang": "de"},
        {"value": "Sugar", "lang": "en"},
    ]
    entity = revision(
        "g1",
        "nas:GlossaryEntry",
        {"heading": heading, "explanation": [{"value": "Süß.", "lang": "de"}]},
    )
    assert card(entity, language="en")["entity"]["label"] == "Sugar"
    assert card(entity, language="de")["entity"]["label"] == "Zucker"
    assert card(entity)["entity"]["label"] == "Zucker"  # no language: the smallest tag
    fields = section(card(entity, language="en"), "fields")
    assert fields["items"][0] == {"name": "heading", "role": "label", "value": "Sugar"}
    assert fields["other"] == ["explanation"]


def test_a_missing_label_leaves_the_key_out() -> None:
    entity = note("n1", None)
    data = card(entity)
    assert "label" not in data["entity"]
    assert [i["name"] for i in section(data, "fields")["items"]] == ["status", "body", "kind"]


def test_a_long_string_is_cut_with_its_length_and_a_long_list_with_a_count() -> None:
    model = with_attribute(
        MODEL, "nas:Note", "tags", "title", required=True, many=True, role=None, unique=False
    )
    long = "x" * 250
    entity = note("n1", "T", attrs={"body": long, "tags": [f"tag{i}" for i in range(9)]})
    items = {i["name"]: i for i in section(card(entity, model=model), "fields")["items"]}
    assert items["body"] == {"name": "body", "value": "x" * 100 + "…", "chars": 250}
    assert items["tags"]["value"] == ["tag0", "tag1", "tag2", "tag3", "tag4"]
    assert items["tags"]["more"] == 4
    wide = note("n2", "y" * 300)
    assert card(wide)["entity"]["label"] == "y" * 100 + "…"


def test_at_most_eight_fields_and_the_rest_is_named_with_a_count() -> None:
    model = MODEL
    for n in range(10):
        model = with_attribute(model, "nas:Note", f"a{n:02d}", "body", required=n < 6, unique=False)
    values: dict[str, JsonValue] = {f"a{n:02d}": f"v{n}" for n in range(10)}
    entity = note("n1", "Title", attrs=values)
    fields = section(card(entity, model=model), "fields")
    assert len(fields["items"]) == 8
    shown = [i["name"] for i in fields["items"]]
    assert shown == ["title", "status", "a00", "a01", "a02", "a03", "a04", "a05"]
    # body and kind are required too, so they are named with the optional ones, none dropped
    assert fields["other"] == ["a06", "a07", "a08", "a09", "body", "kind"]


def test_other_names_at_most_twelve_attributes_and_counts_the_rest() -> None:
    model = MODEL
    for n in range(15):
        model = with_attribute(model, "nas:Note", f"z{n:02d}", "body", required=False, unique=False)
    values: dict[str, JsonValue] = {f"z{n:02d}": "v" for n in range(15)}
    fields = section(card(note("n1", "T", attrs=values), model=model), "fields")
    assert len(fields["other"]) == 12
    assert fields["other_more"] == 3


def test_an_empty_value_is_not_shown_or_named() -> None:
    entity = topic("t1", "Physics", description="", synonyms=[])
    fields = section(card(entity), "fields")
    assert [i["name"] for i in fields["items"]] == ["label"]
    assert "other" not in fields


# ---- relation groups -------------------------------------------------------------------------


def test_groups_are_ordered_out_before_in_functional_and_required_first() -> None:
    about = MODEL.relations["nas:about"]
    cites = MODEL.relations["nas:cites"]
    relations_ = {
        **MODEL.relations,
        "nas:about": replace(about, out=Bound(1, None)),  # required, not functional
        "nas:cites": replace(cites, out=Bound(0, 1)),  # functional, optional
        "nas:asserts": replace(MODEL.relations["nas:asserts"], out=Bound(1, 1)),
    }
    model = replace(MODEL, relations=relations_)
    entity = note(
        "n1",
        "T",
        [edge("nas:about", "t1"), edge("nas:cites", "w1"), edge("nas:asserts", "c1")],
    )
    web = revision("w1", "nas:WebPage", {"title": "Page", "url": "https://example.org/p"})
    claim = revision("c1", "provenance:Claim", {"statement": "A claim."})
    groups = section(card(entity, topic("t1", "Physics"), web, claim, model=model), "relations")[
        "groups"
    ]
    assert [(g["relation"], g["direction"]) for g in groups] == [
        ("nas:asserts", "out"),  # functional and required
        ("nas:cites", "out"),  # functional
        ("nas:about", "out"),  # required
    ]


def test_in_groups_follow_the_out_groups_and_count_active_sources() -> None:
    physics = topic("t1", "Physics")
    about = [edge("nas:about", "t1")]
    notes = [
        note("n1", "Beta", about, alias="NOTE-0002", state="published"),
        note("n2", "alpha", about, alias="NOTE-0001"),
        note("n3", "Gone", about, status="retracted"),
        note("n4", None, about),
        note("n5", "Gamma", about),
        note("n6", "Delta", about),
    ]
    data = card(physics, *notes)
    groups = section(data, "relations")["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert (group["relation"], group["direction"], group["count"]) == ("nas:about", "in", 5)
    # first by name (normalized), a neighbour without a label last
    assert [i.get("label") for i in group["items"]] == ["alpha", "Beta", "Delta"]
    assert group["items"][0]["ref"] == "NOTE-0001" and group["items"][1]["ref"] == "NOTE-0002"
    assert group["items"][0]["state"] == "draft" and group["items"][1]["state"] == "published"
    assert group["more"] == 2 and "type" not in group["items"][0]
    assert group["states"] == {"draft": 4, "published": 1}  # the retracted source is not counted


def test_states_are_left_out_when_every_neighbour_is_shown_with_its_state() -> None:
    notes = [note(f"n{i}", f"Note {i}", [edge("nas:about", "t1")]) for i in range(3)]
    group = section(card(topic("t1", "Physics"), *notes), "relations")["groups"][0]
    assert group["count"] == 3 and "states" not in group and "more" not in group
    assert [i["state"] for i in group["items"]] == ["draft"] * 3


def test_items_are_the_first_three_by_name_and_the_rest_is_counted() -> None:
    entity = note("n1", "T", [edge("nas:about", f"t{i}") for i in range(7)])
    topics = [topic(f"t{i}", f"Topic {chr(ord('g') - i)}") for i in range(7)]
    group = section(card(entity, *topics), "relations")["groups"][0]
    assert group["count"] == 7 and group["more"] == 4
    assert [i["label"] for i in group["items"]] == ["Topic a", "Topic b", "Topic c"]


def test_a_group_of_mixed_types_names_the_type_of_each_neighbour() -> None:
    entity = note("n1", "T", [edge("nas:cites", "w1"), edge("nas:cites", "b1")])
    web = revision("w1", "nas:WebPage", {"title": "A page", "url": "https://example.org/p"})
    book = revision("b1", "nas:Book", {"title": "B book"})
    group = section(card(entity, web, book), "relations")["groups"][0]
    assert [(i["label"], i["type"]) for i in group["items"]] == [
        ("A page", "nas:WebPage"),
        ("B book", "nas:Book"),
    ]


def test_edge_qualifiers_are_shown_on_the_item() -> None:
    entity = note("n1", "T", [edge("nas:cites", "w1", locator="#shapes")])
    web = revision("w1", "nas:WebPage", {"title": "A page", "url": "https://example.org/p"})
    item = section(card(entity, web), "relations")["groups"][0]["items"][0]
    assert item["qualifiers"] == {"locator": "#shapes"}


def test_an_inactive_neighbour_says_so_and_a_missing_one_shows_its_id() -> None:
    entity = note("n1", "T", [edge("nas:about", "t1"), edge("nas:about", "gone")])
    old = revision("t1", "nas:Topic", {"label": "Old"}, status="superseded", alias="TOPIC-9")
    items = section(card(entity, old), "relations")["groups"][0]["items"]
    assert items[0] == {"ref": "TOPIC-9", "label": "Old", "record_status": "superseded"}
    assert items[1]["ref"].count("-") == 4 and "label" not in items[1]  # the id of a missing target


def test_a_missing_minimum_is_flagged() -> None:
    relations_ = {
        **MODEL.relations,
        "nas:about": replace(MODEL.relations["nas:about"], out=Bound(3, None)),
    }
    model = replace(MODEL, relations=relations_)
    entity = note("n1", "T", [edge("nas:about", "t1"), edge("nas:about", "t2")])
    topics = [topic("t1", "Physics"), topic("t2", "Optics")]
    groups = section(card(entity, *topics, model=model), "relations")["groups"]
    assert [(g["relation"], g["count"], g["missing"]) for g in groups] == [("nas:about", 2, 1)]


def test_what_an_entity_replaced_and_who_replaced_it_are_groups() -> None:
    new = note("n1", "New", [edge("core:supersedes", "n0")], alias="NOTE-0002")
    old = note("n0", "Old", status="superseded", alias="NOTE-0001")
    replaces = section(card(new, old), "relations")["groups"]
    assert [(g["relation"], g["direction"], g["count"]) for g in replaces] == [
        ("core:supersedes", "out", 1)
    ]
    assert replaces[0]["items"] == [
        {
            "ref": "NOTE-0001",
            "label": "Old",
            "record_status": "superseded",
            "field": {"name": "body", "value": "Text."},
        }
    ]
    data = card(old, new, superseded_by=[str(new.id)])
    replaced = section(data, "relations")["groups"]
    assert [(g["relation"], g["direction"]) for g in replaced] == [("core:supersedes", "in")]
    assert data["entity"]["superseded_by"] == [str(new.id)]


def test_at_most_eight_groups_and_the_others_are_named() -> None:
    relations_ = dict(MODEL.relations)
    edges = []
    for n in range(11):
        name = f"nas:r{n:02d}"
        relations_[name] = MODEL.relations["nas:about"]
        edges.append(edge(name, "t1"))
    model = replace(MODEL, relations=relations_)
    data = card(note("n1", "T", edges), topic("t1", "Physics"), model=model)
    rel = section(data, "relations")
    assert [g["relation"] for g in rel["groups"]] == [f"nas:r{n:02d}" for n in range(8)]
    assert rel["more_groups"] == ["nas:r08", "nas:r09", "nas:r10"]


def test_a_large_group_is_sampled_and_says_so() -> None:
    spec = with_sections(generated_spec(MODEL, "nas:Topic"), relations={"load_max": 4})
    notes = [note(f"n{i}", f"Note {i:02d}", edges=[edge("nas:about", "t1")]) for i in range(9)]
    notes.append(note("gone", "Retracted", edges=[edge("nas:about", "t1")], status="retracted"))
    group = section(card(topic("t1", "Physics"), *notes, spec=spec), "relations")["groups"][0]
    assert group["count"] == 10 and group["approx"] is True and group["sampled"] == 4
    assert group["more"] == 7  # count minus the three shown
    # an out group is exact however large: the edges are in the revision
    out = note("n", "T", [edge("nas:about", f"t{i}") for i in range(9)])
    topics = [topic(f"t{i}", f"Topic {i}") for i in range(9)]
    spec = with_sections(generated_spec(MODEL, "nas:Note"), relations={"load_max": 4})
    group = section(card(out, *topics, spec=spec), "relations")["groups"][0]
    assert group["count"] == 9 and group["sampled"] == 4 and "approx" not in group


def test_states_need_neighbours_that_share_one_lifecycle_attribute() -> None:
    entity = note("n1", "T", [edge("nas:cites", "w1"), edge("nas:cites", "b1")])
    web = revision("w1", "nas:WebPage", {"title": "A page", "url": "https://example.org/p"})
    book = revision("b1", "nas:Book", {"title": "B book"})
    group = section(card(entity, web, book), "relations")["groups"][0]
    assert "states" not in group  # sources have no lifecycle


# ---- files -----------------------------------------------------------------------------------


def test_an_entity_lists_its_files_with_pages_ordered_by_title_and_page() -> None:
    edges = [
        edge("files:from_file", "s2", page=9, last_page=11),
        edge("files:from_file", "s1", page=30),
        edge("files:from_file", "s1", page=4, locator="fig 2"),
        edge("files:from_file", "s3", page=1),
    ]
    entity = note("n1", "T", edges)
    scans = [
        scan("s1", "Alpha notebook", alias="SCAN-0001"),
        scan("s2", "Beta notebook", alias="SCAN-0002"),
        scan("s3", "gamma notebook"),
    ]
    data = card(entity, *scans)
    files = section(data, "files")
    assert files["items"] == [
        {"ref": "SCAN-0001", "title": "Alpha notebook", "page": 4, "locator": "fig 2"},
        {"ref": "SCAN-0001", "title": "Alpha notebook", "page": 30},
        {"ref": "SCAN-0002", "title": "Beta notebook", "page": 9, "last_page": 11},
        {"ref": str(scans[2].id), "title": "gamma notebook", "page": 1},
    ]
    assert "more" not in files
    # the file edges are the files section, not a relation group
    assert "relations" not in [s["kind"] for s in data["sections"]]
    assert "sha256" not in str(files)


def test_at_most_five_files_and_the_rest_is_counted() -> None:
    entity = note("n1", "T", [edge("files:from_file", "s1", page=p) for p in range(1, 9)])
    files = section(card(entity, scan("s1", "Notebook")), "files")
    assert [i["page"] for i in files["items"]] == [1, 2, 3, 4, 5]
    assert files["more"] == 3


def test_an_entity_without_files_has_no_files_section() -> None:
    assert [s["kind"] for s in card(note("n1", "T"))["sections"]] == ["fields", "affordances"]


def test_a_stored_file_shows_who_cites_it_and_the_pages_in_page_order() -> None:
    notebook = scan("s1", "Notebook", alias="SCAN-0001")
    notes = [
        note("n1", "Later", [edge("files:from_file", "s1", page=20)], alias="NOTE-0001"),
        note(
            "n2", "Early", [edge("files:from_file", "s1", page=5, last_page=7)], alias="NOTE-0002"
        ),
        note("n3", "Same page b", [edge("files:from_file", "s1", page=5)], alias="NOTE-0003"),
        note("n4", "Same page a", [edge("files:from_file", "s1", page=5)], alias="NOTE-0004"),
        note("n5", "Dropped", [edge("files:from_file", "s1", page=1)], status="retracted"),
        note("n6", "No page", [edge("files:from_file", "s1", locator="front")], alias="NOTE-0006"),
    ]
    data = card(notebook, *notes)
    cited = section(data, "files", "cited_by")
    assert cited["count"] == 5  # the retracted source is not counted
    assert (cited["page_min"], cited["page_max"], cited["pages_cited"]) == (5, 20, 4)
    assert cited["items"] == [
        {"ref": "NOTE-0002", "label": "Early", "page": 5, "last_page": 7},
        {"ref": "NOTE-0004", "label": "Same page a", "page": 5},
        {"ref": "NOTE-0003", "label": "Same page b", "page": 5},
        {"ref": "NOTE-0001", "label": "Later", "page": 20},
        {"ref": "NOTE-0006", "label": "No page"},
    ]
    assert "more" not in cited
    # the citations are not also a relation group
    assert "relations" not in [s["kind"] for s in data["sections"]]


def test_cited_by_counts_pages_without_listing_them() -> None:
    notebook = scan("s1", "Notebook")
    edges = [(1, 3), (2, 6), (10, 10), (4000000000, 4000000002)]
    notes = [
        note(f"n{i}", f"N{i}", [edge("files:from_file", "s1", page=a, last_page=b)])
        for i, (a, b) in enumerate(edges)
    ]
    cited = section(card(notebook, *notes), "files", "cited_by")
    assert (cited["page_min"], cited["page_max"]) == (1, 4000000002)
    assert cited["pages_cited"] == 6 + 1 + 3  # 1-6, 10, and the three huge ones


def test_a_large_file_is_sampled_and_the_statistics_use_every_edge() -> None:
    notebook = scan("s1", "Notebook")
    notes = [
        note(f"n{i:03d}", f"N{i:03d}", [edge("files:from_file", "s1", page=i + 1)])
        for i in range(30)
    ]
    spec = with_sections(generated_spec(MODEL, "nas:Scan"), files={"load_max": 10})
    cited = section(card(notebook, *notes, spec=spec), "files", "cited_by")
    assert cited["count"] == 30 and cited["approx"] is True and cited["sampled"] == 10
    assert (cited["page_min"], cited["page_max"], cited["pages_cited"]) == (1, 30, 30)
    assert [i["page"] for i in cited["items"]] == [1, 2, 3, 4, 5]


# ---- status, affordances ---------------------------------------------------------------------


def test_a_superseded_entity_shows_its_state_and_who_replaced_it_and_nothing_applies() -> None:
    entity = note("n1", "Old", state="published", status="superseded")
    data = card(entity, superseded_by=["NOTE-0002"], affordances={"record_status": "superseded"})
    assert data["entity"]["record_status"] == "superseded"
    assert data["entity"]["superseded_by"] == ["NOTE-0002"]
    assert section(data, "affordances") == {"kind": "affordances"}
    gone = card(note("n2", "Gone", status="retracted"), retracted_by=["NOTE-0003"])
    assert gone["entity"]["retracted_by"] == ["NOTE-0003"]
    assert "superseded_by" not in gone["entity"]


def test_the_affordance_summary_names_transitions_actions_and_functions() -> None:
    entity = note("n1", "T", state="draft", attrs={"body": ""})
    snapshot = DictSnapshot(entity)
    found: dict[str, JsonValue] = {"record_status": "active"}
    found.update(transitions(MODEL, entity, snapshot).to_json())
    found.update(relations(MODEL, entity, snapshot).to_json())
    found["actions"] = [
        {
            "action": "publish_note",
            "as": "note",
            "applies": True,
            "blocked_by": [],
            "unchecked": [],
        },
        {
            "action": "merge_notes",
            "as": "note",
            "applies": False,
            "blocked_by": [],
            "unchecked": [],
        },
    ]
    found["functions"] = [{"function": "notes_about", "as": "note"}]
    summary: Any = affordance_summary(found)
    assert summary == {
        "transitions": [
            {
                "attr": "status",
                "allowed": ["archived"],
                "blocked": [{"state": "published", "by": ["nas.body_not_empty"]}],
            }
        ],
        "actions": {"apply": ["publish_note"], "blocked": 1},
        "functions": ["notes_about"],
    }
    data = card(entity, affordances=found, spec=None)
    assert section(data, "affordances")["detail"] == f"graph get {entity.id} --affordances"
    aliased = card(replace(entity, alias="NOTE-0001"), affordances=found)
    assert section(aliased, "affordances")["detail"] == "graph get NOTE-0001 --affordances"
    assert "relations" not in section(aliased, "affordances")  # counts are in the groups


def test_affordance_names_are_capped_at_five_and_the_cut_is_counted() -> None:
    found: dict[str, JsonValue] = {
        "actions": [
            {"action": f"a{n}", "as": "x", "applies": True, "blocked_by": [], "unchecked": []}
            for n in range(8)
        ],
        "actions_more": 20,
        "functions": [{"function": f"f{n}", "as": "x"} for n in range(7)],
        "transitions": [
            {
                "attr": "status",
                "lifecycle": "L",
                "from": "a",
                "to": [{"state": f"s{n}", "allowed": True} for n in range(7)],
            }
        ],
    }
    summary: Any = affordance_summary(found)
    assert summary["actions"] == {"apply": ["a0", "a1", "a2", "a3", "a4"]}
    assert summary["actions_more"] == 3 + 20
    assert summary["functions"] == ["f0", "f1", "f2", "f3", "f4"]
    assert summary["functions_more"] == 2
    assert summary["transitions"] == [
        {"attr": "status", "allowed": ["s0", "s1", "s2", "s3", "s4"], "allowed_more": 2}
    ]


def test_no_affordances_gives_an_empty_section_without_detail() -> None:
    assert section(card(note("n1", "T"), affordances={}), "affordances") == {"kind": "affordances"}


# ---- determinism and the shape ---------------------------------------------------------------


def test_the_same_input_gives_the_same_bytes_and_bytes_is_the_size_of_the_document() -> None:
    entity = note("n1", "T", [edge("nas:about", f"t{i}") for i in range(5)])
    topics = [topic(f"t{i}", f"Topic {i}") for i in range(5)]
    spec = generated_spec(MODEL, "nas:Note")
    first = render(spec, entity, DictSnapshot(entity, *topics), MODEL)
    second = render(spec, entity, DictSnapshot(entity, *reversed(topics)), MODEL)
    assert first.to_json() == second.to_json()
    data = first.to_json()
    assert data["bytes"] == len(dumps(data))
    assert "cut" not in data


def test_no_key_has_an_empty_value() -> None:
    entity = note("n1", "T", [edge("nas:about", "t1")])
    data = card(entity, topic("t1", "Physics"), affordances={"record_status": "active"})

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert item not in (None, "", [], {}), key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)


def empty_values(value: object) -> list[str]:
    """The keys whose value is empty, anywhere in the document."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if item in (None, "", [], {}):
                found.append(key)
            found += empty_values(item)
    elif isinstance(value, list):
        for item in value:
            found += empty_values(item)
    return found


def test_an_entity_without_values_has_no_fields_section_and_no_empty_key() -> None:
    data = card(revision("t0", "nas:Topic", {}), affordances={"record_status": "active"})
    assert [s["kind"] for s in data["sections"]] == ["affordances"]
    assert section(data, "affordances") == {"kind": "affordances"}
    assert empty_values(data) == []


def test_a_sample_of_inactive_sources_leaves_the_items_key_out() -> None:
    spec = with_sections(generated_spec(MODEL, "nas:Topic"), relations={"load_max": 4})
    notes = [
        note(f"n{i}", f"Note {i}", [edge("nas:about", "t1")], status="retracted") for i in range(9)
    ]
    data = card(topic("t1", "Physics"), *notes, spec=spec)
    group = section(data, "relations")["groups"][0]
    assert group["count"] == 9 and group["approx"] is True and "items" not in group
    assert empty_values(data) == []


def test_a_relation_whose_sources_are_all_inactive_is_no_group_and_is_not_named() -> None:
    dead = [
        note(f"n{i}", f"Note {i}", [edge("nas:about", "t1")], status="retracted") for i in range(2)
    ]
    data = card(topic("t1", "Physics"), *dead)
    assert [s["kind"] for s in data["sections"]] == ["fields", "affordances"]


def test_more_groups_names_the_relations_past_the_eighth_without_direction() -> None:
    relations_ = dict(MODEL.relations)
    edges = []
    for n in range(9):
        name = f"nas:r{n:02d}"
        relations_[name] = MODEL.relations["nas:about"]
        edges.append(edge(name, "t1"))
    model = replace(MODEL, relations=relations_)
    entity = note("n1", "T", edges)
    topics = topic("t1", "Physics")
    inbound = note("n2", "Other", [edge("nas:r08", "n1")])  # r08 in both directions
    data = card(entity, topics, inbound, model=model)
    rel = section(data, "relations")
    assert [g["relation"] for g in rel["groups"]] == [f"nas:r{n:02d}" for n in range(8)]
    assert rel["more_groups"] == ["nas:r08"]  # named once, whichever direction was cut


def test_a_neighbour_carries_its_first_required_text_field_cut_to_the_cap() -> None:
    topic_ = topic("t1", "Physics")
    long = note("n1", "Long", [edge("nas:about", "t1")], attrs={"body": "w" * 90}, alias="N-1")
    short = note("n2", "Short", [edge("nas:about", "t1")], alias="N-2")
    items = section(card(topic_, long, short), "relations")["groups"][0]["items"]
    assert items[0]["field"] == {"name": "body", "value": "w" * 60 + "…"}
    assert items[1]["field"] == {"name": "body", "value": "Text."}
    # the label and the lifecycle state are not repeated as key fields; a topic has none
    plain = section(card(note("n3", "T", [edge("nas:about", "t1")]), topic_), "relations")
    assert "field" not in plain["groups"][0]["items"][0]


def test_a_key_field_comes_from_the_model_the_reader_sees() -> None:
    hidden = with_attribute(MODEL, "nas:Note", "body", "body", required=False)
    entity = topic("t1", "Physics")
    note_ = note("n1", "T", [edge("nas:about", "t1")], attrs={"kind": "summary"})
    plain = section(card(entity, note_, model=hidden), "relations")["groups"][0]["items"][0]
    assert plain["field"]["name"] == "kind"  # body is no longer required: the next one
    cut = replace(
        hidden,
        classes={
            **hidden.classes,
            "nas:Note": replace(
                hidden.classes["nas:Note"],
                attributes={
                    n: a
                    for n, a in hidden.classes["nas:Note"].attributes.items()
                    if n not in ("body", "kind")
                },
            ),
        },
    )
    gone = section(card(entity, note_, model=cut), "relations")["groups"][0]["items"][0]
    assert "field" not in gone  # an attribute the model does not have is not shown


def test_the_guard_drops_the_key_fields_of_neighbours_first() -> None:
    entity = topic("t1", "Physics")
    notes = [
        note(f"n{i}", f"Note {i}", [edge("nas:about", "t1")], attrs={"body": "b" * 55})
        for i in range(3)
    ]
    spec = generated_spec(MODEL, "nas:Topic")
    full = render(spec, entity, DictSnapshot(entity, *notes), MODEL)
    assert not full.cut and "field" in str(full.to_json())
    tight = replace(spec, byte_limit=full.size() - 1)
    guarded = render(tight, entity, DictSnapshot(entity, *notes), MODEL).to_json()
    group: Any = next(s for s in guarded["sections"] if s["kind"] == "relations")["groups"][0]  # type: ignore[index, attr-defined]
    assert guarded["cut"] is True and len(group["items"]) == 3
    assert all("field" not in i for i in group["items"])
