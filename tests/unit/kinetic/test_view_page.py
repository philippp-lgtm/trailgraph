"""Set views: pages cut at whole rows, cursors, and the rows of a listing (plan V6, B5, B6)."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest
from support.cards import CountingSnapshot, as_snapshot
from support.snapshots import DictSnapshot, edge, eid, revision
from support.views import company_model, compiled_entries

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.records import Revision
from graph_cli.kinetic.card import ViewSpec, dumps, spec_of
from graph_cli.kinetic.view_page import (
    PAGE_BYTES,
    Cursor,
    cursor_key,
    decode_cursor,
    render_set,
)
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.query.plan import FindSpec, plan_find
from graph_cli.services.read import entity_json

MODEL = company_model()
ENTRY = compiled_entries()["org_catalog"]
KEY = cursor_key(ENTRY.hash, {})
BIG = 5000
type Data = dict[str, Any]


def world(units: int = 12, per: int = 3) -> list[Revision]:
    found: list[Revision] = [
        revision(
            "dept", "org:Department", {"name": "Engineering", "alt_labels": ["Eng"]}, alias="DEPT-1"
        )
    ]
    for u in range(units):
        found.append(
            revision(
                f"team{u}",
                "org:Team",
                {"name": f"Team {u:02d}", "alt_labels": [f"T{u}"]},
                [edge("org:part_of", "dept")],
                alias=f"TEAM-{u:02d}",
            )
        )
        for p in range(per):
            name = f"pos{u}_{p}"
            found.append(
                revision(
                    name,
                    "org:Position",
                    {"title": f"Role {u:02d}-{p}", "status": "active"},
                    [edge("org:in_unit", f"team{u}")],
                    alias=f"POS-{u:02d}{p}",
                )
            )
            if p < 2:
                found.append(
                    revision(
                        f"emp{u}_{p}",
                        "ppl:Employee",
                        {
                            "name": f"Person {u:02d}-{p}",
                            "status": "active",
                            "employee_number": f"E-{u:02d}{p}",
                            "work_email": f"p{u}{p}@example.org",
                        },
                        [edge("org:holds", name, since="2024-01-01")],
                        alias=f"EMP-{u:02d}{p}",
                    )
                )
    found.append(
        revision(
            "ext",
            "ppl:External",
            {"name": "Contractor", "kind": "contractor"},
            alias="EXT-1",
        )
    )
    return found


def rows_result(count: int = 5) -> RowsResult:
    cells: tuple[Mapping[str, JsonValue], ...] = tuple(
        {"person": f"P{i:02d}", "training": "Safety", "completed_on": "2026-01-01"}
        for i in range(count)
    )
    return RowsResult(
        "training_completions", "sha256:fn", ("person", "training", "completed_on"), cells
    )


def spec_for(entry: Any = ENTRY, **params: Any) -> ViewSpec:
    return spec_of(entry, MODEL, params)


def page(
    snapshot: DictSnapshot | Any,
    *,
    spec: ViewSpec | None = None,
    cursor: Cursor | None = None,
    **kwargs: Any,
) -> Any:
    kwargs.setdefault("rows", {"completions": rows_result()})
    return render_set(spec or spec_for(), snapshot, MODEL, cursor=cursor, key=KEY, **kwargs)


def pages(snapshot: DictSnapshot, limit: int, **kwargs: Any) -> list[Data]:
    """Every page of the view at ``limit`` bytes, following ``next``."""
    found: list[Data] = []
    cursor = None
    while True:
        model = page(snapshot, page_bytes=limit, cursor=cursor, **kwargs)
        data: Data = model.to_json()
        found.append(data)
        if "next" not in data:
            return found
        cursor = decode_cursor(data["next"])
        assert len(found) < 5000


def whole(snapshot: DictSnapshot, **kwargs: Any) -> Data:
    data: Data = page(snapshot, page_bytes=262_144, **kwargs).to_json()
    assert "next" not in data
    return data


def rows_by_section(data: Sequence[Data]) -> dict[str, list[Any]]:
    """The items of every section over a list of pages, in order."""
    found: dict[str, list[Any]] = {}
    for one in data:
        for section in one["sections"]:
            found.setdefault(section["id"], []).extend(section.get("items", []))
    return found


# one page


def test_a_set_view_that_fits_is_one_page_with_every_section() -> None:
    snapshot = DictSnapshot(*world())
    data = whole(snapshot)
    assert list(data) == ["view", "sections", "bytes"]
    assert data["view"] == {"name": "org_catalog", "origin": "authored", "hash": ENTRY.hash}
    assert "entity" not in data
    assert [(s["kind"], s["id"]) for s in data["sections"]] == [
        ("list", "units"),
        ("list", "positions"),
        ("list", "people"),
        ("rows", "completions"),
    ]
    counts = {s["id"]: s["count"] for s in data["sections"]}
    assert counts == {"units": 13, "positions": 36, "people": 25, "completions": 5}
    assert data["bytes"] == len(dumps(data))


def test_the_rows_of_a_listing_are_what_find_returns_for_the_type() -> None:
    """The generic oracle (B6): the rows of a ``list`` section are exactly the active entities
    ``find --type`` plans and returns, in its order, with their attributes as ``get`` shows them
    and the targets of their out-edges, keyed by alias."""
    revisions = world()
    snapshot = DictSnapshot(*revisions)
    data = whole(snapshot)
    sections = {s["id"]: s for s in data["sections"]}
    for sid, type_, sort in (
        ("units", "org:OrgUnit", ()),
        ("positions", "org:Position", ("title",)),
        ("people", "ppl:Person", ()),
    ):
        asked = plan_find(MODEL, FindSpec(types=(type_,), sort=sort, limit=500))
        found = snapshot.query(asked).items
        items = sections[sid]["items"]
        assert [i["ref"] for i in items] == [r.alias or str(r.id) for r in found], sid
        by_ref = {r.alias or str(r.id): r for r in found}
        for item in items:
            shown = entity_json(by_ref[item["ref"]])
            assert item["fields"] == shown["attrs"], item["ref"]
            targets: dict[str, list[str]] = {}
            for e in shown["edges"]:  # type: ignore[attr-defined]
                other = next(r for r in revisions if str(r.id) == e["target"])
                targets.setdefault(e["rel"], []).append(other.alias or str(other.id))
            out = {
                g["relation"]: sorted(i["ref"] for i in g["items"])
                for g in item.get("relations", [])
                if g["direction"] == "out"
            }
            assert out == {k: sorted(v) for k, v in targets.items()}, item["ref"]


def test_a_listing_of_several_concrete_classes_names_the_class_of_every_row() -> None:
    data = whole(DictSnapshot(*world()))
    sections = {s["id"]: s for s in data["sections"]}
    assert {i["type"] for i in sections["units"]["items"]} == {"org:Department", "org:Team"}
    assert {i["type"] for i in sections["people"]["items"]} == {"ppl:Employee", "ppl:External"}
    assert all("type" not in i for i in sections["positions"]["items"])  # one concrete class


def test_a_listing_of_one_class_names_none_even_when_the_page_shows_one_kind() -> None:
    """The decision is by the classes, not by the data on the page."""
    only_teams = [r for r in world() if r.type != TermRef("org:Department")]
    data = whole(DictSnapshot(*only_teams))
    assert all("type" in i for i in {s["id"]: s for s in data["sections"]}["units"]["items"])


def test_values_are_never_cut_in_a_set_view() -> None:
    big = revision(
        "big",
        "ppl:Employee",
        {"name": "N" * BIG, "status": "active", "employee_number": "E-1", "work_email": "a@b.c"},
        alias="EMP-BIG",
    )
    data = whole(DictSnapshot(big))
    people = {s["id"]: s for s in data["sections"]}["people"]
    assert people["items"][0]["fields"]["name"] == "N" * BIG
    assert "chars" not in people["items"][0] and people["items"][0]["label"] == "N" * BIG
    cells = RowsResult("training_completions", None, ("a",), ({"a": "x" * BIG},))
    data = whole(DictSnapshot(big), rows={"completions": cells})
    assert {s["id"]: s for s in data["sections"]}["completions"]["items"][0]["a"] == "x" * BIG


# pages


@pytest.mark.parametrize("limit", [1500, 3000, 6000, 20000])
def test_the_pages_put_together_are_the_one_big_page(limit: int) -> None:
    snapshot = DictSnapshot(*world())
    one = whole(snapshot)
    many = pages(snapshot, limit)
    assert len(many) > 1 or limit >= 20000
    expected = {s["id"]: s.get("items", []) for s in one["sections"]}
    assert rows_by_section(many) == expected
    for data in many:
        assert data["bytes"] == len(dumps(data))
        assert data["bytes"] <= limit or data.get("oversize") is True
    for section in {s["id"]: s["count"] for s in one["sections"]}.items():
        counted = {s["count"] for d in many for s in d["sections"] if s["id"] == section[0]}
        assert counted == {section[1]}  # the count is the whole section on every page


def test_a_page_says_where_a_section_continues() -> None:
    many = pages(DictSnapshot(*world()), 3000)
    seen: dict[str, int] = {}
    for data in many:
        for section in data["sections"]:
            if section["kind"] != "list" or not section.get("items"):
                continue
            expected = seen.get(section["id"], 0)
            assert section.get("from", 0) == expected, section["id"]
            seen[section["id"]] = expected + len(section["items"])
    assert seen == {"units": 13, "positions": 36, "people": 25}


def test_next_and_more_come_only_before_the_end() -> None:
    many = pages(DictSnapshot(*world()), 3000)
    assert all("next" in d and d["more"] is True for d in many[:-1])
    assert "next" not in many[-1] and "more" not in many[-1]
    first = decode_cursor(many[0]["next"])
    assert (first.seq, first.key) == (1, KEY) and first.offset >= 0


def test_the_same_input_gives_the_same_bytes() -> None:
    snapshot = DictSnapshot(*world())
    first = [dumps(p) for p in pages(snapshot, 3000)]
    again = [dumps(p) for p in pages(DictSnapshot(*reversed(world())), 3000)]
    assert first == again


def test_the_default_page_is_below_the_spill_limit_and_a_file_can_ask_for_more() -> None:
    assert PAGE_BYTES == 12_288
    snapshot = DictSnapshot(*world(units=40))
    rows = {"completions": rows_result()}
    bare = replace(spec_for(), page_bytes=None)
    plain = render_set(bare, snapshot, MODEL, key=KEY, rows=rows).to_json()
    assert plain["bytes"] <= PAGE_BYTES and "next" in plain
    assert spec_for().page_bytes == 65536  # the file of org_catalog asks for 64 KiB
    asked = render_set(spec_for(), snapshot, MODEL, key=KEY, rows=rows).to_json()
    assert PAGE_BYTES < asked["bytes"] <= 65536
    explicit = render_set(spec_for(), snapshot, MODEL, key=KEY, rows=rows, page_bytes=4000)
    assert explicit.to_json()["bytes"] <= 4000  # the call beats the file


def test_a_page_limit_outside_the_range_is_refused() -> None:
    snapshot = DictSnapshot(*world(2))
    for bad in (0, 262_145):
        with pytest.raises(GraphCliError) as exc:
            page(snapshot, page_bytes=bad)
        assert exc.value.code == "usage.invalid_argument"


def test_a_row_larger_than_the_page_goes_alone_and_is_marked() -> None:
    revisions = world(units=2, per=1)
    big = revision(
        "big",
        "ppl:Employee",
        {"name": "N" * BIG, "status": "active", "employee_number": "E-9", "work_email": "a@b.c"},
        alias="EMP-ZZZ",
    )
    many = pages(DictSnapshot(*revisions, big), 1500)
    flagged = [d for d in many if d.get("oversize")]
    assert len(flagged) == 1
    rows = [i for s in flagged[0]["sections"] for i in s.get("items", [])]
    assert len(rows) == 1 and rows[0]["ref"] == "EMP-ZZZ"
    assert flagged[0]["bytes"] > 1500
    assert all(d["bytes"] <= 1500 for d in many if not d.get("oversize"))
    one = whole(DictSnapshot(*revisions, big))
    assert rows_by_section(many) == {s["id"]: s.get("items", []) for s in one["sections"]}


def test_a_page_always_holds_at_least_one_row() -> None:
    many = pages(DictSnapshot(*world(units=2, per=1)), 1)
    assert all(sum(len(s.get("items", [])) for s in d["sections"]) == 1 for d in many[:-1])
    assert all(d.get("oversize") for d in many)


def test_a_loads_bound_ends_a_page_at_the_same_row_every_time() -> None:
    snapshot = DictSnapshot(*world())
    first = pages(snapshot, 262_144, page_loads=40)
    assert len(first) > 1 and all("next" in d for d in first[:-1])
    assert [dumps(d) for d in first] == [dumps(d) for d in pages(snapshot, 262_144, page_loads=40)]
    # the loads of a row count: its neighbours are read for it, so a page of loads=40 is short
    one = whole(snapshot)
    assert rows_by_section(first) == {s["id"]: s.get("items", []) for s in one["sections"]}
    counting = CountingSnapshot(snapshot)
    render_set(
        spec_for(),
        as_snapshot(counting),
        MODEL,
        key=KEY,
        page_loads=40,
        rows={"completions": rows_result()},
    )
    assert len(counting.loaded) < 200  # one page reads a few dozen revisions, not the graph


def test_the_work_of_a_page_is_bounded_by_the_loads_budget() -> None:
    snapshot = DictSnapshot(*world(units=30))
    counting = CountingSnapshot(snapshot)
    model = render_set(
        spec_for(),
        as_snapshot(counting),
        MODEL,
        key=KEY,
        page_loads=100,
        page_bytes=262_144,
        rows={"completions": rows_result()},
    )
    assert model.next is not None
    assert len(counting.loaded) <= 100 + 60  # the budget plus what the last row itself needs


# cursors


def test_a_cursor_round_trips() -> None:
    cursor = Cursor(41, 2, 17, KEY)
    assert cursor.encode() == f"v1.41.2.17.{KEY}"
    assert decode_cursor(cursor.encode()) == cursor


@pytest.mark.parametrize(
    "text",
    [
        "",
        "v1",
        "v2.1.0.0.abcdef01",
        "v1.1.0.0.xyz",
        "v1.-1.0.0.abcdef01",
        "v1.1.0.0.ABCDEF01",
        "garbage",
    ],
)
def test_a_malformed_cursor_is_invalid(text: str) -> None:
    with pytest.raises(GraphCliError) as exc:
        decode_cursor(text)
    assert exc.value.code == "view.cursor_invalid"


def test_a_cursor_of_another_view_or_other_parameters_is_invalid() -> None:
    snapshot = DictSnapshot(*world())
    first = decode_cursor(pages(snapshot, 3000)[0]["next"])
    other = cursor_key(ENTRY.hash, {"since": "2020-01-01"})
    assert other != KEY and cursor_key(ENTRY.hash, {}) == KEY
    for bad in (replace(first, key=other), replace(first, key=cursor_key("sha256:other", {}))):
        with pytest.raises(GraphCliError) as exc:
            page(snapshot, cursor=bad)
        assert exc.value.code == "view.cursor_invalid"
    beyond = replace(first, section=9)
    with pytest.raises(GraphCliError) as exc:
        page(snapshot, cursor=beyond)
    assert exc.value.code == "view.cursor_invalid"


def test_a_cursor_of_another_graph_state_is_stale() -> None:
    first = decode_cursor(pages(DictSnapshot(*world(), seq=7), 3000)[0]["next"])
    assert first.seq == 7
    with pytest.raises(GraphCliError) as exc:
        page(DictSnapshot(*world(), seq=8), cursor=first)
    assert exc.value.code == "view.cursor_stale"
    assert exc.value.details == {"cursor_seq": 7, "seq": 8}
    assert "--as-of tx:7" in exc.value.message
    page(DictSnapshot(*world(), seq=7), cursor=first)  # the same state reads on


def test_the_key_depends_on_the_view_and_the_parameters_only() -> None:
    assert cursor_key("sha256:a", {"x": 1}) == cursor_key("sha256:a", {"x": 1})
    assert cursor_key("sha256:a", {"x": 1, "y": 2}) == cursor_key("sha256:a", {"y": 2, "x": 1})
    assert (
        len(
            {
                cursor_key("sha256:a", {"x": 1}),
                cursor_key("sha256:a", {"x": 2}),
                cursor_key("sha256:b", {"x": 1}),
            }
        )
        == 3
    )


# sections


def test_an_empty_listing_is_a_section_with_a_count_of_zero_and_is_not_repeated() -> None:
    teams_only = [r for r in world(units=3, per=1) if r.type == TermRef("org:Team")]
    many = pages(DictSnapshot(*teams_only), 800)
    for sid in ("positions", "people"):
        found = [s for d in many for s in d["sections"] if s["id"] == sid]
        assert found == [{"kind": "list", "id": sid, "type": found[0]["type"], "count": 0}], sid


def test_the_rows_of_a_function_are_paged_like_a_listing() -> None:
    snapshot = DictSnapshot(*world(2, 1))
    many = pages(snapshot, 1200, rows={"completions": rows_result(40)})
    cells = [
        i
        for d in many
        for s in d["sections"]
        if s["id"] == "completions"
        for i in s.get("items", [])
    ]
    assert [c["person"] for c in cells] == [f"P{i:02d}" for i in range(40)]
    parts = [s for d in many for s in d["sections"] if s["id"] == "completions"]
    assert all(s["count"] == 40 and "more" not in s for s in parts)
    assert len(parts) > 1 and parts[0].get("from", 0) == 0 and all(s["from"] > 0 for s in parts[1:])
    assert parts[0]["function"] == {"name": "training_completions", "hash": "sha256:fn"}


def test_a_failing_section_answers_alone_once_and_the_rest_pages_on() -> None:
    failed = RowsResult("training_completions", "sha256:fn", error=("query.too_large", "too big"))
    snapshot = DictSnapshot(*world())
    many = pages(snapshot, 3000, rows={"completions": failed})
    errors = [s for d in many for s in d["sections"] if s["kind"] == "rows"]
    assert errors == [
        {
            "kind": "rows",
            "id": "completions",
            "function": {"name": "training_completions", "hash": "sha256:fn"},
            "error": {"code": "query.too_large", "message": "too big"},
        }
    ]
    assert rows_by_section(many)["units"] and "next" not in many[-1]
    missing = pages(snapshot, 262_144, rows={})
    assert missing[0]["sections"][-1]["error"]["code"] == "view.section_unavailable"


def test_a_listing_reads_through_the_snapshot_it_is_given() -> None:
    """Hidden entities are not rows: the snapshot is the reader's filtered one."""
    revisions = world(units=3, per=2)
    visible = [r for r in revisions if r.type != TermRef("org:Position")]
    data = whole(DictSnapshot(*visible))
    sections = {s["id"]: s for s in data["sections"]}
    assert sections["positions"]["count"] == 0
    units = sections["units"]["items"]
    assert all(g["relation"] != "org:in_unit" for u in units for g in u.get("relations", []))


def test_where_order_and_parameters_of_a_listing_are_bound_typed() -> None:
    entry = compiled_entries()["tasks_by_state"]
    tasks = [
        revision(
            f"t{i}",
            "ops:Task",
            {
                "title": f"Task {i}",
                "status": "open" if i % 2 else "done",
                "due_on": f"2026-0{i + 1}-01",
            },
            alias=f"TASK-{i}",
        )
        for i in range(6)
    ]
    snapshot = DictSnapshot(*tasks)
    key = cursor_key(entry.hash, {"status": "open", "before": "2026-05-01"})
    spec = spec_of(entry, MODEL, {"status": "open", "before": "2026-05-01"})
    data = render_set(spec, snapshot, MODEL, key=key).to_json()
    items = data["sections"][0]["items"]
    assert [i["ref"] for i in items] == ["TASK-1", "TASK-3"]  # open, due before May, by due date
    assert items[0]["fields"] == {"title": "Task 1", "due_on": "2026-02-01", "status": "open"}
    assert data["sections"][0]["count"] == 2
    other = spec_of(entry, MODEL, {"status": "done", "before": "2100-01-01"})
    both = render_set(other, snapshot, MODEL, key=key).to_json()
    assert [i["ref"] for i in both["sections"][0]["items"]] == ["TASK-0", "TASK-2", "TASK-4"]


def test_shown_names_the_rows_of_the_page() -> None:
    snapshot = DictSnapshot(*world(2, 1))
    model = page(snapshot, page_bytes=262_144)
    named = set(model.shown())
    assert eid("team0") in named and eid("pos0_0") in named and eid("emp0_0") in named


def test_a_cursor_that_points_past_the_end_of_its_section_is_invalid() -> None:
    snapshot = DictSnapshot(*world())
    first = decode_cursor(pages(snapshot, 3000)[0]["next"])
    for offset in (13, 14, 1_000_000):  # units has 13 rows: row 13 does not exist
        bad = replace(first, section=0, offset=offset)
        with pytest.raises(GraphCliError) as exc:
            page(snapshot, cursor=bad)
        assert exc.value.code == "view.cursor_invalid" and exc.value.details["reason"] == "offset"
    ok = replace(first, section=0, offset=12)  # the last row is a valid place to start
    assert page(snapshot, cursor=ok, page_bytes=262_144).to_json()["sections"][0]["from"] == 12
    start = replace(first, section=1, offset=0)  # offset 0 is always a valid place
    page(snapshot, cursor=start)
    rows_cursor = replace(first, section=3, offset=5)  # five function rows: row 5 does not exist
    with pytest.raises(GraphCliError) as exc:
        page(snapshot, cursor=rows_cursor)
    assert exc.value.details["reason"] == "offset"


class Barred(DictSnapshot):
    """A snapshot that cannot hand out some entities by ``get_many`` although edges still lead
    to them (an entity hidden by its own class: the edges of a revision are not stripped)."""

    def __init__(self, *revisions: Revision, barred: set[str]) -> None:
        super().__init__(*revisions)
        self.barred = {eid(n) for n in barred}

    def get_many(self, entities: Sequence[Any]) -> Mapping[Any, Revision]:
        return {e: r for e, r in super().get_many(entities).items() if e not in self.barred}


def test_a_neighbour_that_cannot_be_read_is_absent_not_a_raw_id() -> None:
    revisions = world(units=2, per=1)
    snapshot = Barred(*revisions, barred={"dept", "pos0_0"})
    data = whole(snapshot)
    text = dumps(data).decode()
    assert str(eid("dept")) not in text and str(eid("pos0_0")) not in text
    units = {s["id"]: s for s in data["sections"]}["units"]["items"]
    team = next(u for u in units if u["ref"] == "TEAM-00")
    assert "relations" not in team  # its one relation led to the barred department
    # the same holds for the neighbours of a listed row that are listed too
    items = {s["id"]: s for s in data["sections"]}["positions"]["items"]
    holder = next(i for i in items if i["ref"] == "POS-010")
    assert "relations" in holder  # a position still names the unit it sits in
