"""The reconcile planner (reconcile plan R-K4 to R-K9, R-K13) on the real write service.

Every case that writes plans, expands, applies the ops through the write service and then plans
the same document again: on the post-state the plan must be empty with every item unchanged
(idempotence), which also proves the planner judges "unchanged" by the rules a write applies.
"""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from support.reconcile_toy import ID, MODEL, REQUEST, World, doc, row, rows, scope

from graph_cli.kernel.changeset import (
    MAX_RECONCILE_OPS,
    CreateOp,
    LinkOp,
    RetractOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    parse_changeset,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, LocalRef
from graph_cli.kernel.records import SourceRef
from graph_cli.services.write import WriteRequest
from graph_cli.sources import reconcile as planner
from graph_cli.sources.reconcile import (
    MAX_LISTED_KEYS,
    ReconcilePlan,
    default_reason,
    expanded,
    plan_reconcile,
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def summary(plan: ReconcilePlan, index: int = 0) -> dict[str, int]:
    scope_summary = plan.scopes[index]
    return {
        name: getattr(scope_summary, name)
        for name in (
            "created",
            "updated",
            "unchanged",
            "moved",
            "adopted",
            "kept",
            "retired",
            "revived",
        )
    }


def only(**counts: int) -> dict[str, int]:
    base = dict.fromkeys(
        ("created", "updated", "unchanged", "moved", "adopted", "kept", "retired", "revived"), 0
    )
    return {**base, **counts}


# ---- create, update, unchanged, retire ---------------------------------------------------------


def test_one_run_creates_updates_leaves_and_retires(world: World) -> None:
    first, result = world.run(doc(scope(complete=True, items=rows("curl", "jq", "zlib"))))
    assert summary(first) == only(created=3)
    assert result is not None and result.seq == 1 and len(result.entities) == 3
    assert [type(op) for op in first.ops] == [CreateOp] * 3
    assert world.keys() == ["inventory/host_a/curl", "inventory/host_a/jq", "inventory/host_a/zlib"]
    stored = world.find("inventory/host_a/jq")
    assert stored is not None
    assert stored.attrs["package"] == "jq" and stored.attrs["state"] == "present"

    second, result = world.run(
        doc(
            scope(
                complete=True,
                items=[row("host_a/curl", "9"), *rows("jq", "bash")],
            )
        )
    )
    assert summary(second) == only(created=1, updated=1, unchanged=1, retired=1)
    assert second.scopes[0].retired_keys == ("host_a/zlib",)
    assert result is not None and result.seq == 2
    assert world.find("inventory/host_a/zlib") is None
    curl = world.find("inventory/host_a/curl")
    assert curl is not None and curl.attrs["version"] == "9"


def test_a_second_identical_run_writes_nothing(world: World) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq")))
    world.run(document)
    plan, result = world.run(document)
    assert plan.ops == () and result is None
    assert summary(plan) == only(unchanged=2)
    assert world.env.head_seq() == 1


def test_a_field_an_item_leaves_out_is_unset_and_other_attributes_stay(world: World) -> None:
    world.run(doc(scope(items=[row("host_a/curl", "1")])))
    entity = world.find("inventory/host_a/curl")
    assert entity is not None
    world.plain({"op": "update", "id": str(entity.id), "set": {"count": 7}})  # not a field
    plan, _ = world.run(doc(scope(items=[{"key": "host_a/curl", "attrs": {"package": "curl"}}])))
    assert summary(plan) == only(updated=1)
    (op,) = plan.ops
    assert isinstance(op, UpdateOp) and op.unset == ("version",) and op.set == {}
    after = world.find("inventory/host_a/curl")
    assert after is not None
    assert "version" not in after.attrs and after.attrs["count"] == 7


def test_a_partial_scope_never_retires(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq"))))
    plan, _ = world.run(doc(scope(items=rows("curl"))))
    assert summary(plan) == only(unchanged=1) and plan.ops == ()
    assert world.keys() == ["inventory/host_a/curl", "inventory/host_a/jq"]


def test_only_the_prefix_of_the_scope_is_its_own(world: World) -> None:
    world.run(doc(scope(under="host_a/", items=rows("curl", "jq"))))
    world.run(doc(scope(under="host_b/", items=rows("curl", prefix="host_b/"))))
    plan, _ = world.run(
        doc(
            scope(under="host_a/", complete=True, items=rows("curl")),
            scope(under="host_b/", items=[]),
        )
    )
    assert summary(plan, 0) == only(unchanged=1, retired=1)
    assert world.find("inventory/host_b/curl") is not None


def test_another_source_is_never_reached(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq"))))
    other = doc(scope(complete=True, items=rows("curl")), source={"name": "other_source"})
    plan, _ = world.run(other)
    assert summary(plan) == only(created=1)  # its own key space: no entity of inventory is retired
    assert world.find("inventory/host_a/jq") is not None
    assert world.find("other_source/host_a/curl") is not None


# ---- unchanged means unchanged -----------------------------------------------------------------


def test_restating_the_stored_values_emits_nothing(world: World) -> None:
    fields = ["package", "count", "ratio", "title", "tags"]
    document = doc(
        scope(
            fields=fields,
            items=[
                {
                    "key": "a/x",
                    "attrs": {"package": "x", "tags": ["b", "a"]},
                    "attrs_text": {"count": "3", "ratio": "1.50", "title": "Hello"},
                }
            ],
        )
    )
    world.run(document)
    stored = world.find("inventory/a/x")
    assert stored is not None
    assert stored.attrs["count"] == 3 and stored.attrs["title"] == {"lang": "en", "value": "Hello"}
    again = world.plan(document)
    assert again.ops == () and summary(again) == only(unchanged=1)
    # the same values in other forms the write path reads alike
    same = doc(
        scope(
            fields=fields,
            items=[
                {
                    "key": "a/x",
                    "attrs": {
                        "package": "x",
                        "tags": ["b", "a"],
                        "count": 3,
                        "ratio": "1.50",
                        "title": {"lang": "en", "value": "Hello"},
                    },
                }
            ],
        )
    )
    assert world.plan(same).ops == ()
    tagged = doc(
        scope(
            fields=fields,
            items=[
                {
                    "key": "a/x",
                    "attrs": {"package": "x", "tags": ["b", "a"]},
                    "attrs_text": {"count": "3", "ratio": "1.50", "title": "Hello@en"},
                }
            ],
        )
    )
    assert world.plan(tagged).ops == ()


@pytest.mark.parametrize(
    ("attrs", "texts"),
    [
        ({"tags": ["a", "b"]}, {}),  # the order of a list is part of the value
        ({}, {"count": "4"}),
        ({}, {"ratio": "1.5"}),  # text of a decimal is kept as written
        ({}, {"title": "Hello@de"}),  # another language is another value
        ({}, {"title": "Hallo"}),
    ],
)
def test_a_changed_value_is_one_update(
    world: World, attrs: dict[str, Any], texts: dict[str, str]
) -> None:
    fields = ["package", "count", "ratio", "title", "tags"]
    base = {
        "key": "a/x",
        "attrs": {"package": "x", "tags": ["b", "a"]},
        "attrs_text": {"count": "3", "ratio": "1.50", "title": "Hello"},
    }
    world.run(doc(scope(fields=fields, items=[base])))
    changed = {
        "key": "a/x",
        "attrs": {**base["attrs"], **attrs},
        "attrs_text": {**base["attrs_text"], **texts},
    }
    plan, _ = world.run(doc(scope(fields=fields, items=[changed])))
    assert summary(plan) == only(updated=1)
    (op,) = plan.ops
    assert isinstance(op, UpdateOp)
    assert set(op.set) | set(op.set_text) == set(attrs) | set(texts)


def test_keys_are_compared_exactly_as_the_unique_check_does(world: World) -> None:
    composed, decomposed = "a/caf\u00e9", "a/cafe\u0301"
    world.run(doc(scope(under="a/", fields=[], items=[{"key": composed}, {"key": decomposed}])))
    assert len(world.keys()) == 2  # two keys: nothing folds them together
    assert world.find(f"inventory/{decomposed}") is not None


def test_a_text_that_does_not_coerce_is_left_to_the_validator(world: World) -> None:
    plan = world.plan(
        doc(scope(fields=["count"], items=[{"key": "a/x", "attrs_text": {"count": "many"}}]))
    )
    (op,) = plan.ops
    assert isinstance(op, CreateOp) and op.attrs_text == {"count": "many"}


# ---- guards --------------------------------------------------------------------------------------


def test_an_empty_complete_scope_is_refused_unless_allowed(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq"))))
    error = world.refused(doc(scope(complete=True)))
    assert error.code == "reconcile.source_empty"
    assert error.details["field"] == "allow_empty" and error.details["current"] == 2
    plan, _ = world.run(doc(scope(complete=True, allow_empty=True)))
    assert summary(plan) == only(retired=2)
    assert world.keys() == []


def test_an_empty_complete_scope_over_nothing_is_fine(world: World) -> None:
    plan, result = world.run(doc(scope(complete=True)))
    assert plan.ops == () and result is None


def test_an_empty_partial_scope_is_fine(world: World) -> None:
    world.run(doc(scope(items=rows("curl"))))
    assert world.plan(doc(scope())).ops == ()


def test_the_retire_limit_and_its_override(world: World) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    world.run(doc(scope(items=rows(*names))))
    document = doc(scope(complete=True, items=rows(*names[:14])))  # 6 of 20 go
    error = world.refused(document)
    assert error.code == "reconcile.retire_limit"
    assert (error.details["count"], error.details["limit"]) == (6, 5)
    assert error.details["field"] == "allow_retire" and error.details["value"] == 6
    assert error.details["keys"] == [f"host_a/{n}" for n in names[14:]]
    assert "allow_retire" in error.message and "6" in error.message
    still = doc(scope(complete=True, allow_retire=5, items=rows(*names[:14])))
    assert world.refused(still).code == "reconcile.retire_limit"
    plan, _ = world.run(doc(scope(complete=True, allow_retire=6, items=rows(*names[:14]))))
    assert summary(plan) == only(unchanged=14, retired=6)


def test_the_limit_is_a_tenth_of_the_scope_above_fifty(world: World) -> None:
    names = [f"p{n:03d}" for n in range(100)]
    world.run(doc(scope(items=rows(*names))))
    ok, _ = world.run(doc(scope(complete=True, items=rows(*names[:90]))))  # 10 of 100: not above
    assert summary(ok)["retired"] == 10
    error = world.refused(doc(scope(complete=True, items=rows(*names[:70]))))  # 20 of 90
    assert (error.details["count"], error.details["limit"]) == (20, 9)


def test_the_refusal_lists_at_most_twenty_keys(world: World) -> None:
    names = [f"p{n:03d}" for n in range(60)]
    world.run(doc(scope(items=rows(*names))))
    error = world.refused(doc(scope(complete=True, items=rows(*names[:10]))))
    assert len(error.details["keys"]) == MAX_LISTED_KEYS  # type: ignore[arg-type]
    plan = world.plan(doc(scope(complete=True, allow_retire=50, items=rows(*names[:10]))))
    assert len(plan.scopes[0].retired_keys) == 50
    block = plan.to_json(SourceRef("inventory"))["scopes"][0]  # type: ignore[index]
    assert len(block["retired_keys"]) == MAX_LISTED_KEYS
    assert block["retired"] == 50
    full = plan.scopes[0].to_json(max_keys=None)
    assert len(full["retired_keys"]) == 50  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expect", "count", "ok"),
    [
        ({"min_items": 3}, 2, False),
        ({"min_items": 2}, 2, True),
        ({"max_items": 2}, 3, False),
        ({"max_items": 3}, 3, True),
        ({"min_items": 2, "max_items": 3}, 4, False),
    ],
)
def test_expected_counts(world: World, expect: dict[str, int], count: int, ok: bool) -> None:
    names = [f"p{n}" for n in range(count)]
    document = doc(scope(expect=expect, items=rows(*names)))
    if ok:
        world.plan(document)
        return
    error = world.refused(document)
    assert error.code == "reconcile.shape" and error.details["items"] == count


@pytest.mark.parametrize(
    ("type_curie", "key", "word"),
    [
        ("ex:Installation", "no_such_attribute", "no attribute"),
        ("ex:Loose", "source_key", "not unique"),
        ("ex:Serial", "source_key", "not plain text"),
        ("ex:Installation", "title", "not unique"),
        ("ex:Installation", "tags", "not unique"),
    ],
)
def test_the_key_attribute_must_be_a_unique_single_text(
    world: World, type_curie: str, key: str, word: str
) -> None:
    error = world.refused(doc({"type": type_curie, "key": key, "items": []}))
    assert error.code == "reconcile.key_attribute" and word in error.message
    assert error.details["path"] == "reconcile[0].key"


def test_a_key_that_moves_must_not_be_immutable_but_adoption_may(world: World) -> None:
    items = [{"key": "a/x"}, {"key": "a/y"}]
    pinned = {"type": "ex:Pinned", "key": "source_key", "items": items}
    error = world.refused(doc({**pinned, "moves": [{"from": "a/old", "to": "a/y"}]}))
    assert error.code == "reconcile.key_attribute" and "immutable" in error.message
    adopted = world.plain({"op": "create", "type": "ex:Pinned", "local": "p"})
    plan, _ = world.run(doc({**pinned, "moves": [{"id": str(adopted["p"]), "to": "a/y"}]}))
    assert summary(plan) == only(created=1, adopted=1)


def test_an_unknown_type_or_relation_is_an_unknown_term(world: World) -> None:
    error = world.refused(doc(scope(type="ex:Nothing")))
    assert (error.code, error.details["kind"]) == ("term.unknown", "type")
    error = world.refused(doc(scope(edges=["ex:nowhere"])))
    assert (error.code, error.details["kind"]) == ("term.unknown", "relation")


def test_scopes_that_cover_the_same_entities_overlap(world: World) -> None:
    for first, second in [
        (scope(under="a/"), scope(under="a/b/")),  # one prefix inside the other
        (scope(under=""), scope(under="a/")),
        (scope(under="a/"), scope(under="a/", fields=["version"])),
    ]:
        error = world.refused(doc(first, second))
        assert error.code == "reconcile.scope_overlap" and error.details["path"] == "reconcile[1]"
    # disjoint prefixes of one type, and unrelated types, do not
    world.plan(doc(scope(under="a/"), scope(under="b/")))
    world.plan(
        doc(
            scope(under="a/"),
            {"type": "ex:Plain", "key": "source_key", "under": "a/", "items": []},
        )
    )


def test_sibling_types_that_share_a_key_namespace_overlap_by_prefix(world: World) -> None:
    tool = {"type": "ex:Tool", "key": "source_key", "items": []}
    gadget = {"type": "ex:Gadget", "key": "source_key", "items": []}
    assert world.refused(doc({**tool, "under": "a/"}, {**gadget, "under": "a/"})).code == (
        "reconcile.scope_overlap"
    )
    world.plan(doc({**tool, "under": "a/"}, {**gadget, "under": "b/"}))


def test_a_scope_over_a_parent_and_a_child_type_overlap(world: World) -> None:
    tool = {"type": "ex:Tool", "key": "source_key", "under": "a/", "items": []}
    item = {"type": "ex:Item", "key": "source_key", "under": "a/", "items": []}
    assert world.refused(doc(item, tool)).code == "reconcile.scope_overlap"


def test_own_ops_must_not_touch_an_entity_of_a_scope(world: World) -> None:
    world.run(doc(scope(items=rows("curl"))))
    entity = world.find("inventory/host_a/curl")
    assert entity is not None
    target = str(entity.id)
    alias_free = {"op": "update", "id": target, "set": {"count": 1}}
    for op in (
        alias_free,
        {"op": "retract", "id": target},
        {"op": "link", "id": target, "rel": "ex:installed_on", "target": ID},
        {"op": "transition", "id": target, "to": "removed"},
        {"op": "supersede", "by": target, "targets": [ID]},
        {"op": "supersede", "by": ID, "targets": [target]},
    ):
        error = world.refused(doc(scope(items=rows("curl")), ops=[op]))
        assert error.code == "reconcile.scope_conflict"
        assert error.details["path"] == "ops[0]" and error.details["scope"] == "reconcile[0]"


def test_own_ops_must_not_create_under_the_keys_of_a_scope(world: World) -> None:
    create = {"op": "create", "type": "ex:Installation"}
    for attrs in (
        {"attrs": {"source_key": "inventory/host_a/x"}},
        {"attrs_text": {"source_key": "inventory/host_a/x"}},
    ):
        error = world.refused(doc(scope(under="host_a/"), ops=[{**create, **attrs}]))
        assert error.code == "reconcile.scope_conflict"
    # another prefix, another source, another key attribute, an entity outside the family
    world.plan(
        doc(scope(under="host_a/"), ops=[{**create, "attrs": {"source_key": "inventory/b"}}])
    )
    world.plan(doc(scope(under="host_a/"), ops=[{**create, "attrs": {"source_key": "x/host_a/x"}}]))
    world.plan(doc(scope(under="host_a/"), ops=[{**create, "attrs": {"package": "inventory/"}}]))


def test_own_ops_that_touch_other_entities_are_fine_and_run_first(world: World) -> None:
    ids = world.plain({"op": "create", "type": "ex:Package", "local": "p", "attrs": {"name": "jq"}})
    document = doc(
        scope(
            edges=["ex:installation_of"],
            items=[
                {
                    "key": "host_a/jq",
                    "attrs": {"package": "jq", "version": "1"},
                    "edges": [{"rel": "ex:installation_of", "target": "@pkg_curl"}],
                }
            ],
        ),
        ops=[
            {"op": "create", "type": "ex:Package", "local": "pkg_curl", "attrs": {"name": "curl"}},
            {"op": "update", "id": str(ids["p"]), "set": {"name": "jq2"}},
        ],
    )
    plan = world.plan(document)
    assert [type(op) for op in plan.ops] == [CreateOp]  # the plan holds its own ops only
    (create,) = plan.ops
    assert isinstance(create, CreateOp) and create.edges[0].target == LocalRef("pkg_curl")
    cs = parse_changeset(document)
    assert expanded(cs, plan).ops[: len(cs.ops)] == cs.ops
    result = world.env.service.apply(
        WriteRequest(world.env.actor, world.env.name, expanded(cs, plan), False, REQUEST)
    )
    assert result.seq == 2


def test_a_type_outside_the_scope_that_holds_a_key_is_refused(world: World) -> None:
    world.run(doc({"type": "ex:Tool", "key": "source_key", "items": [{"key": "a/x"}]}))
    plain = {"type": "ex:Plain", "key": "source_key", "items": [{"key": "a/x"}]}
    gadgets = {"type": "ex:Gadget", "key": "source_key", "items": [{"key": "a/x"}]}
    assert world.refused(doc(gadgets)).code == "reconcile.type_mismatch"
    # Plain does not share the key attribute's declaring class: its own namespace
    world.plan(doc(plain))


def test_an_item_cannot_change_the_type_of_its_entity(world: World) -> None:
    world.run(
        doc({"type": "ex:Item", "key": "source_key", "items": [{"key": "a/x", "type": "ex:Tool"}]})
    )
    error = world.refused(
        doc(
            {"type": "ex:Item", "key": "source_key", "items": [{"key": "a/x", "type": "ex:Gadget"}]}
        )
    )
    assert error.code == "reconcile.type_mismatch"
    assert error.details["path"] == "reconcile[0].items[0]"
    ok = doc({"type": "ex:Item", "key": "source_key", "items": [{"key": "a/x", "type": "ex:Tool"}]})
    assert world.plan(ok).ops == ()
    # no type named: the entity of the family is matched as it is
    assert (
        world.plan(doc({"type": "ex:Item", "key": "source_key", "items": [{"key": "a/x"}]})).ops
        == ()
    )


@pytest.mark.parametrize(
    "item",
    [
        {"key": "a/x"},  # an abstract scope needs the type
        {"key": "a/x", "type": "ex:Item"},  # an abstract type cannot be created
        {"key": "a/x", "type": "ex:Plain"},  # not in the family
    ],
)
def test_a_new_item_needs_a_concrete_type_of_its_scope(world: World, item: dict[str, Any]) -> None:
    error = world.refused(doc({"type": "ex:Item", "key": "source_key", "items": [item]}))
    assert error.code == "reconcile.type_mismatch"


def test_an_abstract_scope_creates_items_of_their_concrete_types(world: World) -> None:
    items = [{"key": "a/x", "type": "ex:Tool"}, {"key": "a/y", "type": "ex:Gadget"}]
    plan, _ = world.run(
        doc({"type": "ex:Item", "key": "source_key", "complete": True, "items": items})
    )
    assert summary(plan) == only(created=2)
    assert world.find("inventory/a/x", type_curie="ex:Tool") is not None
    plan, _ = world.run(
        doc({"type": "ex:Item", "key": "source_key", "complete": True, "items": items[:1]})
    )
    assert summary(plan) == only(unchanged=1, retired=1)


def test_an_item_type_unknown_to_the_model_is_an_unknown_term(world: World) -> None:
    error = world.refused(
        doc({"type": "ex:Item", "key": "source_key", "items": [{"key": "a/x", "type": "ex:Nope"}]})
    )
    assert error.code == "term.unknown"


# ---- keep ------------------------------------------------------------------------------------


def test_kept_entities_are_neither_updated_nor_retired(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq", "zlib"))))
    document = doc(
        scope(complete=True, keep=["host_a/zlib", "host_a/gone"], items=[row("host_a/curl", "2")])
    )
    plan = world.plan(document)
    assert summary(plan) == only(updated=1, retired=1, kept=1)  # jq goes, zlib stays
    assert plan.scopes[0].retired_keys == ("host_a/jq",)
    world.run(document)
    assert world.find("inventory/host_a/zlib") is not None
    assert world.find("inventory/host_a/jq") is None


def test_keeping_everything_retires_nothing_even_without_items(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq"))))
    plan, result = world.run(doc(scope(complete=True, keep=["host_a/curl", "host_a/jq"])))
    assert plan.ops == () and result is None and plan.scopes[0].kept == 2


# ---- moves and adoption --------------------------------------------------------------------------


def test_a_move_keeps_the_identity_and_a_rerun_is_a_no_op(world: World) -> None:
    world.run(doc(scope(items=[row("host_a/old_name", "1")])))
    before = world.find("inventory/host_a/old_name")
    assert before is not None
    document = doc(
        scope(
            complete=True,
            moves=[{"from": "host_a/old_name", "to": "host_a/new_name"}],
            items=[{"key": "host_a/new_name", "attrs": {"package": "old_name", "version": "2"}}],
        )
    )
    plan, _ = world.run(document)
    assert summary(plan) == only(moved=1, updated=1)
    after = world.find("inventory/host_a/new_name")
    assert after is not None and after.id == before.id and after.rev == before.rev + 1
    assert world.find("inventory/host_a/old_name") is None
    assert [type(op) for op in plan.ops] == [UpdateOp, UpdateOp]  # the key first, then the fields
    again, result = world.run(document)
    assert again.ops == () and result is None and summary(again) == only(unchanged=1)


def test_a_moved_entity_without_other_changes_is_counted_as_moved_only(world: World) -> None:
    world.run(doc(scope(items=[row("host_a/old", "1")])))
    plan, _ = world.run(
        doc(
            scope(
                complete=True,
                moves=[{"from": "host_a/old", "to": "host_a/new"}],
                items=[{"key": "host_a/new", "attrs": {"package": "old", "version": "1"}}],
            )
        )
    )
    assert summary(plan) == only(moved=1)


@pytest.mark.parametrize(
    ("moves", "items", "what"),
    [
        ([{"from": "a/x", "to": "a/y"}], ["a/other"], "is not the key of an item"),
        ([{"from": "a/nope", "to": "a/y"}], ["a/y"], "neither key is held"),
        ([{"from": "a/x", "to": "a/z"}], ["a/z", "a/y"], "is held by another entity"),
        ([{"from": "a/x", "to": "a/y"}, {"from": "a/x", "to": "a/w"}], ["a/y", "a/w"], "two moves"),
        ([{"from": "a/x", "to": "a/y"}, {"from": "a/q", "to": "a/y"}], ["a/y"], "two moves"),
        ([{"from": "a/x", "to": "a/y"}, {"from": "a/y", "to": "a/w"}], ["a/y", "a/w"], "two moves"),
        ([{"from": "a/x", "to": "a/y"}], ["a/x", "a/y"], "still the key of an item"),
    ],
)
def test_move_problems_are_refused(
    world: World, moves: list[dict[str, str]], items: list[str], what: str
) -> None:
    world.run(doc(scope(under="a/", items=[{"key": k} for k in ("a/x", "a/z", "a/q")])))
    error = world.refused(doc(scope(under="a/", moves=moves, items=[{"key": k} for k in items])))
    assert error.code == "reconcile.move_invalid" and what in error.message
    assert str(error.details["path"]).startswith("reconcile[0].moves[")


def test_a_move_of_an_entity_outside_the_scope_is_a_type_mismatch(world: World) -> None:
    world.run(doc({"type": "ex:Tool", "key": "source_key", "items": [{"key": "a/x"}]}))
    error = world.refused(
        doc(
            {
                "type": "ex:Gadget",
                "key": "source_key",
                "moves": [{"from": "a/x", "to": "a/y"}],
                "items": [{"key": "a/y"}],
            }
        )
    )
    assert error.code == "reconcile.type_mismatch"


def test_adoption_gives_an_unkeyed_entity_its_key_without_a_duplicate(world: World) -> None:
    ids = world.plain(
        {
            "op": "create",
            "type": "ex:Installation",
            "local": "a",
            "attrs": {"package": "curl", "version": "1"},
        },
        {
            "op": "create",
            "type": "ex:Installation",
            "local": "b",
            "attrs": {"package": "jq", "version": "1"},
        },
    )
    document = doc(
        scope(
            complete=True,
            moves=[
                {"id": str(ids["a"]), "to": "host_a/curl"},
                {"id": str(ids["b"]), "to": "host_a/jq"},
            ],
            items=rows("curl", "jq"),
        )
    )
    plan, _ = world.run(document)
    assert summary(plan) == only(adopted=2)  # a key update is counted once, as moved is
    adopted = world.find("inventory/host_a/curl")
    assert adopted is not None and adopted.id == ids["a"]
    assert world.count("ex:Installation") == 2
    again, result = world.run(document)  # the entities hold the keys now
    assert again.ops == () and result is None


def test_adoption_problems_are_refused(world: World) -> None:
    ids = world.plain(
        {"op": "create", "type": "ex:Installation", "local": "a", "attrs": {"package": "curl"}},
        {
            "op": "create",
            "type": "ex:Installation",
            "local": "k",
            "attrs": {"source_key": "inventory/a/k"},
        },
        {"op": "create", "type": "ex:Package", "local": "p", "attrs": {"name": "jq"}},
        {
            "op": "create",
            "type": "ex:Installation",
            "local": "z",
            "attrs": {"source_key": "inventory/a/z"},
        },
    )
    a, k, p = (str(ids[n]) for n in "akp")
    cases = {
        "names no active entity": {"id": ID, "to": "a/x"},
        "already holds the key": {"id": k, "to": "a/x"},
        "is held by another entity": {"id": a, "to": "a/z"},
        "two moves": None,
    }
    for message, move in cases.items():
        if move is None:
            moves = [{"id": a, "to": "a/x"}, {"id": a, "to": "a/y"}]
            items = [{"key": "a/x"}, {"key": "a/y"}]
        else:
            moves, items = [move], [{"key": move["to"]}]
        error = world.refused(doc(scope(under="a/", moves=moves, items=items)))
        assert error.code == "reconcile.move_invalid" and message in error.message, message
    error = world.refused(
        doc(scope(under="a/", moves=[{"id": p, "to": "a/x"}], items=[{"key": "a/x"}]))
    )
    assert error.code == "reconcile.type_mismatch"
    error = world.refused(
        doc(scope(under="a/", moves=[{"id": "@gone", "to": "a/x"}], items=[{"key": "a/x"}]))
    )
    assert error.code == "reconcile.move_invalid"


def test_an_adoption_may_name_an_entity_by_alias_or_binding(world: World) -> None:
    ids = world.plain({"op": "create", "type": "ex:Package", "local": "p", "attrs": {"name": "jq"}})
    alias = world.env.snapshot().get(ids["p"])
    assert alias is not None and alias.alias is not None
    ok = {"type": "ex:Plain", "key": "source_key", "under": "a/", "items": [{"key": "a/x"}]}
    assert world.refused(doc({**ok, "moves": [{"id": alias.alias, "to": "a/x"}]})).code == (
        "reconcile.type_mismatch"  # resolved, found, and of another type
    )
    planned = world.plain({"op": "create", "type": "ex:Plain", "local": "q"})
    plan = world.plan(
        doc({**ok, "moves": [{"id": "@earlier", "to": "a/x"}]}), {"earlier": planned["q"]}
    )
    assert summary(plan) == only(adopted=1)


# ---- retire by transition and revive ----------------------------------------------------------


def test_retire_by_transition_keeps_the_entity_and_its_key(world: World) -> None:
    retire = {"transition": "removed", "revive": "present"}
    world.run(doc(scope(retire=retire, complete=True, items=rows("curl", "jq"))))
    jq = world.find("inventory/host_a/jq")
    assert jq is not None
    plan, _ = world.run(doc(scope(retire=retire, complete=True, items=rows("curl"))))
    assert summary(plan) == only(unchanged=1, retired=1)
    (op,) = plan.ops
    assert isinstance(op, TransitionOp) and op.to == "removed"
    gone = world.find("inventory/host_a/jq")
    assert gone is not None and gone.id == jq.id and gone.attrs["state"] == "removed"
    # a retired entity is not retired twice, and does not count towards the threshold
    again, result = world.run(doc(scope(retire=retire, complete=True, items=rows("curl"))))
    assert again.ops == () and result is None
    # the item returns: found by its key, brought back, same identity
    back, _ = world.run(doc(scope(retire=retire, complete=True, items=rows("curl", "jq"))))
    assert summary(back) == only(unchanged=1, revived=1)
    revived = world.find("inventory/host_a/jq")
    assert revived is not None and revived.id == jq.id and revived.attrs["state"] == "present"


def test_without_revive_a_returning_item_stays_retired_but_is_updated(world: World) -> None:
    retire = {"transition": "removed"}
    world.run(doc(scope(retire=retire, complete=True, items=rows("curl", "jq"))))
    world.run(doc(scope(retire=retire, complete=True, items=rows("curl"))))
    plan, _ = world.run(
        doc(scope(retire=retire, complete=True, items=[*rows("curl"), row("host_a/jq", "5")]))
    )
    assert summary(plan) == only(unchanged=1, updated=1)
    stored = world.find("inventory/host_a/jq")
    assert stored is not None and stored.attrs["state"] == "removed"
    assert stored.attrs["version"] == "5"


def test_retired_entities_do_not_count_as_current_for_the_threshold(world: World) -> None:
    retire = {"transition": "removed"}
    names = [f"p{n:02d}" for n in range(20)]
    world.run(doc(scope(retire=retire, items=rows(*names))))
    world.run(doc(scope(retire=retire, complete=True, allow_retire=5, items=rows(*names[:15]))))
    # 15 are live: retiring 5 more is within max(5, 10%), as the 5 retired ones do not count
    plan, _ = world.run(doc(scope(retire=retire, complete=True, items=rows(*names[:10]))))
    assert summary(plan)["retired"] == 5


def test_a_retire_by_transition_names_a_lifecycle_the_type_has(world: World) -> None:
    plain = {"type": "ex:Plain", "key": "source_key", "items": [{"key": "a/x"}]}
    for retire, word in (
        ({"transition": "removed"}, "no lifecycle attribute"),
        ({"transition": "removed", "attr": "state"}, "no lifecycle attribute"),
    ):
        error = world.refused(doc({**plain, "retire": retire}))
        assert error.code == "changeset.malformed" and word in error.message
        assert error.details["path"] == "reconcile[0].retire"
    error = world.refused(doc(scope(retire={"transition": "vanished"})))
    assert error.code == "changeset.malformed" and "vanished" in error.message
    error = world.refused(doc(scope(retire={"transition": "removed", "revive": "alive"})))
    assert error.code == "changeset.malformed" and "alive" in error.message
    error = world.refused(doc(scope(retire={"transition": "removed", "attr": "version"})))
    assert error.code == "changeset.malformed"


def test_a_retire_by_retract_is_final_and_names_the_entity(world: World) -> None:
    world.run(doc(scope(items=rows("curl", "jq"))))
    jq = world.find("inventory/host_a/jq")
    assert jq is not None
    plan, _ = world.run(doc(scope(complete=True, items=rows("curl"))))
    (op,) = plan.ops
    assert isinstance(op, RetractOp) and op.id == jq.id
    # a key that reappears is a new entity (retract is terminal)
    plan, _ = world.run(doc(scope(complete=True, items=rows("curl", "jq"))))
    assert summary(plan) == only(created=1, unchanged=1)
    new = world.find("inventory/host_a/jq")
    assert new is not None and new.id != jq.id


# ---- edges -------------------------------------------------------------------------------------


def edges_scope(**extra: Any) -> dict[str, Any]:
    return scope(edges=["ex:installation_of", "ex:installed_on"], **extra)


def test_edges_are_diffed_by_relation_target_and_qualifiers(world: World) -> None:
    ids = world.plain(
        {"op": "create", "type": "ex:Package", "local": "p1", "attrs": {"name": "curl"}},
        {"op": "create", "type": "ex:Package", "local": "p2", "attrs": {"name": "jq"}},
        {"op": "create", "type": "ex:Host", "local": "h", "attrs": {"hostname": "host-a"}},
    )
    p1, p2, host = (str(ids[n]) for n in ("p1", "p2", "h"))

    def item(*edges: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": "host_a/curl",
            "attrs": {"package": "curl", "version": "1"},
            "edges": list(edges),
        }

    of = {"rel": "ex:installation_of"}
    on = {"rel": "ex:installed_on", "target": host}
    first, _ = world.run(doc(edges_scope(items=[item({**of, "target": p1}, on)])))
    assert summary(first) == only(created=1)
    entity = world.find("inventory/host_a/curl")
    assert entity is not None and {e.rel for e in entity.edges} == {
        "ex:installation_of",
        "ex:installed_on",
    }
    # another package: one unlink, one link; the host edge is untouched
    plan, _ = world.run(doc(edges_scope(items=[item({**of, "target": p2}, on)])))
    assert summary(plan) == only(updated=1)
    assert [type(op) for op in plan.ops] == [UnlinkOp, LinkOp]  # unlinks before links
    # a qualifier is part of the edge: the edge is replaced
    plan, _ = world.run(
        doc(edges_scope(items=[item({**of, "target": p2, "qualifiers": {"pinned": True}}, on)]))
    )
    assert [type(op) for op in plan.ops] == [UnlinkOp, LinkOp]
    # no edge of a relation of the scope: all of them go; relations outside edges are never read
    plan, _ = world.run(doc(edges_scope(items=[item(on)])))
    assert [type(op) for op in plan.ops] == [UnlinkOp]
    only_host = doc(scope(edges=["ex:installed_on"], items=[item(on)]))
    assert world.plan(only_host).ops == ()
    # one entity named twice, by id and by alias, is one edge
    alias = world.env.snapshot().get(ids["p2"])
    assert alias is not None and alias.alias is not None
    twice = doc(edges_scope(items=[item(on, {**of, "target": p2}, {**of, "target": alias.alias})]))
    (link,) = world.plan(twice).ops  # the package edge was removed above: one link, not two
    assert isinstance(link, LinkOp) and link.edge.target == ids["p2"]


def test_edge_targets_by_alias_are_the_entities_they_name(world: World) -> None:
    ids = world.plain(
        {"op": "create", "type": "ex:Package", "local": "p", "attrs": {"name": "curl"}}
    )
    stored = world.env.snapshot().get(ids["p"])
    assert stored is not None and stored.alias is not None
    item = {
        "key": "host_a/curl",
        "attrs": {"package": "curl", "version": "1"},
        "edges": [{"rel": "ex:installation_of", "target": stored.alias}],
    }
    plan, _ = world.run(doc(edges_scope(items=[item])))
    (op,) = plan.ops
    assert isinstance(op, CreateOp) and op.edges[0].target == ids["p"]  # the id, not the alias
    missing = {**item, "edges": [{"rel": "ex:installation_of", "target": "PKG-0099"}]}
    error = world.refused(doc(edges_scope(items=[missing])))
    assert error.code == "reconcile.target_not_found"
    assert error.details["path"] == "reconcile[0].items[0].edges[0].target"


def test_edge_targets_by_value_find_existing_entities(world: World) -> None:
    ids = world.plain(
        {"op": "create", "type": "ex:Host", "local": "h", "attrs": {"hostname": "host-a"}}
    )
    by_host = {"type": "ex:Host", "attr": "hostname", "value": "host-a"}
    item = {
        "key": "host_a/jq",
        "attrs": {"package": "jq", "version": "1"},
        "edges": [{"rel": "ex:installed_on", "target": by_host}],
    }
    document = doc(scope(edges=["ex:installed_on"], items=[item]))
    plan, _ = world.run(document)  # idempotent: the edge exists, the value finds the same host
    (create,) = plan.ops
    assert isinstance(create, CreateOp) and create.edges[0].target == ids["h"]


def test_an_edge_target_by_value_may_be_an_item_of_the_document(world: World) -> None:
    by_key = {"type": "ex:Installation", "attr": "source_key", "value": "inventory/host_a/curl"}
    items = [
        {"key": "host_a/curl", "attrs": {"package": "curl", "version": "1"}},
        {
            "key": "host_a/jq",
            "attrs": {"package": "jq", "version": "1"},
            "edges": [{"rel": "ex:installed_on", "target": by_key}],
        },
    ]
    document = doc(scope(edges=["ex:installed_on"], items=items))
    # an installation as the host of an installation is no valid edge; the planner only resolves
    plan = world.plan(document)
    first, second = plan.ops
    assert isinstance(first, CreateOp) and isinstance(second, CreateOp)
    assert first.local is not None, "the item pointed at is declared under a minted local"
    assert second.edges[0].target == LocalRef(first.local)


def test_an_edge_target_by_value_is_the_entity_of_an_item_that_exists(world: World) -> None:
    world.run(doc(scope(items=[row("host_a/curl")])))
    existing = world.find("inventory/host_a/curl")
    assert existing is not None
    by_key = {"type": "ex:Installation", "attr": "source_key", "value": "inventory/host_a/curl"}
    items = [
        row("host_a/curl"),
        {
            "key": "host_a/jq",
            "attrs": {"package": "jq", "version": "1"},
            "edges": [{"rel": "ex:installed_on", "target": by_key}],
        },
    ]
    (create,) = world.plan(doc(scope(edges=["ex:installed_on"], items=items))).ops
    assert isinstance(create, CreateOp) and create.edges[0].target == existing.id
    assert create.local is None  # nothing points at a created entity


def test_an_item_that_is_pointed_at_gets_a_local_for_its_create(world: World) -> None:
    by_key = {"type": "ex:Installation", "attr": "source_key", "value": "inventory/host_a/second"}
    items = [
        {
            "key": "host_a/first",
            "attrs": {"package": "a"},
            "edges": [{"rel": "ex:installed_on", "target": by_key}],
        },
        {"key": "host_a/second", "attrs": {"package": "b"}},
        {"key": "host_a/third", "attrs": {"package": "c"}},
    ]
    plan = world.plan(doc(scope(edges=["ex:installed_on"], items=items)))
    first, second, third = plan.ops
    assert (
        isinstance(first, CreateOp) and isinstance(second, CreateOp) and isinstance(third, CreateOp)
    )
    assert second.local is not None and first.local is None and third.local is None
    assert first.edges[0].target == LocalRef(second.local)
    assert second.local.startswith("reconcile_")


def test_minted_locals_avoid_every_name_taken(world: World) -> None:
    by_key = {"type": "ex:Installation", "attr": "source_key", "value": "inventory/a/second"}
    items = [
        {"key": "a/first", "edges": [{"rel": "ex:installed_on", "target": by_key}]},
        {"key": "a/second"},
    ]
    taken = {"op": "create", "type": "ex:Package", "local": "reconcile_0_1", "attrs": {"name": "x"}}
    plan = world.plan(
        doc(scope(under="a/", fields=[], edges=["ex:installed_on"], items=items), ops=[taken])
    )
    second = plan.ops[1]
    assert isinstance(second, CreateOp) and second.local == "reconcile_0_1_"


def test_a_value_target_with_no_match_or_many_is_refused(world: World) -> None:
    def item(target: dict[str, Any]) -> dict[str, Any]:
        return {"key": "a/x", "edges": [{"rel": "ex:installed_on", "target": target}]}

    base = scope(under="a/", fields=[], edges=["ex:installed_on"])
    missing = {"type": "ex:Host", "attr": "hostname", "value": "nowhere"}
    error = world.refused(doc({**base, "items": [item(missing)]}))
    assert error.code == "reconcile.target_not_found" and "no active entity" in error.message
    not_unique = {"type": "ex:Installation", "attr": "package", "value": "x"}
    error = world.refused(doc({**base, "items": [item(not_unique)]}))
    assert error.code == "reconcile.target_not_found" and "not unique" in error.message
    unknown_attr = {"type": "ex:Host", "attr": "nothing", "value": "x"}
    assert world.refused(doc({**base, "items": [item(unknown_attr)]})).code == "term.unknown"
    unknown_type = {"type": "ex:Nothing", "attr": "hostname", "value": "x"}
    assert world.refused(doc({**base, "items": [item(unknown_type)]})).code == "term.unknown"


def test_edge_targets_by_local_use_ids_where_the_planner_knows_them(world: World) -> None:
    ids = world.plain(
        {"op": "create", "type": "ex:Host", "local": "h", "attrs": {"hostname": "host-a"}}
    )
    existing = {"key": "a/old", "attrs": {"package": "o"}, "local": "old"}
    world.run(doc(scope(under="a/", items=[{"key": "a/old", "attrs": {"package": "o"}}])))
    stored = world.find("inventory/a/old")
    assert stored is not None
    existing_id = stored.id
    items = [
        {**existing, "edges": [{"rel": "ex:installed_on", "target": "@earlier"}]},
        {
            "key": "a/new",
            "attrs": {"package": "n"},
            "local": "fresh",
            "edges": [{"rel": "ex:installed_on", "target": "@earlier"}],
        },
        {
            "key": "a/third",
            "attrs": {"package": "t"},
            "edges": [{"rel": "ex:installed_on", "target": "@fresh"}],
        },
    ]
    document = doc(scope(under="a/", edges=["ex:installed_on"], items=items))
    plan = world.plan(document, {"earlier": ids["h"]})
    new, third, link = plan.ops  # creates first, then the edges of the entity that exists
    assert isinstance(link, LinkOp) and link.edge.target == ids["h"]  # a binding is its id
    assert link.id == existing_id
    assert isinstance(new, CreateOp) and new.local == "fresh"
    assert new.edges[0].target == ids["h"]
    assert isinstance(third, CreateOp) and third.edges[0].target == LocalRef("fresh")
    # an unbound local is left for the resolver to refuse
    stray = doc(
        scope(
            under="a/",
            edges=["ex:installed_on"],
            items=[{"key": "a/z", "edges": [{"rel": "ex:installed_on", "target": "@nobody"}]}],
        )
    )
    (create,) = world.plan(stray).ops
    assert isinstance(create, CreateOp) and create.edges[0].target == LocalRef("nobody")


def test_locals_are_one_name_one_owner(world: World) -> None:
    items = [{"key": "a/x", "local": "dup"}, {"key": "a/y", "local": "dup"}]
    error = world.refused(doc(scope(under="a/", fields=[], items=items)))
    assert error.code == "changeset.local_ref_duplicate"
    assert error.details["path"] == "reconcile[0].items[1].local"
    own = {"op": "create", "type": "ex:Package", "local": "dup", "attrs": {"name": "x"}}
    error = world.refused(doc(scope(under="a/", fields=[], items=items[:1]), ops=[own]))
    assert error.code == "changeset.local_ref_duplicate"
    alone = doc(scope(under="a/", fields=[], items=items[:1]))
    assert world.plan(alone).ops  # no clash alone
    with pytest.raises(GraphCliError) as info:  # bound by an earlier document
        world.plan(alone, {"dup": EntityId(uuid4())})
    assert info.value.code == "changeset.local_ref_duplicate"


# ---- limits and shape of the result ------------------------------------------------------------


def test_a_document_expands_to_at_most_the_op_limit(world: World) -> None:
    def many(count: int) -> dict[str, Any]:
        return doc(scope(fields=[], under="a/", items=[{"key": f"a/{n}"} for n in range(count)]))

    assert len(world.plan(many(MAX_RECONCILE_OPS)).ops) == MAX_RECONCILE_OPS
    error = world.refused(many(MAX_RECONCILE_OPS + 1))
    assert error.code == "changeset.too_large"
    assert error.details["limit_ops"] == MAX_RECONCILE_OPS
    assert error.details["actual_ops"] == MAX_RECONCILE_OPS + 1


def test_the_frame_counts_against_the_op_limit(world: World) -> None:
    own = [{"op": "create", "type": "ex:Package", "attrs": {"name": f"p{n}"}} for n in range(3)]
    document = doc(
        scope(fields=[], under="a/", items=[{"key": f"a/{n}"} for n in range(3)]), ops=own
    )
    cs = parse_changeset(document)
    assert cs.source is not None
    plan_reconcile(
        cs.source, cs.reconcile, world.env.snapshot(), MODEL, frame_ops=cs.ops, max_ops=6
    )
    with pytest.raises(GraphCliError) as info:
        plan_reconcile(
            cs.source, cs.reconcile, world.env.snapshot(), MODEL, frame_ops=cs.ops, max_ops=5
        )
    assert info.value.code == "changeset.too_large" and info.value.details["actual_ops"] == 6


def test_thirty_two_scopes_are_planned(world: World) -> None:
    scopes = [
        scope(under=f"s{n:02d}/", items=[{"key": f"s{n:02d}/x", "attrs": {"package": "x"}}])
        for n in range(32)
    ]
    plan, _ = world.run(doc(*scopes))
    assert len(plan.scopes) == 32 and all(s.created == 1 for s in plan.scopes)


def test_a_large_scope_is_read_in_pages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"p{n:02d}" for n in range(23)]
    world.run(doc(scope(items=rows(*names))))
    monkeypatch.setattr(planner, "_PAGE", 4)
    plan = world.plan(doc(scope(complete=True, allow_retire=23, items=rows(*names[:2]))))
    assert summary(plan) == only(unchanged=2, retired=21)


def test_ops_come_in_phases_across_scopes(world: World) -> None:
    world.run(
        doc(
            scope(under="a/", complete=True, items=[row("a/old", "1"), row("a/gone", "1")]),
            scope(under="b/", items=[row("b/same", "1")]),
        )
    )
    plan = world.plan(
        doc(
            scope(
                under="a/",
                complete=True,
                allow_retire=1,
                moves=[{"from": "a/old", "to": "a/new"}],
                items=[row("a/new", "2"), row("a/fresh", "1")],
            ),
            scope(under="b/", items=[row("b/same", "2"), row("b/extra", "1")]),
        )
    )
    kinds = [type(op).__name__ for op in plan.ops]
    assert kinds == [
        "UpdateOp",  # the move (key)
        "CreateOp",  # a/fresh
        "CreateOp",  # b/extra
        "UpdateOp",  # a/new fields
        "UpdateOp",  # b/same fields
        "RetractOp",  # a/gone
    ]


def test_the_default_reason_and_the_expanded_change_set() -> None:
    assert default_reason(SourceRef("inventory", "collector 3", "run-1")) == (
        "reconcile inventory collector 3 run run-1"
    )
    assert default_reason(SourceRef("inventory")) == "reconcile inventory"
    assert default_reason(SourceRef("inventory", run="r")) == "reconcile inventory run r"
    cs = parse_changeset(doc(scope(items=rows("a")), reason="Inventory run", idempotency_key="k"))
    plan = ReconcilePlan((RetractOp(id=EntityId(uuid4())),), ())
    out = expanded(cs, plan)
    assert out.reason == "Inventory run" and out.idempotency_key == "k"
    assert out.reconcile == () and out.source == cs.source and len(out.ops) == 1
    no_reason = parse_changeset(doc(scope(items=rows("a"))))
    assert expanded(no_reason, plan).reason == "reconcile inventory collector 3 run run-1"
    assert expanded(no_reason, ReconcilePlan((), ())).reason is None  # nothing to write


def test_the_answer_block_names_source_and_scopes(world: World) -> None:
    plan = world.plan(doc(scope(under="host_a/", complete=True, items=rows("curl"))))
    assert plan.to_json(SourceRef("inventory", "v", "r")) == {
        "source": {"name": "inventory", "version": "v", "run": "r"},
        "scopes": [
            {
                "type": "ex:Installation",
                "under": "host_a/",
                "complete": True,
                "created": 1,
                "updated": 0,
                "unchanged": 0,
                "moved": 0,
                "adopted": 0,
                "kept": 0,
                "retired": 0,
                "revived": 0,
                "retired_keys": [],
            }
        ],
    }


def test_the_plan_reads_but_never_writes(world: World) -> None:
    world.run(doc(scope(items=rows("curl"))))
    seq = world.env.head_seq()
    world.plan(doc(scope(complete=True, items=rows("jq"))))
    assert world.env.head_seq() == seq
