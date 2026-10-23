"""Change set documents (contracts B3, B1.2, B1.9, B3.13, C.1, C.5, C.6; plan W2)."""

import json
from typing import Any
from uuid import UUID

import pytest

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    CHANGESET_SCHEMA,
    MAX_REASON,
    ChangeSet,
    CreateOp,
    EdgeSpec,
    FloatLiteral,
    LinkOp,
    RetractOp,
    SetAliasOp,
    SupersedeOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    changeset_hash,
    changeset_to_json,
    contains_float,
    load_document,
    op_action,
    parse_changeset,
    read_documents,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import (
    AbbrevRef,
    AliasRef,
    EdgeId,
    EntityId,
    LocalRef,
    TermRef,
    parse_entity_ref,
    ref_text,
)

ID = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
EDGE = "0192f6d4-2a10-7b44-9e8d-7c6b5a493827"


def doc(*ops: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"ops": list(ops), **top}


def create(**extra: Any) -> dict[str, Any]:
    return {"op": "create", "type": "ex:Thing", **extra}


def malformed(document: JsonValue) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        parse_changeset(document)
    assert info.value.code == "changeset.malformed", info.value.message
    return info.value


def test_full_document_parses_every_op() -> None:
    cs = parse_changeset(
        doc(
            create(
                local="a",
                attrs={"name": "A", "tags": ["x"]},
                attrs_text={"count": "3"},
                edges=[{"rel": "ex:rel", "target": "@b", "qualifiers": {"q": "v"}}],
                valid_from="2026-01-01",
                valid_to="2026-02-01T10:00:00+02:00",
            ),
            {"op": "update", "id": "ITEM-0003", "expect_rev": 2, "set": {"n": 1}, "unset": ["m"]},
            {"op": "link", "id": ID, "rel": "ex:rel", "target": "@a", "target_rev": 1},
            {"op": "unlink", "id": "@a", "edge_id": EDGE},
            {"op": "unlink", "id": "@a", "rel": "ex:rel", "target": "ITEM-0001"},
            {"op": "transition", "id": "@a", "to": "done", "attr": "status"},
            {"op": "supersede", "by": "@a", "targets": [ID.upper(), "ITEM-0002"]},
            {"op": "retract", "id": "@a", "by": f"urn:uuid:{ID}"},
            {"op": "set_alias", "id": "@a", "alias": "ITEM-0042"},
            schema=CHANGESET_SCHEMA,
            reason="why",
            idempotency_key="seed:001",
            expect_seq=0,
        )
    )
    entity = EntityId(UUID(ID))
    assert cs.reason == "why" and cs.idempotency_key == "seed:001" and cs.expect_seq == 0
    first = cs.ops[0]
    assert isinstance(first, CreateOp)
    assert first.local == "a" and first.attrs_text == {"count": "3"}
    assert first.edges == (EdgeSpec(TermRef("ex:rel"), LocalRef("b"), {"q": "v"}),)
    assert (first.valid_from, first.valid_to) == ("2026-01-01", "2026-02-01T08:00:00.000Z")
    assert cs.ops[1] == UpdateOp(AliasRef("ITEM-0003"), {"n": 1}, unset=("m",), expect_rev=2)
    assert cs.ops[2] == LinkOp(entity, EdgeSpec(TermRef("ex:rel"), LocalRef("a"), target_rev=1))
    assert cs.ops[3] == UnlinkOp(LocalRef("a"), edge_id=EdgeId(UUID(EDGE)))
    assert cs.ops[4] == UnlinkOp(LocalRef("a"), rel=TermRef("ex:rel"), target=AliasRef("ITEM-0001"))
    assert cs.ops[5] == TransitionOp(LocalRef("a"), "done", "status")
    assert cs.ops[6] == SupersedeOp(LocalRef("a"), (entity, AliasRef("ITEM-0002")))
    assert cs.ops[7] == RetractOp(LocalRef("a"), entity)
    assert cs.ops[8] == SetAliasOp(LocalRef("a"), "ITEM-0042")
    assert [op_action(op) for op in cs.ops][::4] == ["write.create", "write.unlink", "write.alias"]


def test_round_trip_is_canonical_and_stable() -> None:
    cs = parse_changeset(
        doc(
            create(local="a", attrs={"n": 1}, edges=[{"rel": "ex:r", "target": ID}]),
            {"op": "supersede", "by": "@a", "targets": [f"urn:uuid:{ID.upper()}"]},
            reason="r",
        )
    )
    data = changeset_to_json(cs)
    assert data == {
        "schema": CHANGESET_SCHEMA,
        "reason": "r",
        "ops": [
            {
                "op": "create",
                "type": "ex:Thing",
                "local": "a",
                "attrs": {"n": 1},
                "edges": [{"rel": "ex:r", "target": ID}],
            },
            {"op": "supersede", "by": "@a", "targets": [ID]},
        ],
    }
    assert parse_changeset(data) == cs


def test_hash_ignores_key_order_and_explicit_defaults() -> None:
    plain = doc(create(attrs={"a": 1, "b": 2}))
    noisy = {
        "schema": CHANGESET_SCHEMA,
        "ops": [{"type": "ex:Thing", "attrs": {"b": 2, "a": 1}, "op": "create", "edges": []}],
    }
    assert changeset_hash(parse_changeset(plain)) == changeset_hash(parse_changeset(noisy))
    other = doc(create(attrs={"a": 1, "b": 3}))
    assert changeset_hash(parse_changeset(plain)) != changeset_hash(parse_changeset(other))


@pytest.mark.parametrize(
    ("document", "path"),
    [
        ([], ""),
        ({}, "ops"),
        ({"ops": []}, "ops"),
        ({"ops": [create()], "extra": 1}, "extra"),
        ({"ops": [create()], "schema": "other"}, "schema"),
        ({"ops": [create()], "reason": ""}, "reason"),
        ({"ops": [create()], "reason": "x" * (MAX_REASON + 1)}, "reason"),
        ({"ops": [create()], "idempotency_key": "has space"}, "idempotency_key"),
        ({"ops": [create()], "expect_seq": -1}, "expect_seq"),
        ({"ops": [create()], "expect_seq": True}, "expect_seq"),
        (doc({"type": "ex:Thing"}), "ops[0].op"),
        (doc({"op": "delete", "id": ID}), "ops[0].op"),
        (doc(create(id=ID)), "ops[0].id"),
        (doc({"op": "create", "type": "Thing"}), "ops[0].type"),
        (doc(create(local="@a")), "ops[0].local"),
        (doc(create(local="A")), "ops[0].local"),
        (doc(create(attrs={"Bad": 1})), "ops[0].attrs.Bad"),
        (doc(create(attrs=[1])), "ops[0].attrs"),
        (doc(create(attrs_text={"n": 1})), "ops[0].attrs_text.n"),
        (doc(create(attrs={"n": 1}, attrs_text={"n": "1"})), "ops[0].attrs_text.n"),
        (doc(create(edges={})), "ops[0].edges"),
        (doc(create(edges=[{"rel": "ex:r"}])), "ops[0].edges[0].target"),
        (doc(create(edges=[{"rel": "ex:r", "target": ID, "x": 1}])), "ops[0].edges[0].x"),
        (
            doc(create(edges=[{"rel": "ex:r", "target": ID, "target_rev": 0}])),
            "ops[0].edges[0].target_rev",
        ),
        (doc(create(valid_from="2026-13-01")), "ops[0].valid_from"),
        (doc(create(valid_from="2026-01-01T00:00:00")), "ops[0].valid_from"),
        (doc({"op": "update", "id": ID}), "ops[0]"),
        (doc({"op": "update", "id": ID, "set": {"a": 1}, "unset": ["a"]}), "ops[0].unset[0]"),
        (doc({"op": "update", "id": ID, "unset": ["a", "a"]}), "ops[0].unset[1]"),
        (doc({"op": "update", "id": ID, "set": {"a": 1}, "expect_rev": 0}), "ops[0].expect_rev"),
        (doc({"op": "update", "id": ID, "add": {"a": []}}), "ops[0].add.a"),
        (doc({"op": "update", "id": ID, "add": {"a": "x"}}), "ops[0].add.a"),
        (doc({"op": "update", "id": ID, "add_text": {"a": [1]}}), "ops[0].add_text.a[0]"),
        (doc({"op": "update", "id": ID, "remove": {"A": [1]}}), "ops[0].remove.A"),
        (doc({"op": "update", "id": ID, "set": {"a": [1]}, "add": {"a": [2]}}), "ops[0].add.a"),
        (doc({"op": "update", "id": ID, "unset": ["a"], "remove": {"a": [2]}}), "ops[0].remove.a"),
        (
            doc({"op": "update", "id": ID, "add": {"a": [1]}, "remove_text": {"a": ["1"]}}),
            "ops[0].remove_text.a",
        ),
        (doc({"op": "link", "id": ID, "rel": "ex:r"}), "ops[0].target"),
        (doc({"op": "unlink", "id": ID}), "ops[0]"),
        (doc({"op": "unlink", "id": ID, "rel": "ex:r"}), "ops[0]"),
        (doc({"op": "unlink", "id": ID, "edge_id": EDGE, "rel": "ex:r", "target": ID}), "ops[0]"),
        (doc({"op": "unlink", "id": ID, "edge_id": "ITEM-0001"}), "ops[0].edge_id"),
        (doc({"op": "transition", "id": ID}), "ops[0].to"),
        (doc({"op": "supersede", "by": ID, "targets": []}), "ops[0].targets"),
        (doc({"op": "supersede", "by": ID, "targets": [ID, ID]}), "ops[0].targets[1]"),
        (doc({"op": "set_alias", "id": ID, "alias": "item-1"}), "ops[0].alias"),
        (doc({"op": "retract", "id": 7}), "ops[0].id"),
        (doc({"op": "retract", "id": "not a ref"}), "ops[0].id"),
    ],
)
def test_malformed_documents_name_the_path(document: JsonValue, path: str) -> None:
    assert malformed(document).details["path"] == path


def test_abbreviations_are_refused_in_change_sets() -> None:
    error = malformed(doc({"op": "retract", "id": "a5f60718293a4b5c"}))
    assert "abbreviated" in error.message


def test_create_with_id_names_the_rule() -> None:
    assert "B1.2" in malformed(doc(create(id=ID))).message


def test_unknown_schema_major_is_unsupported() -> None:
    with pytest.raises(GraphCliError) as info:
        parse_changeset(doc(create(), schema="graph_cli.changeset/2"))
    assert info.value.code == "format.unsupported_version"


def test_too_many_ops() -> None:
    with pytest.raises(GraphCliError) as info:
        parse_changeset(doc(*[create()] * 4), max_ops=3)
    assert info.value.code == "changeset.too_large"
    assert info.value.details["actual_ops"] == 4


def test_too_many_bytes() -> None:
    data = json.dumps(doc(create(attrs={"text": "x" * 200}))).encode()
    with pytest.raises(GraphCliError) as info:
        load_document(data, max_bytes=100)
    assert info.value.code == "changeset.too_large"


def test_floats_in_values_survive_as_literals() -> None:
    raw = '{"ops": [{"op": "create", "type": "ex:T", "attrs": {"n": 1.50, "j": {"x": [2e3]}},'
    raw += ' "edges": [{"rel": "ex:r", "target": "@a", "qualifiers": {"q": 0.5}}]}]}'
    cs = parse_changeset(load_document(raw))
    op = cs.ops[0]
    assert isinstance(op, CreateOp)
    assert isinstance(op.attrs["n"], FloatLiteral) and op.attrs["n"] == "1.50"
    assert contains_float(op.attrs["j"]) and contains_float(op.edges[0].qualifiers)
    assert not contains_float({"x": ["1.5"]})


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ('{"ops": [{"op": "create", "type": "ex:T"}], "expect_seq": 1.0}', "expect_seq"),
        (
            '{"ops": [{"op": "create", "type": "ex:T", "attrs_text": {"n": 1.5}}]}',
            "ops[0].attrs_text.n",
        ),
        (
            '{"ops": [{"op": "update", "id": "ITEM-0001", "set": {"a": 1}, "expect_rev": 2.0}]}',
            "ops[0].expect_rev",
        ),
        ('{"ops": [{"op": "create", "type": "ex:T"}], "reason": 1.5}', "reason"),
    ],
)
def test_floats_elsewhere_are_malformed(raw: str, path: str) -> None:
    assert malformed(load_document(raw)).details["path"] == path


@pytest.mark.parametrize(
    "raw",
    [
        b'{"ops": [}',
        b'{"ops": NaN}',
        b'{"ops": Infinity}',
        b'{"a": 1, "a": 2}',
        b'{"n": 9007199254740992}',
        b'{"s": "\\u0000"}',
        b'{"s": "\\ud800"}',
        b'{"s": "\xff"}',
    ],
)
def test_input_errors_are_usage_errors(raw: bytes) -> None:
    with pytest.raises(GraphCliError) as info:
        load_document(raw)
    assert info.value.code == "usage.invalid_input"


def test_read_documents_single_pretty_and_jsonl() -> None:
    one = doc(create())
    assert read_documents(json.dumps(one).encode()) == [one]
    assert read_documents(json.dumps(one, indent=2).encode()) == [one]
    two = doc(create(local="b"))
    jsonl = f"{json.dumps(one)}\n\n{json.dumps(two)}\n".encode()
    assert read_documents(jsonl) == [one, two]


def test_read_documents_reports_the_broken_line() -> None:
    jsonl = f'{json.dumps(doc(create()))}\n{{"ops": [}}\n'.encode()
    with pytest.raises(GraphCliError) as info:
        read_documents(jsonl)
    assert info.value.code == "usage.invalid_input"
    assert info.value.details["line"] == 2
    with pytest.raises(GraphCliError) as info:
        read_documents(b"  \n")
    assert info.value.code == "usage.invalid_input"


def test_entity_ref_grammar() -> None:
    assert parse_entity_ref("@a") is None
    assert parse_entity_ref("@a", allow_local=True) == LocalRef("a")
    assert parse_entity_ref("ITEM-0001") == AliasRef("ITEM-0001")
    assert parse_entity_ref(f"urn:uuid:{ID}") == EntityId(UUID(ID))
    assert parse_entity_ref("A5F60718") is None
    assert parse_entity_ref("A5F60718", allow_abbrev=True) == AbbrevRef("a5f60718")
    assert parse_entity_ref("a5f6071") is None
    for ref in (LocalRef("a"), AliasRef("ITEM-0001"), EntityId(UUID(ID)), AbbrevRef("a5f60718")):
        assert parse_entity_ref(ref_text(ref), allow_local=True, allow_abbrev=True) == ref


def test_changeset_without_optional_parts() -> None:
    cs = parse_changeset(doc(create()))
    assert cs == ChangeSet(ops=(CreateOp(TermRef("ex:Thing")),))
    assert changeset_to_json(cs) == {"schema": CHANGESET_SCHEMA, "ops": [create()]}


def test_update_list_changes_round_trip() -> None:
    document = doc(
        {
            "op": "update",
            "id": ID,
            "add": {"tags": ["a", 2]},
            "add_text": {"days": ["2026-10-01"]},
            "remove": {"refs": [{"k": 1}]},
            "remove_text": {"names": ["x"]},
        }
    )
    cs = parse_changeset(document)
    (op,) = cs.ops
    assert isinstance(op, UpdateOp)
    assert op.add == {"tags": ("a", 2)} and op.remove_text == {"names": ("x",)}
    assert op.list_names() == ("days", "names", "refs", "tags")
    assert changeset_to_json(cs)["ops"] == [{**document["ops"][0], "id": ID}]
    assert parse_changeset(changeset_to_json(cs)) == cs


def test_an_update_with_only_list_changes_is_valid() -> None:
    (op,) = parse_changeset(doc({"op": "update", "id": ID, "remove": {"a": [1]}})).ops
    assert isinstance(op, UpdateOp) and op.remove == {"a": (1,)}
