"""An authored view stays below its byte and work limits however big the entity is (plan V6, B4):
the guard trims by section index in a fixed order, exact counts survive, the same input gives the
same bytes."""

from dataclasses import replace
from typing import Any

from support.cards import CountingSnapshot, as_snapshot
from support.snapshots import DictSnapshot, edge, revision
from support.views import company_model, one_entry

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import Revision
from graph_cli.kinetic import card
from graph_cli.kinetic.card import BYTE_LIMIT, ENTITY_LOADS, dumps, render, spec_of
from graph_cli.kinetic.view_rows import RowsResult

MODEL = company_model()
BIG = 2000
HEAD = (
    "schema: graph_cli.view/1\nname: probe\nmodule: organization\nsummary: A probe.\n"
    "use_when: Testing.\ntakes: org:OrgUnit\nsections:\n"
)
ADVERSARIAL = (
    HEAD
    + """\
  - kind: fields
    fields: all
    max: 32
    value_chars: 2000
  - kind: relations
  - kind: neighbors
    id: positions
    relation: org:in_unit
    direction: in
    order: [title]
    fields: all
    max: 50
    relations:
      - all
      - {relation: org:holds, direction: in, fields: all, max: 10}
  - kind: neighbors
    id: again
    relation: org:in_unit
    direction: in
    order: ["title:desc"]
    fields: all
    max: 50
    relations:
      - {relation: org:holds, direction: in, fields: all, max: 10}
  - kind: rows
    id: members
    function: members_of_unit
    args: {unit: $entity}
    max: 50
  - kind: affordances
"""
)


def adversarial_world(positions: int = 24, holders: int = 4) -> tuple[Revision, list[Revision]]:
    unit = revision(
        "team",
        "org:Team",
        {"name": "N" * BIG, "alt_labels": ["A" * BIG, "B" * BIG]},
        [edge("org:part_of", "dept"), edge("org:charged_to", "cc")],
        alias="TEAM-" + "1" * 40,
    )
    others: list[Revision] = [
        revision("dept", "org:Department", {"name": "D" * BIG}, alias="DEPT-" + "1" * 40),
        revision("cc", "org:CostCenter", {"name": "C" * BIG, "code": "CC"}, alias="CC-" + "1" * 40),
    ]
    for i in range(positions):
        title = f"{i:03d}" + "T" * BIG
        pos_edges = [edge("org:in_unit", "team"), edge("org:carries", f"role{i}")]
        others.append(
            revision(
                f"p{i}",
                "org:Position",
                {"title": title, "status": "active"},
                pos_edges,
                alias=f"POS-{i:04d}" + "P" * 40,
            )
        )
        others.append(revision(f"role{i}", "org:Role", {"name": "R" * BIG}, alias=f"ROLE-{i}"))
        for h in range(holders):
            others.append(
                revision(
                    f"e{i}_{h}",
                    "ppl:Employee",
                    {
                        "name": "H" * BIG,
                        "status": "active",
                        "employee_number": f"E-{i}-{h}",
                        "work_email": "w" * 200 + "@example.org",
                    },
                    [edge("org:holds", f"p{i}", since="2024-01-01")],
                )
            )
    return unit, others


def big_rows() -> RowsResult:
    cells: tuple[dict[str, JsonValue], ...] = tuple(
        {"person": "P" * 5000, "position": "Q" * 5000, "tags": ["x" * 100] * 20} for _ in range(60)
    )
    return RowsResult(
        "members_of_unit", "sha256:" + "f" * 64, ("person", "position", "tags"), cells
    )


def render_big(
    snapshot: DictSnapshot | Any, unit: Revision, *, limit: int | None = None
) -> card.ViewModel:
    spec = spec_of(one_entry(ADVERSARIAL), MODEL)
    if limit is not None:
        spec = replace(spec, byte_limit=limit)
    return render(spec, unit, snapshot, MODEL, rows={"members": big_rows()}, affordances={})


def test_an_adversarial_authored_view_stays_below_the_card_limit() -> None:
    unit, others = adversarial_world()
    data: Any = render_big(DictSnapshot(unit, *others), unit).to_json()
    assert len(dumps(data)) < BYTE_LIMIT and data["bytes"] == len(dumps(data))
    assert data["cut"] is True
    assert data["entity"]["alias"].startswith("TEAM-")  # the header is never trimmed
    sections = {s["id"]: s for s in data["sections"]}
    assert sections["positions"]["count"] == 24 and sections["again"]["count"] == 24
    assert sections["members"]["count"] == 60 and "error" not in sections["members"]
    for sid in ("positions", "again", "members"):
        found = sections[sid]
        assert found["count"] == len(found.get("items", [])) + found.get("more", 0)


def test_the_same_input_gives_the_same_bytes_whatever_the_order_of_the_snapshot() -> None:
    unit, others = adversarial_world()
    first = dumps(render_big(DictSnapshot(unit, *others), unit).to_json())
    second = dumps(render_big(DictSnapshot(unit, *reversed(others)), unit).to_json())
    assert first == second


def test_the_loads_of_an_adversarial_view_are_bounded() -> None:
    unit, others = adversarial_world(positions=60, holders=12)
    counting = CountingSnapshot(DictSnapshot(unit, *others))
    render_big(as_snapshot(counting), unit)
    # the authored sections share ENTITY_LOADS; the generated parts keep their own caps
    assert len(counting.loaded) <= ENTITY_LOADS + 900


def test_a_limit_lower_than_the_cards_is_kept() -> None:
    unit, others = adversarial_world()
    data: Any = render_big(DictSnapshot(unit, *others), unit, limit=4500).to_json()
    assert data["bytes"] <= 4500 and data["cut"] is True


def test_every_stage_of_the_guard_works_in_its_fixed_order() -> None:
    """Sweeping the limit down from the untrimmed size, the effects of the stages appear in the
    order of the guard and never come back."""
    unit, others = adversarial_world(positions=6, holders=2)
    spec = replace(spec_of(one_entry(ADVERSARIAL), MODEL), byte_limit=10**9)
    full = render(spec, unit, DictSnapshot(unit, *others), MODEL, rows={"members": big_rows()})
    assert not full.cut
    order = [
        "row fields gone",
        "lists to one",
        "rows to three",
        "rows to one",
        "relation items to one",
        "groups moved",
        "fields moved",
        "rows gone",
    ]
    first: dict[str, int] = {}
    limit = full.size()
    while limit > 200:
        found = effects(card._guard(full, limit).to_json())  # type: ignore[arg-type]
        for effect in found:
            first.setdefault(effect, limit)
        limit = int(limit * 0.97)
    assert set(first) == set(order), set(order) ^ set(first)
    firsts = [first[e] for e in order]
    assert firsts == sorted(firsts, reverse=True), first  # each appears at a lower limit


def effects(document: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    sections = {s["id"]: s for s in document["sections"] if "id" in s}
    positions, members = sections["positions"], sections["members"]
    items = positions.get("items", [])
    if items and not any("fields" in r for r in items):
        found.add("row fields gone")
    groups = [g for r in items for g in r.get("relations", [])]
    if (
        groups
        and all(len(g.get("items", [])) <= 1 for g in groups)
        and any(g["count"] > 1 for g in groups)
    ):
        found.add("lists to one")
    if len(items) == 3 and positions["count"] > 3:
        found.add("rows to three")
    if len(items) == 1 and positions["count"] > 1:
        found.add("rows to one")
    if not items and positions["count"] > 0:
        found.add("rows gone")
    relations = sections["relations"]
    if any(len(g.get("items", [])) == 1 and g["count"] > 1 for g in relations.get("groups", [])):
        found.add("relation items to one")
    if relations.get("more_groups"):
        found.add("groups moved")
    del members
    fields = sections["fields"]
    if fields.get("other"):
        found.add("fields moved")
    return found
