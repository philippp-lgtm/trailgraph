"""Reference resolution (contracts B1.8, B1.9, B3.8, B3.9, B6.9; plan W6)."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.changeset import ChangeSet, EdgeSpec, LinkOp, UpdateOp, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import AbbrevRef, EntityId, TermRef
from graph_cli.services.refs import resolve_refs

ROOM = revision("room", "ex:Place", {"name": "R"}, alias="LOC-0002")
DESK = revision("desk", "ex:Thing", {"name": "D"}, alias="ITEM-0001")
SNAP = DictSnapshot(ROOM, DESK, former_aliases={"LOC-0001": ROOM.id})


def cs(*ops: dict[str, Any]) -> ChangeSet:
    return parse_changeset({"ops": list(ops)})


def error(changeset: ChangeSet, **kw: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        resolve_refs(changeset, SNAP, **kw)
    return info.value


def test_aliases_ids_and_locals_resolve_with_pairs() -> None:
    result = resolve_refs(
        cs(
            {
                "op": "create",
                "type": "ex:Thing",
                "local": "a",
                "edges": [{"rel": "ex:in", "target": "LOC-0002"}],
            },
            {"op": "update", "id": "ITEM-0001", "set": {"n": 1}},
            {"op": "link", "id": "@a", "rel": "ex:near", "target": str(eid("nowhere"))},
            {"op": "supersede", "by": "@a", "targets": ["ITEM-0001"]},
            {"op": "retract", "id": str(ROOM.id), "by": "@a"},
        ),
        SNAP,
    )
    assert result.refs == {"LOC-0002": ROOM.id, "ITEM-0001": DESK.id}
    assert result.pairs == (
        ("write.create", "ex:Thing"),
        ("write.link", "ex:Thing"),
        ("write.retract", "ex:Place"),
        ("write.retract", "ex:Thing"),
        ("write.supersede", "ex:Thing"),
        ("write.update", "ex:Thing"),
    )
    assert result.warnings == ()


def test_former_alias_resolves_with_a_warning() -> None:
    result = resolve_refs(cs({"op": "update", "id": "LOC-0001", "set": {"n": 1}}), SNAP)
    assert result.refs == {"LOC-0001": ROOM.id}
    (warning,) = result.warnings
    assert warning.code == "entity.alias_renamed"
    assert warning.details == {"alias": "LOC-0001", "id": str(ROOM.id), "current": "LOC-0002"}


@pytest.mark.parametrize(
    ("ops", "code", "path"),
    [
        ([{"op": "update", "id": "ITEM-0404", "set": {"n": 1}}], "entity.not_found", "ops[0].id"),
        (
            [{"op": "update", "id": str(eid("ghost")), "set": {"n": 1}}],
            "entity.not_found",
            "ops[0].id",
        ),
        (
            [{"op": "supersede", "by": "ITEM-0001", "targets": [str(eid("ghost"))]}],
            "entity.not_found",
            "ops[0].targets[0]",
        ),
        (
            [{"op": "link", "id": "ITEM-0001", "rel": "ex:r", "target": "ITEM-0404"}],
            "entity.not_found",
            "ops[0].target",
        ),
        (
            [{"op": "update", "id": "@nope", "set": {"n": 1}}],
            "changeset.local_ref_unresolved",
            "ops[0].id",
        ),
        (
            [
                {"op": "create", "type": "ex:T", "local": "a"},
                {"op": "create", "type": "ex:T", "local": "a"},
            ],
            "changeset.local_ref_duplicate",
            "ops[1].local",
        ),
    ],
)
def test_resolution_errors(ops: list[dict[str, Any]], code: str, path: str) -> None:
    found = error(cs(*ops))
    assert (found.code, found.details["path"]) == (code, path)


def test_edge_targets_given_as_ids_are_not_checked() -> None:
    ghost = str(eid("ghost"))
    result = resolve_refs(
        cs({"op": "link", "id": "ITEM-0001", "rel": "ex:r", "target": ghost}), SNAP
    )
    assert result.refs == {"ITEM-0001": DESK.id}


def test_bindings_from_earlier_documents() -> None:
    bindings = {"shelf": ROOM.id}
    result = resolve_refs(
        cs({"op": "update", "id": "@shelf", "set": {"n": 1}}), SNAP, bindings=bindings
    )
    assert result.refs == {"@shelf": ROOM.id}
    assert result.pairs == (("write.update", "ex:Place"),)
    found = error(cs({"op": "create", "type": "ex:T", "local": "shelf"}), bindings=bindings)
    assert found.code == "changeset.local_ref_duplicate"


def test_abbreviations_only_when_allowed() -> None:
    suffix = DESK.id.hex[-10:]
    changeset = ChangeSet(ops=(UpdateOp(AbbrevRef(suffix), {"n": 1}),))
    assert error(changeset).code == "changeset.malformed"
    result = resolve_refs(changeset, SNAP, allow_abbrev=True)
    assert result.changeset.ops == (UpdateOp(DESK.id, {"n": 1}),)
    link = ChangeSet(ops=(LinkOp(DESK.id, EdgeSpec(TermRef("ex:r"), AbbrevRef(ROOM.id.hex[-8:]))),))
    resolved = resolve_refs(link, SNAP, allow_abbrev=True).changeset.ops[0]
    assert isinstance(resolved, LinkOp) and resolved.edge.target == ROOM.id


def test_ambiguous_abbreviation_lists_candidates() -> None:
    twins = [
        replace(DESK, id=EntityId(UUID(f"0192f0{n:02d}-0000-7000-8000-0000aaaaaaaa")))
        for n in range(12)
    ]
    changeset = ChangeSet(ops=(UpdateOp(AbbrevRef("aaaaaaaa"), {"n": 1}),))
    with pytest.raises(GraphCliError) as info:
        resolve_refs(changeset, DictSnapshot(*twins), allow_abbrev=True)
    assert info.value.code == "entity.ambiguous_ref"
    candidates = info.value.details["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 10


# ---- the terms of an op (write allow lists plan W4) --------------------------------------

NOTE = revision(
    "note",
    "ex:Thing",
    {"name": "N"},
    [edge("ex:near", "room", name="near"), edge("ex:in", "desk", name="in")],
    alias="ITEM-0002",
)
TERM_SNAP = DictSnapshot(ROOM, DESK, NOTE)
NEAR = str(edge("ex:near", "room", name="near").edge_id)


def terms(*ops: dict[str, Any]) -> tuple[tuple[str, str, str, str], ...]:
    return resolve_refs(cs(*ops), TERM_SNAP).fine


def test_every_op_row_names_its_terms_on_the_subjects_type() -> None:
    found = resolve_refs(
        cs(
            {
                "op": "create",
                "type": "ex:Thing",
                "local": "a",
                "attrs": {"name": "A"},
                "attrs_text": {"note": "n"},
                "edges": [{"rel": "ex:in", "target": "LOC-0002"}],
            },
            {
                "op": "update",
                "id": "ITEM-0001",
                "set": {"a": 1},
                "set_text": {"b": "x"},
                "unset": ["c"],
                "add": {"d": [1]},
                "add_text": {"e": ["x"]},
                "remove": {"f": [1]},
                "remove_text": {"g": ["x"]},
            },
            {"op": "link", "id": "@a", "rel": "ex:near", "target": "LOC-0002"},
            {"op": "unlink", "id": "ITEM-0002", "rel": "ex:in", "target": "ITEM-0001"},
            {"op": "transition", "id": "ITEM-0001", "to": "done", "attr": "status"},
            {"op": "supersede", "by": "@a", "targets": ["ITEM-0001"]},
            {"op": "retract", "id": "ITEM-0001"},
            {"op": "set_alias", "id": "ITEM-0001", "alias": "ITEM-0099"},
        ),
        TERM_SNAP,
    )
    attr, rel = "attr", "rel"
    assert found.fine == tuple(
        sorted(
            [
                ("write.create", "ex:Thing", attr, "name"),
                ("write.create", "ex:Thing", attr, "note"),
                ("write.create", "ex:Thing", rel, "ex:in"),  # an inline edge, as create
                *(("write.update", "ex:Thing", attr, n) for n in "abcdefg"),
                ("write.link", "ex:Thing", rel, "ex:near"),
                ("write.unlink", "ex:Thing", rel, "ex:in"),
                ("write.transition", "ex:Thing", attr, "status"),
            ]
        )
    )
    assert found.bare_transitions == ()


def test_the_terms_of_an_op_sit_on_the_type_of_its_subject() -> None:
    assert terms({"op": "update", "id": "LOC-0002", "set": {"a": 1}}) == (
        ("write.update", "ex:Place", "attr", "a"),
    )
    assert terms({"op": "link", "id": "LOC-0002", "rel": "ex:near", "target": "ITEM-0001"}) == (
        ("write.link", "ex:Place", "rel", "ex:near"),
    )


def test_an_unlink_by_edge_id_reads_the_relation_from_the_subjects_revision() -> None:
    assert terms({"op": "unlink", "id": "ITEM-0002", "edge_id": NEAR}) == (
        ("write.unlink", "ex:Thing", "rel", "ex:near"),
    )


def test_an_unlink_by_edge_id_without_a_readable_edge_has_no_term() -> None:
    ghost = str(edge("ex:near", "room", name="ghost").edge_id)
    assert terms({"op": "unlink", "id": "ITEM-0002", "edge_id": ghost}) == ()  # unknown edge
    assert terms({"op": "unlink", "id": "LOC-0002", "edge_id": NEAR}) == ()  # not its edge
    local = resolve_refs(
        cs(
            {"op": "create", "type": "ex:Thing", "local": "a"},
            {"op": "unlink", "id": "@a", "edge_id": NEAR},
        ),
        TERM_SNAP,
    )
    assert local.fine == ()  # an @local of this document has no revision
    bound = resolve_refs(
        cs({"op": "unlink", "id": "@earlier", "edge_id": NEAR}),
        TERM_SNAP,
        bindings={"earlier": NOTE.id},
    )
    assert bound.fine == (("write.unlink", "ex:Thing", "rel", "ex:near"),)  # an earlier document's


def test_a_transition_without_attr_is_left_to_the_service() -> None:
    found = resolve_refs(
        cs(
            {"op": "transition", "id": "ITEM-0001", "to": "done"},
            {"op": "transition", "id": "LOC-0002", "to": "done"},
            {"op": "transition", "id": "ITEM-0002", "to": "done"},
        ),
        TERM_SNAP,
    )
    assert found.fine == ()  # the lifecycle attribute is the model's to name
    assert found.bare_transitions == ("ex:Place", "ex:Thing")


def test_terms_outside_the_resource_grammar_are_left_out() -> None:
    """A hand-built change set may name what the parser would refuse; no resource can name
    it, so it is no term and validation reports it."""
    bad = ChangeSet(
        ops=(
            UpdateOp(DESK.id, {"Bad Name": 1, "good": 2}, unset=("also bad",)),
            LinkOp(DESK.id, EdgeSpec(TermRef("not a curie"), ROOM.id)),
        )
    )
    assert resolve_refs(bad, TERM_SNAP).fine == (("write.update", "ex:Thing", "attr", "good"),)
