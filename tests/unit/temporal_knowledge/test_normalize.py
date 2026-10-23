"""normalize and the working state (contracts B3.6, B3.16, B1.8, B8.3-B8.6, B8.14; plan W4)."""

from dataclasses import replace
from typing import Any
from uuid import UUID

from support.clock import FixedClock
from support.fixtures import compiled_model
from support.ids import SeededIdProvider
from support.snapshots import AT, DictSnapshot, edge, eid, revision

from graph_cli.kernel.changeset import parse_changeset
from graph_cli.kernel.ids import EntityId, TermRef, TxId
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.records import revision_content_hash
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.normalize import normalize
from graph_cli.temporal_knowledge.working import WorkingState

INV = compiled_model("inventory")
NAS = compiled_model("notes_and_sources")
TX = TxId(UUID("0192f6d4-2a10-7b41-9c3d-5e6f70819a2b"))
PREFIXES = {"inv:Item": "ITEM", "inv:Location": "LOC", "nas:Note": "NOTE"}

ROOM = revision("room", "inv:Location", {"name": "Room", "code": "R1"}, alias="LOC-0001")
DESK = revision(
    "desk",
    "inv:Item",
    {"name": "Desk", "quantity": 1, "status": "in_stock"},
    [edge("inv:located_in", "room", name="desk-room")],
    alias="ITEM-0001",
    rev=2,
)


class Ids:
    def __init__(self) -> None:
        self.provider = SeededIdProvider(FixedClock("2026-10-15T10:00:00Z"), seed=7)
        self.minted: list[UUID] = []

    def __call__(self) -> UUID:
        value = self.provider.new_id()
        self.minted.append(value)
        return value


def run(
    *ops: dict[str, Any],
    base: DictSnapshot | None = None,
    model: CompiledModel = INV,
    refs: dict[str, EntityId] | None = None,
    counters: dict[str, int] | None = None,
    allocate: bool = True,
    ids: Ids | None = None,
) -> WorkingState:
    cs = parse_changeset({"ops": list(ops)})
    return normalize(
        cs.ops,
        base if base is not None else DictSnapshot(ROOM, DESK),
        model,
        refs=refs if refs is not None else {"LOC-0001": ROOM.id, "ITEM-0001": DESK.id},
        new_id=ids or Ids(),
        recorded_at=AT,
        tx_id=TX,
        tx_seq=9,
        pin_version="1.0.0",
        alias_counters=counters if counters is not None else {"ITEM": 1, "LOC": 1},
        alias_prefixes=PREFIXES,
        allocate_aliases=allocate,
    )


def only_codes(ws: WorkingState) -> list[str]:
    return [f.code for f in ws.findings]


def test_create_applies_defaults_initial_state_alias_and_ids_in_order() -> None:
    ids = Ids()
    ws = run(
        {
            "op": "create",
            "type": "inv:Item",
            "local": "a",
            "attrs": {"name": "A"},
            "edges": [{"rel": "inv:located_in", "target": "@b"}],
        },
        {"op": "create", "type": "inv:Location", "local": "b", "attrs": {"name": "B", "code": "B"}},
        ids=ids,
    )
    a, b = (EntityId(ids.minted[0]), EntityId(ids.minted[1]))
    assert list(ws.revisions) == [a, b] and ws.created == {a, b}
    item = ws.revisions[a]
    assert item.attrs == {"name": "A", "quantity": 1, "status": "ordered"}
    assert item.edges[0].edge_id == ids.minted[2] and item.edges[0].target == b
    assert (item.alias, ws.revisions[b].alias) == ("ITEM-0002", "LOC-0002")
    assert ws.alias_allocations == ((a, "ITEM-0002"), (b, "LOC-0002"))
    assert (item.rev, item.prev_rev, item.created_at, item.tx_seq) == (1, None, AT, 9)
    assert item.content_hash == revision_content_hash(item)
    assert ws.resolution == {"locals": {"a": str(a), "b": str(b)}, "refs": {}}
    assert ws.local_of(a) == "a" and not ws.findings
    assert ws.effects[0].initial == (("status", "ordered"),)


def test_dry_run_allocates_no_alias() -> None:
    ws = run({"op": "create", "type": "inv:Item", "attrs": {"name": "A"}}, allocate=False)
    (created,) = ws.revisions.values()
    assert created.alias is None and ws.alias_allocations == ()


def test_text_values_are_coerced_per_base_type() -> None:
    ws = run(
        {
            "op": "create",
            "type": "inv:Item",
            "attrs_text": {
                "name": "12",
                "quantity": "3",
                "acquired_on": "2026-01-02",
                "condition": "good",
                "serial_number": "SN-1",
                "extra": "x",
            },
        },
        {"op": "update", "id": "ITEM-0001", "set_text": {"quantity": "many"}},
    )
    created = next(r for r in ws.revisions.values() if r.id != DESK.id)
    assert created.attrs["name"] == "12" and created.attrs["quantity"] == 3
    assert created.attrs["acquired_on"] == "2026-01-02" and created.attrs["extra"] == "x"
    assert ws.revisions[DESK.id].attrs["quantity"] == "many"  # left for datatype_mismatch


def test_many_attribute_text_becomes_a_list() -> None:
    cls = INV.classes["inv:Item"]
    tags = replace(cls.attributes["name"], many=True)
    model = replace(
        INV,
        classes={
            **INV.classes,
            "inv:Item": replace(cls, attributes={**cls.attributes, "name": tags}),
        },
    )
    ws = run({"op": "update", "id": "ITEM-0001", "set_text": {"name": "x"}}, model=model)
    assert ws.revisions[DESK.id].attrs["name"] == ["x"]


def _topic(**attrs: Any) -> Any:
    return revision("topic", "nas:Topic", {"label": "Cost accounting", **attrs}, alias="TOPIC-0001")


def run_topic(topic: Any, *ops: dict[str, Any]) -> WorkingState:
    return run(*ops, base=DictSnapshot(topic), model=NAS, refs={"TOPIC-0001": topic.id})


def test_add_appends_missing_values_in_order_and_keeps_the_stored_order() -> None:
    topic = _topic(synonyms=["CA", "Costing"])
    ws = run_topic(
        topic,
        {"op": "update", "id": "TOPIC-0001", "add": {"synonyms": ["COA", "CA", "COA", "X"]}},
    )
    assert ws.revisions[topic.id].attrs["synonyms"] == ["CA", "Costing", "COA", "X"]
    assert ws.effects[0].names == ("synonyms",) and ws.effects[0].lists == ("synonyms",)
    assert not ws.findings


def test_add_to_an_absent_list_and_remove_everything_unsets_it() -> None:
    topic = _topic()
    ws = run_topic(
        topic,
        {"op": "update", "id": "TOPIC-0001", "add_text": {"misspellings": ["Costign"]}},
        {"op": "update", "id": "TOPIC-0001", "add": {"synonyms": ["COA"]}},
        {"op": "update", "id": "TOPIC-0001", "remove_text": {"misspellings": ["Costign", "y"]}},
    )
    assert ws.revisions[topic.id].attrs == {"label": "Cost accounting", "synonyms": ["COA"]}


def test_remove_drops_every_equal_value_and_ignores_absent_ones() -> None:
    topic = _topic(synonyms=["a", "b", "a", "c"])
    ws = run_topic(
        topic, {"op": "update", "id": "TOPIC-0001", "remove": {"synonyms": ["a", "zzz"]}}
    )
    assert ws.revisions[topic.id].attrs["synonyms"] == ["b", "c"]


def test_add_on_a_single_value_attribute_changes_nothing() -> None:
    topic = _topic()
    ws = run_topic(topic, {"op": "update", "id": "TOPIC-0001", "add": {"label": ["Other"]}})
    assert ws.revisions[topic.id].attrs["label"] == "Cost accounting"
    assert ws.effects[0].lists == ("label",)


def test_update_makes_the_next_revision() -> None:
    ws = run(
        {"op": "update", "id": "ITEM-0001", "set": {"condition": "worn"}, "unset": ["quantity"]},
        {"op": "update", "id": "ITEM-0001", "set": {"name": "Desk 2"}},
    )
    (updated,) = ws.revisions.values()
    assert updated.attrs == {"name": "Desk 2", "status": "in_stock", "condition": "worn"}
    assert (updated.rev, updated.prev_rev, updated.created_at) == (3, 2, AT)
    assert ws.effects[0].names == ("condition", "quantity")
    assert ws.resolution == {"locals": {}, "refs": {"ITEM-0001": str(DESK.id)}}


def test_link_and_unlink() -> None:
    ws = run(
        {"op": "link", "id": "ITEM-0001", "rel": "inv:located_in", "target": "LOC-0001"},
        {"op": "unlink", "id": "ITEM-0001", "edge_id": str(DESK.edges[0].edge_id)},
    )
    (updated,) = ws.revisions.values()
    assert [e.edge_id for e in updated.edges] == [ws.effects[0].edge_id]
    assert ws.effects[1].applied and ws.effects[1].edge_id == DESK.edges[0].edge_id


def test_unlink_needs_exactly_one_match() -> None:
    ws = run(
        {"op": "link", "id": "ITEM-0001", "rel": "inv:located_in", "target": "LOC-0001"},
        {"op": "unlink", "id": "ITEM-0001", "rel": "inv:located_in", "target": "LOC-0001"},
        {"op": "unlink", "id": "ITEM-0001", "rel": "inv:part_of", "target": "LOC-0001"},
    )
    assert not ws.effects[1].applied and "2 edges" in (ws.effects[1].problem or "")
    assert not ws.effects[2].applied and (ws.effects[2].problem or "").startswith("no ")
    assert not ws.findings  # the validator reports edge_missing


def test_transition_records_from_and_to() -> None:
    ws = run({"op": "transition", "id": "ITEM-0001", "to": "in_use"})
    effect = ws.effects[0]
    assert (effect.attr, effect.from_state, effect.to_state) == ("status", "in_stock", "in_use")
    assert ws.revisions[DESK.id].attrs["status"] == "in_use"
    ws = run({"op": "transition", "id": "LOC-0001", "to": "x"})
    assert not ws.effects[0].applied and "no lifecycle" in (ws.effects[0].problem or "")
    assert not ws.revisions
    ws = run({"op": "transition", "id": "ITEM-0001", "to": "x", "attr": "name"})
    assert "not a lifecycle attribute" in (ws.effects[0].problem or "")


def test_supersede_n_to_m_and_record_not_active() -> None:
    other = revision("other", "inv:Item", {"name": "O", "quantity": 1, "status": "ordered"})
    base = DictSnapshot(ROOM, DESK, other)
    refs = {"ITEM-0001": DESK.id, "LOC-0001": ROOM.id}
    ws = run(
        {"op": "create", "type": "inv:Item", "local": "new", "attrs": {"name": "N"}},
        {"op": "supersede", "by": "@new", "targets": ["ITEM-0001", str(other.id)]},
        {"op": "update", "id": "ITEM-0001", "set": {"name": "late"}},
        base=base,
        refs=refs,
    )
    new = ws.locals["new"]
    assert [(e.rel, e.target) for e in ws.revisions[new].edges] == [
        ("core:supersedes", DESK.id),
        ("core:supersedes", other.id),
    ]
    assert ws.revisions[DESK.id].record_status == "superseded"
    assert ws.revisions[other.id].record_status == "superseded"
    assert only_codes(ws) == ["data.temporal.record_not_active"]
    assert ws.findings[0].target == {
        "id": str(DESK.id),
        "type": "inv:Item",
        "path": "record_status",
    }
    assert ws.revisions[DESK.id].attrs["name"] == "Desk"  # the skipped update changed nothing


def test_retract_with_by_and_non_active_by() -> None:
    gone = revision(
        "gone", "inv:Item", {"name": "G", "quantity": 1, "status": "ordered"}, status="retracted"
    )
    ws = run(
        {"op": "retract", "id": "ITEM-0001", "by": str(gone.id)},
        base=DictSnapshot(ROOM, DESK, gone),
    )
    assert only_codes(ws) == ["data.temporal.record_not_active"] and not ws.revisions
    ws = run(
        {"op": "create", "type": "inv:Item", "local": "fix", "attrs": {"name": "F"}},
        {"op": "retract", "id": "ITEM-0001", "by": "@fix"},
    )
    fix = ws.locals["fix"]
    assert ws.revisions[DESK.id].record_status == "retracted"
    assert [(e.rel, e.target) for e in ws.revisions[fix].edges] == [("core:retracts", DESK.id)]


def test_reserved_relations_are_skipped() -> None:
    ws = run(
        {"op": "link", "id": "ITEM-0001", "rel": "core:supersedes", "target": "LOC-0001"},
        {
            "op": "create",
            "type": "inv:Item",
            "local": "a",
            "attrs": {"name": "A"},
            "edges": [{"rel": "core:retracts", "target": "ITEM-0001"}],
        },
    )
    assert only_codes(ws) == ["data.temporal.reserved_relation"] * 2
    assert ws.findings[0].target["path"] == "edges.core:supersedes"
    assert ws.findings[1].target["local"] == "a"
    assert DESK.id not in ws.revisions


def test_unlink_of_a_reserved_edge_is_refused() -> None:
    old = revision("old", "inv:Item", {"name": "O"}, [edge("core:supersedes", "desk", name="sup")])
    ws = run(
        {"op": "unlink", "id": str(old.id), "rel": "core:supersedes", "target": "ITEM-0001"},
        base=DictSnapshot(ROOM, DESK, old),
    )
    assert only_codes(ws) == ["data.temporal.reserved_relation"]


def test_derived_from_gets_the_post_state_revision() -> None:
    src = revision("src", "nas:Note", {"title": "S"}, rev=4)
    ws = run(
        {
            "op": "create",
            "type": "nas:Note",
            "local": "n",
            "attrs": {"title": "N"},
            "edges": [
                {"rel": "core:derived_from", "target": str(src.id)},
                {"rel": "core:derived_from", "target": "@m"},
                {"rel": "core:derived_from", "target": str(src.id), "target_rev": 2},
            ],
        },
        {"op": "create", "type": "nas:Note", "local": "m", "attrs": {"title": "M"}},
        {"op": "update", "id": str(src.id), "set": {"title": "S2"}},
        base=DictSnapshot(src),
        model=NAS,
        refs={},
    )
    revs = [e.target_rev for e in ws.revisions[ws.locals["n"]].edges]
    assert revs == [5, 1, 2]


def test_aliases_rename_advance_and_one_allocation_per_entity() -> None:
    ws = run(
        {"op": "set_alias", "id": "ITEM-0001", "alias": "ITEM-0007"},
        {"op": "create", "type": "inv:Item", "local": "a", "attrs": {"name": "A"}},
        {"op": "set_alias", "id": "@a", "alias": "SPARE-0001"},
    )
    a = ws.locals["a"]
    assert ws.revisions[DESK.id].alias == "ITEM-0007"
    assert ws.revisions[a].alias == "SPARE-0001"
    assert dict(ws.alias_allocations) == {DESK.id: "ITEM-0007", a: "SPARE-0001"}
    ws = run({"op": "set_alias", "id": "ITEM-0001", "alias": "ITEM-0001"})
    assert ws.alias_allocations == ()


def test_invalid_interval() -> None:
    ws = run(
        {
            "op": "create",
            "type": "inv:Item",
            "local": "a",
            "attrs": {"name": "A"},
            "valid_from": "2026-03-01",
            "valid_to": "2026-02-28T23:00:00Z",
        }
    )
    assert only_codes(ws) == ["data.temporal.invalid_interval"]
    assert ws.findings[0].target["path"] == "valid_from"
    ws = run(
        {
            "op": "create",
            "type": "inv:Item",
            "attrs": {"name": "A"},
            "valid_from": "2026-03-01",
            "valid_to": "2026-03-01T00:00:00Z",
        }
    )
    assert not ws.findings


def test_bindings_from_earlier_documents() -> None:
    ws = run(
        {"op": "update", "id": "@shelf", "set": {"name": "x"}},
        refs={"@shelf": ROOM.id},
    )
    assert ws.revisions[ROOM.id].attrs["name"] == "x"
    assert ws.resolution["refs"] == {"@shelf": str(ROOM.id)}
    assert ws.local_of(ROOM.id) == "shelf"


def test_working_state_reads_overlay_the_base() -> None:
    ws = run(
        {
            "op": "create",
            "type": "inv:Item",
            "local": "a",
            "attrs": {"name": "A"},
            "edges": [{"rel": "inv:located_in", "target": "LOC-0001"}],
        },
        {"op": "unlink", "id": "ITEM-0001", "rel": "inv:located_in", "target": "LOC-0001"},
        {"op": "set_alias", "id": "ITEM-0001", "alias": "ITEM-0009"},
    )
    a = ws.locals["a"]
    assert ws.get(a) is ws.revisions[a] and ws.get(ROOM.id) == ROOM
    assert [source for source, _ in ws.incoming(ROOM.id)] == [a]
    assert ws.resolve_alias("ITEM-0009") == (DESK.id, True)
    assert ws.resolve_alias("ITEM-0001") == (DESK.id, False)  # renamed, still bound
    assert ws.resolve_alias("ITEM-0002") == (a, True)
    page = ws.query(Query(types=(TermRef("inv:Item"),), where=Cmp("name", "eq", "A")))
    assert [r.id for r in page.items] == [a]
    assert [m.rev for m in ws.history(DESK.id)] == [2, 3]
    assert ws.get_revision(DESK.id, 3) is ws.revisions[DESK.id]
    assert ws.match_suffix(a.hex[-8:]) == [a]
    assert ws.seq == 1 and ws.touched == {a, DESK.id}


def test_normalize_is_deterministic() -> None:
    ops = (
        {"op": "create", "type": "inv:Item", "local": "a", "attrs": {"name": "A"}},
        {"op": "link", "id": "ITEM-0001", "rel": "inv:part_of", "target": "@a"},
    )
    first, second = run(*ops), run(*ops)
    assert first.revisions == second.revisions and first.effects == second.effects
    assert first.alias_allocations == second.alias_allocations
    assert eid("room") == ROOM.id
