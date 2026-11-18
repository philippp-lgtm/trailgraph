"""Synthetic entities for the card guard golden: big, adversarial and ordinary cards.

The builders mirror the helpers of ``tests/unit/kinetic/test_card_size.py`` (which stays as it is);
each scenario is rendered without a byte limit and then guarded at many limits, so every stage of
the guard and every state between two stages is pinned.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.ontology_layer.model import CompiledModel
from support.cards import with_attribute
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

NOTES = compiled_model("notes_and_sources", "1.2.0")
LONG_LABEL = "L" * 300


def bigger_note_model(out: int, into: int, prefix: str = "nas:r") -> CompiledModel:
    model = NOTES
    for n in range(37):
        model = with_attribute(
            model, "nas:Note", f"attr_{n:02d}", "body", required=n < 10, unique=False
        )
    model = with_attribute(model, "nas:Note", "phase", "status")
    about = NOTES.relations["nas:about"]
    relations_ = dict(NOTES.relations)
    for n in range(out):
        relations_[f"{prefix}out_{n:02d}"] = about
    for n in range(into):
        relations_[f"{prefix}in_{n:02d}"] = replace(
            about, domain=("nas:Topic",), range=("nas:Note",)
        )
    return replace(model, relations=relations_)


def big_entity(
    out: int, into: int, per: int, files: int, prefix: str = "nas:r"
) -> tuple[Revision, list[Revision]]:
    values: dict[str, JsonValue] = {
        "title": LONG_LABEL,
        "body": "b" * 2000,
        "kind": "observation",
        "status": "draft",
        "phase": "draft",
    }
    values.update({f"attr_{n:02d}": "v" * 2000 for n in range(37)})
    edges: list[EdgeRecord] = []
    others: list[Revision] = []
    for r in range(out):
        for i in range(per):
            name = f"o{r}_{i}"
            edges.append(edge(f"{prefix}out_{r:02d}", name))
            others.append(revision(name, "nas:Topic", {"label": LONG_LABEL + name}))
    for f in range(files):
        name = f"f{f}"
        edges.append(edge("files:from_file", name, page=f + 1))
        scan: dict[str, JsonValue] = {
            "title": LONG_LABEL + name,
            "content": f"sha256:{f}",
            "media_type": "application/pdf",
        }
        others.append(revision(name, "nas:Scan", scan))
    entity = revision("big", "nas:Note", values, edges)
    for r in range(into):
        for i in range(per):
            name = f"i{r}_{i}"
            others.append(
                revision(
                    name,
                    "nas:Topic",
                    {"label": LONG_LABEL + name},
                    [edge(f"{prefix}in_{r:02d}", "big")],
                )
            )
    return entity, others


def affordances_of_many() -> dict[str, JsonValue]:
    long = "a_long_name_of_a_synthetic_entry"
    return {
        "record_status": "active",
        "transitions": [
            {
                "attr": attr,
                "lifecycle": "nas:NoteLifecycle",
                "from": "draft",
                "to": [
                    {
                        "state": "published",
                        "allowed": False,
                        "blocked_by": [{"guard": long, "summary": "x" * 200}],
                    },
                    {"state": "archived", "allowed": True},
                ],
            }
            for attr in ("status", "phase")
        ],
        "actions": [
            {
                "action": f"{long}_{n:03d}",
                "as": "note",
                "applies": n % 2 == 0,
                "blocked_by": [],
                "unchecked": [],
            }
            for n in range(40)
        ],
        "actions_more": 20,
        "functions": [{"function": f"{long}_{n:03d}", "as": "note"} for n in range(40)],
        "functions_more": 20,
    }


@dataclass(frozen=True)
class Scenario:
    name: str
    model: CompiledModel
    entity: Revision
    others: list[Revision]
    affordances: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def snapshot(self) -> DictSnapshot:
        return DictSnapshot(self.entity, *self.others)

    @property
    def class_curie(self) -> str:
        return str(self.entity.type)


def _note(name: str, title: str, edges: list[EdgeRecord], **attrs: Any) -> Revision:
    base: dict[str, JsonValue] = {"title": title, "body": "Text.", "kind": "observation"}
    base.update({"status": "draft", **attrs})
    return revision(name, "nas:Note", base, edges, alias=attrs.pop("alias", None))


def scenarios() -> list[Scenario]:
    found: list[Scenario] = []
    entity, others = big_entity(12, 4, 30, 30)
    found.append(Scenario("huge", bigger_note_model(12, 4), entity, others, affordances_of_many()))

    prefix = "nas:a_much_longer_relation_name_"
    entity, others = big_entity(20, 0, 3, 0, prefix=prefix)
    found.append(
        Scenario(
            "twenty_groups",
            bigger_note_model(20, 0, prefix=prefix),
            entity,
            others,
            affordances_of_many(),
        )
    )

    entity, others = big_entity(3, 2, 4, 2)
    entity = replace(
        entity,
        attrs={"title": "A note", "body": "Text.", "kind": "observation", "status": "draft"},
    )
    found.append(Scenario("ordinary", bigger_note_model(3, 2), entity, others))

    topic = revision("t1", "nas:Topic", {"label": "Physics"}, alias="TOPIC-1")
    notes = [
        revision(
            f"n{i}",
            "nas:Note",
            {"title": f"Note {i}", "body": "b" * 55, "kind": "observation", "status": "draft"},
            [edge("nas:about", "t1")],
        )
        for i in range(5)
    ]
    found.append(Scenario("key_fields", NOTES, topic, notes))

    files = [edge("files:from_file", f"s{i}", page=i + 1) for i in range(9)]
    scans = [
        revision(
            f"s{i}",
            "nas:Scan",
            {
                "title": f"Notebook {i}",
                "content": f"sha256:{i}",
                "media_type": "application/pdf",
                "file_name": f"{i}.pdf",
            },
        )
        for i in range(9)
    ]
    note = revision(
        "withfiles",
        "nas:Note",
        {
            "title": "Notes with many files",
            "body": "b" * 300,
            "kind": "observation",
            "status": "draft",
            "alt_labels": ["a one", "a two", "a three", "a four", "a five"],
        },
        files,
    )
    found.append(Scenario("files", NOTES, note, scans))

    notebook = revision(
        "book",
        "nas:Scan",
        {
            "title": "Notebook",
            "content": "sha256:book",
            "media_type": "application/pdf",
            "file_name": "book.pdf",
        },
        alias="SCAN-1",
    )
    citing = [
        revision(
            f"c{i}",
            "nas:Note",
            {"title": f"Citing note {i}" + "x" * 40, "body": "t", "kind": "observation"},
            [edge("files:from_file", "book", page=i % 7 + 1)],
            alias=f"NOTE-{i:04d}",
        )
        for i in range(40)
    ]
    found.append(Scenario("cited_by", NOTES, notebook, citing))

    # one relation that reads both ways: its out group and its in group are trimmed in turn
    about = NOTES.relations["nas:about"]
    twin = replace(about, domain=("nas:Note",), range=("nas:Note",))
    model = bigger_note_model(6, 0)
    model = replace(model, relations={**model.relations, "nas:twin": twin})
    entity, others = big_entity(6, 0, 4, 0)
    entity = replace(
        entity,
        edges=(*entity.edges, *(edge("nas:twin", f"w{i}") for i in range(6))),
    )
    others = [
        *others,
        *(
            revision(
                f"w{i}",
                "nas:Note",
                {"title": f"Twin {i}", "body": "t", "kind": "observation", "status": "draft"},
                [edge("nas:twin", "big")],
            )
            for i in range(6)
        ),
    ]
    found.append(Scenario("twin_relation", model, entity, others, affordances_of_many()))
    return found


def stages_seen(document: dict[str, object]) -> set[str]:
    seen: set[str] = set()
    sections = document["sections"]
    assert isinstance(sections, list)
    for section in sections:
        kind, variant = section["kind"], section.get("variant")
        if kind == "relations":
            groups = section.get("groups", [])
            if any("field" in i for g in groups for i in g.get("items", [])):
                seen.add("key fields kept")
            elif groups:
                seen.add("key fields dropped")
            if any(len(g.get("items", [])) == 1 and g["count"] > 1 for g in groups):
                seen.add("items to one")
            if section.get("more_groups"):
                seen.add("groups moved")
        elif kind == "fields":
            if any(
                len(i["value"]) == 41
                for i in section.get("items", [])
                if isinstance(i["value"], str)
            ):
                seen.add("values shortened")
            if section.get("other_more") or len(section.get("other", [])) > 3:
                seen.add("fields moved")
        elif kind == "files" and variant is None and section.get("more"):
            seen.add("files cut")
        elif variant == "cited_by" and len(section.get("items", [])) <= 1 and section["count"] > 1:
            seen.add("citations cut")
    return seen
