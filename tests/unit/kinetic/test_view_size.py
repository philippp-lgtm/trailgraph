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
        "sections stubbed",
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
    if any(s.get("cut") is True for s in sections.values()):
        found.add("sections stubbed")
    if "count" not in positions:
        return found
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


# the pieces no stage of the card trims: titles, labels, error messages, stubs of sections

CJK = "漢"  # three bytes in UTF-8: the limit is in bytes, the caps are in characters


def cjk_world() -> tuple[Revision, DictSnapshot]:
    unit = revision(
        "team",
        "org:Team",
        {"name": CJK * 3000, "alt_labels": [CJK * 3000, CJK * 3000]},
        [edge("org:part_of", "dept")],
        alias="TEAM-0001",
    )
    return unit, DictSnapshot(unit, revision("dept", "org:Department", {"name": "D"}))


def over(text: str, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    unit, snapshot = cjk_world()
    spec = spec_of(one_entry(HEAD + text), MODEL)
    model = render(spec, unit, snapshot, MODEL, **kwargs)
    data: dict[str, Any] = model.to_json()  # type: ignore[assignment]
    assert len(dumps(data)) <= spec.byte_limit, len(dumps(data))
    assert data["bytes"] == len(dumps(data))
    return model, data


def test_a_long_label_of_an_authored_fields_section_is_shortened_last() -> None:
    _, data = over("  - kind: fields\n    fields: [name]\n    value_chars: 2000\n")
    assert data["cut"] is True
    item = data["sections"][0]["items"][0]
    assert item["role"] == "label" and item["chars"] == 3000 and len(item["value"]) <= 41


def test_several_authored_fields_sections_share_the_limit() -> None:
    sections = "".join(
        f"  - kind: fields\n    id: f{i}\n    fields: [name, alt_labels]\n    value_chars: 2000\n"
        for i in range(4)
    )
    _, data = over(sections)
    assert [s["id"] for s in data["sections"]] == ["f0", "f1", "f2", "f3"]


def test_titles_of_many_sections_go_before_anything_else_that_is_left() -> None:
    title = CJK * 100
    sections = "".join(
        f"  - kind: neighbors\n    id: n{i}\n    title: {title}\n    relation: org:part_of\n"
        for i in range(32)
    )
    _, data = over(sections)
    assert len(data["sections"]) == 32
    gone = ["title" not in s for s in data["sections"]]
    assert any(gone) and gone == sorted(gone)  # the titles go from the last section on
    assert all(s["count"] == 1 for s in data["sections"] if "count" in s)


def test_rows_errors_are_cut_when_the_section_is_built() -> None:
    errors = {
        f"r{i}": RowsResult("members_of_unit", "sha256:x", error=("query.too_large", "m" * 400))
        for i in range(20)
    }
    one = (
        "  - kind: rows\n    id: r{i}\n    function: members_of_unit\n    args: {{unit: $entity}}\n"
    )
    sections = "".join(one.format(i=i) for i in range(20))
    _, data = over(sections, rows=errors)
    messages = [s["error"]["message"] for s in data["sections"] if "error" in s]
    assert messages and all(len(m) <= 301 for m in messages)  # 300 characters and the ellipsis
    full = render(
        spec_of(
            one_entry(HEAD + sections[: sections.index("  - kind: rows\n    id: r1\n")]), MODEL
        ),
        cjk_world()[0],
        cjk_world()[1],
        MODEL,
        rows={"r0": errors["r0"]},
    ).to_json()
    assert full["sections"][0]["error"]["message"] == "m" * 300 + "…"  # type: ignore[index]


def test_what_a_section_cannot_keep_leaves_a_stub_with_its_kind_and_id() -> None:
    sections = "".join(
        f"  - kind: fields\n    id: f{i}\n    fields: [name, alt_labels]\n    value_chars: 2000\n"
        for i in range(4)
    )
    unit, snapshot = cjk_world()
    spec = replace(spec_of(one_entry(HEAD + sections), MODEL), byte_limit=1100)
    data: Any = render(spec, unit, snapshot, MODEL).to_json()
    stubs = [s for s in data["sections"] if s.get("cut") is True]
    assert stubs and all(set(s) == {"kind", "id", "cut"} for s in stubs)
    assert len(dumps(data)) <= 1100
    # the last sections go first: a stub never comes before a section that was kept whole
    kinds = [s.get("cut") is True for s in data["sections"]]
    assert kinds == sorted(kinds)


def test_the_generated_card_has_no_stub_stage() -> None:
    from support.card_scenarios import scenarios

    from graph_cli.kinetic.card import generated_spec

    scenario = next(s for s in scenarios() if s.name == "huge")
    spec = replace(generated_spec(scenario.model, scenario.class_curie), byte_limit=300)
    data: Any = render(spec, scenario.entity, scenario.snapshot, scenario.model).to_json()
    assert all(s.get("cut") is not True for s in data["sections"])  # it keeps its old stages
    assert any("title" not in s for s in data["sections"])


def test_cells_of_a_function_are_cut_and_the_cut_is_counted() -> None:
    sections = "  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n"
    result = RowsResult(
        "members_of_unit",
        None,
        ("a", "b", "c"),
        (
            {"a": "x" * 500, "b": ["y" * 500] * 9, "c": {"k": "z" * 500}},
            {"a": "short", "b": [1, 2], "c": 3},
        ),
    )
    _, data = over(sections, rows={"rows": result})
    rows = data["sections"][0]
    assert rows["items"][0]["a"] == "x" * 200 + "…"
    assert len(rows["items"][0]["b"]) == 5 and rows["items"][0]["b"][0] == "y" * 200 + "…"
    assert rows["items"][0]["c"] == {"k": "z" * 200 + "…"}
    assert rows["items"][1] == {"a": "short", "b": [1, 2], "c": 3}
    assert rows["cells_cut"] == 3


def test_a_view_without_the_limit_of_the_file_uses_the_cards_and_never_more() -> None:
    entry = one_entry(HEAD + "  - kind: files\n")
    assert spec_of(entry, MODEL).byte_limit == BYTE_LIMIT
