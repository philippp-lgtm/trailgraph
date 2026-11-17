"""The ``source`` and ``reconcile`` parts of a change set document (reconcile plan R-K1 to R-K5,
R-K10; contracts B3, C.6): strict keys, grammar, key rules, membership of fields and edges,
limits, and the canonical form."""

import copy
import json
from typing import Any

import pytest

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    CHANGESET_SCHEMA,
    DOCUMENT_KEYS,
    MAX_KEY_BYTES,
    MAX_RECONCILE_SCOPES,
    RETRACT,
    ChangeSet,
    ExpectSpec,
    FloatLiteral,
    MoveSpec,
    ReconcileEdge,
    ReconcileItem,
    ReconcileScope,
    RetireSpec,
    ValueRef,
    changeset_hash,
    changeset_to_json,
    document_hash,
    document_to_json,
    load_document,
    parse_changeset,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, LocalRef, TermRef, parse_id
from graph_cli.kernel.records import SourceRef

ID = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
SOURCE = {"name": "inventory", "version": "collector 3", "run": "2026-11-17T10:00:00Z"}


def item(key: str = "host_a/curl", **extra: Any) -> dict[str, Any]:
    return {"key": key, **extra}


def scope(**extra: Any) -> dict[str, Any]:
    return {"type": "ex:Installation", "key": "source_key", "items": [], **extra}


def reconcile_doc(*scopes: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"source": SOURCE, "reconcile": list(scopes or [scope()]), **top}


def full_scope() -> dict[str, Any]:
    return {
        "type": "ex:Installation",
        "key": "source_key",
        "under": "host_a/",
        "complete": True,
        "fields": ["version", "package"],
        "edges": ["ex:installed_on", "ex:installation_of"],
        "retire": {"transition": "removed", "attr": "state", "revive": "present"},
        "allow_retire": 40,
        "allow_empty": True,
        "keep": ["host_a/zlib", "host_a/bash"],
        "moves": [
            {"from": "host_a/old_name", "to": "host_a/curl"},
            {"id": ID, "to": "host_a/jq"},
        ],
        "expect": {"min_items": 1, "max_items": 500},
        "items": [
            {
                "key": "host_a/curl",
                "type": "ex:Installation",
                "local": "curl",
                "attrs": {"package": "curl"},
                "attrs_text": {"version": "8.5.0"},
                "edges": [
                    {"rel": "ex:installation_of", "target": ID, "qualifiers": {"pinned": True}},
                    {
                        "rel": "ex:installed_on",
                        "target": {"type": "ex:Host", "attr": "hostname", "value": "host-a"},
                    },
                ],
                "valid_from": "2026-11-01",
                "valid_to": "2026-12-01T00:00:00Z",
            },
            {
                "key": "host_a/jq",
                "attrs": {"package": "jq", "version": "1.7"},
                "edges": [{"rel": "ex:installation_of", "target": "@pkg_jq"}],
            },
        ],
    }


def malformed(document: JsonValue) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        parse_changeset(document)
    assert info.value.code == "changeset.malformed", info.value.message
    return info.value


# ---- what parses ---------------------------------------------------------------------------


def test_a_reconcile_document_parses_into_its_records() -> None:
    cs = parse_changeset(
        {
            "schema": CHANGESET_SCHEMA,
            "reason": "Inventory run of host-a",
            "idempotency_key": "inventory-host-a-1",
            "expect_seq": 3,
            "source": SOURCE,
            "reconcile": [full_scope()],
        }
    )
    assert cs.ops == ()
    assert (cs.reason, cs.idempotency_key, cs.expect_seq) == (
        "Inventory run of host-a",
        "inventory-host-a-1",
        3,
    )
    assert cs.source == SourceRef("inventory", "collector 3", "2026-11-17T10:00:00Z")
    (found,) = cs.reconcile
    assert (found.type, found.key, found.under, found.complete) == (
        "ex:Installation",
        "source_key",
        "host_a/",
        True,
    )
    assert found.fields == ("package", "version")  # sets are sorted
    assert found.edges == (TermRef("ex:installation_of"), TermRef("ex:installed_on"))
    assert found.retire == RetireSpec("removed", "state", "present")
    assert (found.allow_retire, found.allow_empty) == (40, True)
    assert found.keep == ("host_a/bash", "host_a/zlib")
    assert found.moves == (
        MoveSpec(to="host_a/curl", old_key="host_a/old_name"),
        MoveSpec(to="host_a/jq", id=EntityId(parse_id(ID))),
    )
    assert found.expect == ExpectSpec(1, 500)
    first, second = found.items
    assert first == ReconcileItem(
        key="host_a/curl",
        type=TermRef("ex:Installation"),
        local="curl",
        attrs={"package": "curl"},
        attrs_text={"version": "8.5.0"},
        edges=(
            ReconcileEdge(TermRef("ex:installation_of"), EntityId(parse_id(ID)), {"pinned": True}),
            ReconcileEdge(
                TermRef("ex:installed_on"), ValueRef(TermRef("ex:Host"), "hostname", "host-a")
            ),
        ),
        valid_from="2026-11-01",
        valid_to="2026-12-01T00:00:00.000Z",
    )
    assert second.edges == (ReconcileEdge(TermRef("ex:installation_of"), LocalRef("pkg_jq")),)


def test_a_scope_has_defaults() -> None:
    (found,) = parse_changeset(reconcile_doc(scope())).reconcile
    assert found == ReconcileScope(TermRef("ex:Installation"), "source_key", ())
    assert (found.under, found.complete, found.retire, found.allow_empty) == (
        "",
        False,
        RETRACT,
        False,
    )
    assert found.retire == RetireSpec() and found.expect is None
    plain = parse_changeset(reconcile_doc(scope(retire="retract", complete=False, under="")))
    assert plain.reconcile == (found,)


def test_ops_are_optional_with_reconcile_and_still_run_first() -> None:
    both = parse_changeset(
        reconcile_doc(
            scope(),
            ops=[
                {"op": "create", "type": "ex:Package", "local": "pkg_jq", "attrs": {"name": "jq"}}
            ],
        )
    )
    assert len(both.ops) == 1 and len(both.reconcile) == 1
    assert parse_changeset(reconcile_doc()).ops == ()


def test_a_source_may_come_with_plain_ops() -> None:
    cs = parse_changeset(
        {"source": {"name": "inventory"}, "ops": [{"op": "create", "type": "ex:Package"}]}
    )
    assert cs.source == SourceRef("inventory") and cs.reconcile == ()


def test_a_document_without_either_is_as_it_was() -> None:
    cs = parse_changeset({"ops": [{"op": "create", "type": "ex:Package"}]})
    assert (cs.source, cs.reconcile) == (None, ())
    assert DOCUMENT_KEYS[0] == () and set(DOCUMENT_KEYS[1]) >= {"ops", "source", "reconcile"}


def test_a_large_item_list_is_bounded_by_the_document_size_only() -> None:
    items = [item(f"host_a/package_{n}", attrs={"version": "1"}) for n in range(3000)]
    cs = parse_changeset(reconcile_doc(scope(fields=["version"], items=items)))
    assert len(cs.reconcile[0].items) == 3000


# ---- strictness, key by key ------------------------------------------------------------------

BAD_DOCUMENTS = [
    # the document: ops or reconcile, and the source that goes with reconcile
    ({}, "ops"),
    ({"source": SOURCE}, "ops"),
    ({"ops": [], "source": SOURCE, "reconcile": [scope()]}, "ops"),
    ({"reconcile": [scope()]}, "source"),
    (reconcile_doc(scope(), extra=1), "extra"),
    ({"source": SOURCE, "reconcile": []}, "reconcile"),
    ({"source": SOURCE, "reconcile": {}}, "reconcile"),
    ({"source": SOURCE, "reconcile": [1]}, "reconcile[0]"),
    # source
    ({"source": "inventory", "reconcile": [scope()]}, "source"),
    ({"source": {}, "reconcile": [scope()]}, "source.name"),
    ({"source": {"name": "Inventory"}, "reconcile": [scope()]}, "source.name"),
    ({"source": {"name": "a/b"}, "reconcile": [scope()]}, "source.name"),
    ({"source": {"name": "a" * 65}, "reconcile": [scope()]}, "source.name"),
    ({"source": {"name": "inventory", "x": 1}, "reconcile": [scope()]}, "source.x"),
    ({"source": {"name": "inventory", "kind": "extractor"}, "reconcile": [scope()]}, "source.kind"),
    ({"source": {"name": "inventory", "version": ""}, "reconcile": [scope()]}, "source.version"),
    ({"source": {"name": "inventory", "version": 3}, "reconcile": [scope()]}, "source.version"),
    ({"source": {"name": "inventory", "run": "x" * 129}, "reconcile": [scope()]}, "source.run"),
    ({"source": {"name": "inventory", "run": "a\nb"}, "reconcile": [scope()]}, "source.run"),
    ({"source": {"name": "inventory", "run": " 1"}, "reconcile": [scope()]}, "source.run"),
    # a scope
    (reconcile_doc({"key": "source_key", "items": []}), "reconcile[0].type"),
    (reconcile_doc({"type": "ex:Installation", "items": []}), "reconcile[0].key"),
    (reconcile_doc({"type": "ex:Installation", "key": "source_key"}), "reconcile[0].items"),
    (reconcile_doc(scope(type="Installation")), "reconcile[0].type"),
    (reconcile_doc(scope(key="Source_Key")), "reconcile[0].key"),
    (reconcile_doc(scope(key="ex:key")), "reconcile[0].key"),
    (reconcile_doc(scope(extra=1)), "reconcile[0].extra"),
    (reconcile_doc(scope(items={})), "reconcile[0].items"),
    (reconcile_doc(scope(under=3)), "reconcile[0].under"),
    (reconcile_doc(scope(under="/host_a/")), "reconcile[0].under"),
    (reconcile_doc(scope(under="host a\n")), "reconcile[0].under"),
    (reconcile_doc(scope(under="x" * (MAX_KEY_BYTES + 1))), "reconcile[0].under"),
    (reconcile_doc(scope(complete="yes")), "reconcile[0].complete"),
    (reconcile_doc(scope(complete=1)), "reconcile[0].complete"),
    (reconcile_doc(scope(allow_empty=None)), "reconcile[0].allow_empty"),
    (reconcile_doc(scope(allow_retire=-1)), "reconcile[0].allow_retire"),
    (reconcile_doc(scope(allow_retire=True)), "reconcile[0].allow_retire"),
    (reconcile_doc(scope(allow_retire="5")), "reconcile[0].allow_retire"),
    (reconcile_doc(scope(fields="package")), "reconcile[0].fields"),
    (reconcile_doc(scope(fields=["Package"])), "reconcile[0].fields[0]"),
    (reconcile_doc(scope(fields=["a", "a"])), "reconcile[0].fields[1]"),
    (reconcile_doc(scope(fields=["a", "source_key"])), "reconcile[0].fields"),
    (reconcile_doc(scope(edges=["installed_on"])), "reconcile[0].edges[0]"),
    (reconcile_doc(scope(edges=["ex:r", "ex:r"])), "reconcile[0].edges[1]"),
    (reconcile_doc(scope(keep="a")), "reconcile[0].keep"),
    (reconcile_doc(scope(keep=["a/b", "a/b"])), "reconcile[0].keep[1]"),
    (reconcile_doc(scope(keep=[""])), "reconcile[0].keep[0]"),
    (reconcile_doc(scope(under="host_a/", keep=["host_b/x"])), "reconcile[0].keep[0]"),
    # retire
    (reconcile_doc(scope(retire="delete")), "reconcile[0].retire"),
    (reconcile_doc(scope(retire=3)), "reconcile[0].retire"),
    (reconcile_doc(scope(retire={})), "reconcile[0].retire.transition"),
    (reconcile_doc(scope(retire={"attr": "state"})), "reconcile[0].retire.transition"),
    (reconcile_doc(scope(retire={"transition": "Gone"})), "reconcile[0].retire.transition"),
    (reconcile_doc(scope(retire={"transition": "gone", "x": 1})), "reconcile[0].retire.x"),
    (
        reconcile_doc(scope(retire={"transition": "gone", "revive": "Up"})),
        "reconcile[0].retire.revive",
    ),
    (
        reconcile_doc(scope(retire={"transition": "gone", "revive": "gone"})),
        "reconcile[0].retire.revive",
    ),
    # expect
    (reconcile_doc(scope(expect={})), "reconcile[0].expect"),
    (reconcile_doc(scope(expect=5)), "reconcile[0].expect"),
    (reconcile_doc(scope(expect={"min_items": -1})), "reconcile[0].expect.min_items"),
    (reconcile_doc(scope(expect={"max_items": 1.5})), "reconcile[0].expect.max_items"),
    (reconcile_doc(scope(expect={"min_items": 3, "max_items": 2})), "reconcile[0].expect"),
    (reconcile_doc(scope(expect={"min": 1})), "reconcile[0].expect.min"),
    # moves
    (reconcile_doc(scope(moves={})), "reconcile[0].moves"),
    (reconcile_doc(scope(moves=["a"])), "reconcile[0].moves[0]"),
    (reconcile_doc(scope(moves=[{"to": "a/b"}])), "reconcile[0].moves[0]"),
    (reconcile_doc(scope(moves=[{"from": "a", "to": "b", "id": ID}])), "reconcile[0].moves[0]"),
    (reconcile_doc(scope(moves=[{"from": "a"}])), "reconcile[0].moves[0].to"),
    (reconcile_doc(scope(moves=[{"id": ID}])), "reconcile[0].moves[0].to"),
    (reconcile_doc(scope(moves=[{"from": "a", "to": "b", "x": 1}])), "reconcile[0].moves[0].x"),
    (reconcile_doc(scope(moves=[{"from": "a", "to": "a"}])), "reconcile[0].moves[0]"),
    (reconcile_doc(scope(moves=[{"from": "/a", "to": "b"}])), "reconcile[0].moves[0].from"),
    (reconcile_doc(scope(moves=[{"from": "a", "to": ""}])), "reconcile[0].moves[0].to"),
    (reconcile_doc(scope(moves=[{"id": "not a ref", "to": "b"}])), "reconcile[0].moves[0].id"),
    (
        reconcile_doc(scope(moves=[{"id": "a5f60718293a4b5c", "to": "b"}])),
        "reconcile[0].moves[0].id",
    ),
    (
        reconcile_doc(scope(under="host_a/", moves=[{"from": "host_a/x", "to": "other"}])),
        "reconcile[0].moves[0].to",
    ),
    (
        reconcile_doc(scope(under="host_a/", moves=[{"from": "other", "to": "host_a/x"}])),
        "reconcile[0].moves[0].from",
    ),
    # an item
    (reconcile_doc(scope(items=[1])), "reconcile[0].items[0]"),
    (reconcile_doc(scope(items=[{}])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(extra=1)])), "reconcile[0].items[0].extra"),
    (reconcile_doc(scope(items=[item(key=3)])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(key="")])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(key="/a")])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(key="a b ")])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(key="a\tb")])), "reconcile[0].items[0].key"),
    (reconcile_doc(scope(items=[item(key="a\x00b")])), "reconcile[0].items[0].key"),
    (
        reconcile_doc(scope(items=[item(key="x" * (MAX_KEY_BYTES + 1))])),
        "reconcile[0].items[0].key",
    ),
    (
        reconcile_doc(scope(items=[item(key="ä" * (MAX_KEY_BYTES // 2 + 1))])),
        "reconcile[0].items[0].key",
    ),
    (
        reconcile_doc(scope(under="host_a/", items=[item(key="host_b/x")])),
        "reconcile[0].items[0].key",
    ),
    (reconcile_doc(scope(items=[item(type="Installation")])), "reconcile[0].items[0].type"),
    (reconcile_doc(scope(items=[item(local="@a")])), "reconcile[0].items[0].local"),
    (reconcile_doc(scope(items=[item(local="A")])), "reconcile[0].items[0].local"),
    (
        reconcile_doc(scope(items=[item(valid_from="2026-13-01")])),
        "reconcile[0].items[0].valid_from",
    ),
    (reconcile_doc(scope(items=[item(valid_to="soon")])), "reconcile[0].items[0].valid_to"),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs=[1])])),
        "reconcile[0].items[0].attrs",
    ),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs={"A": 1})])),
        "reconcile[0].items[0].attrs.A",
    ),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs_text={"a": 1})])),
        "reconcile[0].items[0].attrs_text.a",
    ),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs={"a": 1}, attrs_text={"a": "1"})])),
        "reconcile[0].items[0].attrs_text.a",
    ),
    # membership: attributes in fields, relations in edges, the key attribute never a field
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs={"b": 1})])),
        "reconcile[0].items[0].attrs.b",
    ),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs_text={"b": "1"})])),
        "reconcile[0].items[0].attrs_text.b",
    ),
    (reconcile_doc(scope(items=[item(attrs={"a": 1})])), "reconcile[0].items[0].attrs.a"),
    (
        reconcile_doc(scope(fields=["a"], items=[item(attrs={"source_key": "x"})])),
        "reconcile[0].items[0].attrs.source_key",
    ),
    (
        reconcile_doc(scope(items=[item(attrs_text={"source_key": "x"})])),
        "reconcile[0].items[0].attrs_text.source_key",
    ),
    (
        reconcile_doc(
            scope(edges=["ex:r"], items=[item(edges=[{"rel": "ex:other", "target": ID}])])
        ),
        "reconcile[0].items[0].edges[0].rel",
    ),
    (
        reconcile_doc(scope(items=[item(edges=[{"rel": "ex:r", "target": ID}])])),
        "reconcile[0].items[0].edges[0].rel",
    ),
    # an item edge
    (reconcile_doc(scope(items=[item(edges={})])), "reconcile[0].items[0].edges"),
    (reconcile_doc(scope(items=[item(edges=[1])])), "reconcile[0].items[0].edges[0]"),
    (
        reconcile_doc(scope(edges=["ex:r"], items=[item(edges=[{"rel": "ex:r"}])])),
        "reconcile[0].items[0].edges[0].target",
    ),
    (
        reconcile_doc(scope(edges=["ex:r"], items=[item(edges=[{"target": ID}])])),
        "reconcile[0].items[0].edges[0].rel",
    ),
    (
        reconcile_doc(
            scope(edges=["ex:r"], items=[item(edges=[{"rel": "ex:r", "target": ID, "x": 1}])])
        ),
        "reconcile[0].items[0].edges[0].x",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[item(edges=[{"rel": "ex:r", "target": ID, "target_rev": 1}])],
            )
        ),
        "reconcile[0].items[0].edges[0].target_rev",
    ),
    (
        reconcile_doc(
            scope(edges=["ex:r"], items=[item(edges=[{"rel": "ex:r", "target": "not a ref"}])])
        ),
        "reconcile[0].items[0].edges[0].target",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[item(edges=[{"rel": "ex:r", "target": "a5f60718293a4b5c"}])],
            )
        ),
        "reconcile[0].items[0].edges[0].target",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[item(edges=[{"rel": "ex:r", "target": ID, "qualifiers": {"Q": 1}}])],
            )
        ),
        "reconcile[0].items[0].edges[0].qualifiers.Q",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[item(edges=[{"rel": "ex:r", "target": ID}, {"rel": "ex:r", "target": ID}])],
            )
        ),
        "reconcile[0].items[0].edges[1]",
    ),
    # an edge target given by value
    (
        reconcile_doc(
            scope(
                edges=["ex:r"], items=[item(edges=[{"rel": "ex:r", "target": {"type": "ex:Host"}}])]
            )
        ),
        "reconcile[0].items[0].edges[0].target.attr",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[
                    item(
                        edges=[
                            {"rel": "ex:r", "target": {"type": "Host", "attr": "a", "value": "x"}}
                        ]
                    )
                ],
            )
        ),
        "reconcile[0].items[0].edges[0].target.type",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[
                    item(
                        edges=[
                            {
                                "rel": "ex:r",
                                "target": {"type": "ex:Host", "attr": "A", "value": "x"},
                            }
                        ]
                    )
                ],
            )
        ),
        "reconcile[0].items[0].edges[0].target.attr",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[
                    item(
                        edges=[
                            {
                                "rel": "ex:r",
                                "target": {"type": "ex:Host", "attr": "a", "value": "x", "y": 1},
                            }
                        ]
                    )
                ],
            )
        ),
        "reconcile[0].items[0].edges[0].target.y",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[
                    item(
                        edges=[
                            {
                                "rel": "ex:r",
                                "target": {"type": "ex:Host", "attr": "a", "value": True},
                            }
                        ]
                    )
                ],
            )
        ),
        "reconcile[0].items[0].edges[0].target.value",
    ),
    (
        reconcile_doc(
            scope(
                edges=["ex:r"],
                items=[
                    item(
                        edges=[
                            {
                                "rel": "ex:r",
                                "target": {"type": "ex:Host", "attr": "a", "value": None},
                            }
                        ]
                    )
                ],
            )
        ),
        "reconcile[0].items[0].edges[0].target.value",
    ),
    # the second scope is named by its index
    (reconcile_doc(scope(), scope(key="Bad")), "reconcile[1].key"),
]


@pytest.mark.parametrize(("document", "path"), BAD_DOCUMENTS)
def test_malformed_reconcile_documents_name_the_path(document: JsonValue, path: str) -> None:
    assert malformed(document).details["path"] == path


def test_duplicate_item_keys_name_both_items() -> None:
    error = malformed(
        reconcile_doc(scope(items=[item("a/x"), item("a/y"), item("a/z"), item("a/y")]))
    )
    assert error.details["path"] == "reconcile[0].items[3].key"
    assert error.details["first_path"] == "reconcile[0].items[1].key"


def test_an_item_key_may_not_also_be_kept() -> None:
    error = malformed(reconcile_doc(scope(keep=["a/x"], items=[item("a/x")])))
    assert error.details["path"] == "reconcile[0].items[0].key"
    assert "keep" in error.message


def test_the_same_key_may_lie_in_two_scopes() -> None:
    # overlap is judged against the graph's types (the planner), not here
    cs = parse_changeset(reconcile_doc(scope(items=[item("a/x")]), scope(items=[item("a/x")])))
    assert len(cs.reconcile) == 2


def test_a_document_cannot_name_a_source_kind() -> None:
    error = malformed({"source": {"name": "inventory", "kind": "extractor"}, "ops": [{}]})
    assert error.details["path"] == "source.kind"
    assert "not by a document" in error.message


def test_a_key_may_be_as_long_as_the_limit_and_use_any_printable_text() -> None:
    longest = "k" * MAX_KEY_BYTES
    wide = "ä" * (MAX_KEY_BYTES // 2)  # two bytes each
    cs = parse_changeset(reconcile_doc(scope(items=[item(longest), item(wide), item("a b/c d")])))
    assert [i.key for i in cs.reconcile[0].items] == [longest, wide, "a b/c d"]


def test_scopes_are_limited() -> None:
    parse_changeset(reconcile_doc(*[scope()] * MAX_RECONCILE_SCOPES))
    with pytest.raises(GraphCliError) as info:
        parse_changeset(reconcile_doc(*[scope()] * (MAX_RECONCILE_SCOPES + 1)))
    assert info.value.code == "changeset.too_large"
    assert info.value.details["actual_scopes"] == MAX_RECONCILE_SCOPES + 1


def test_the_op_limit_counts_the_frame_ops_only() -> None:
    ops = [{"op": "create", "type": "ex:Package"}] * 3
    with pytest.raises(GraphCliError) as info:
        parse_changeset(reconcile_doc(scope(), ops=ops), max_ops=2)
    assert info.value.code == "changeset.too_large"
    items = [item(f"a/{n}") for n in range(50)]
    assert parse_changeset(reconcile_doc(scope(items=items), ops=ops[:1]), max_ops=2)


def test_floats_in_item_values_reach_the_validator_as_literals() -> None:
    document = load_document(
        b'{"source": {"name": "inventory"}, "reconcile": [{"type": "ex:T", "key": "k",'
        b' "fields": ["w"], "items": [{"key": "a", "attrs": {"w": 1.5}}]}]}'
    )
    (found,) = parse_changeset(document).reconcile
    value = found.items[0].attrs["w"]
    assert isinstance(value, FloatLiteral)


def test_a_float_in_an_edge_value_is_malformed() -> None:
    document = load_document(
        b'{"source": {"name": "inventory"}, "reconcile": [{"type": "ex:T", "key": "k",'
        b' "edges": ["ex:r"], "items": [{"key": "a", "edges": [{"rel": "ex:r",'
        b' "target": {"type": "ex:H", "attr": "n", "value": 1.5}}]}]}]}'
    )
    assert malformed(document).details["path"] == "reconcile[0].items[0].edges[0].target.value"


# ---- the canonical form ----------------------------------------------------------------------


def test_the_canonical_form_round_trips() -> None:
    document = reconcile_doc(
        full_scope(),
        scope(type="ex:Folder", key="source_key"),
        reason="r",
        idempotency_key="run-1",
        ops=[{"op": "create", "type": "ex:Package", "local": "pkg_jq", "attrs": {"name": "jq"}}],
    )
    cs = parse_changeset(document)
    data = document_to_json(cs)
    assert parse_changeset(data) == cs
    assert document_to_json(parse_changeset(data)) == data
    assert json.loads(json.dumps(data)) == json.loads(json.dumps(data))
    assert document_hash(parse_changeset(data)) == document_hash(cs)
    assert data["source"] == SOURCE


def test_the_canonical_form_omits_defaults_and_sorts_sets() -> None:
    noisy = reconcile_doc(
        {
            "items": [],
            "retire": "retract",
            "complete": False,
            "allow_empty": False,
            "under": "",
            "fields": ["b", "a"],
            "edges": ["ex:s", "ex:r"],
            "keep": ["k/2", "k/1"],
            "key": "source_key",
            "type": "ex:Installation",
        },
        schema=CHANGESET_SCHEMA,
    )
    assert document_to_json(parse_changeset(noisy)) == {
        "schema": CHANGESET_SCHEMA,
        "source": SOURCE,
        "reconcile": [
            {
                "type": "ex:Installation",
                "key": "source_key",
                "fields": ["a", "b"],
                "edges": ["ex:r", "ex:s"],
                "keep": ["k/1", "k/2"],
                "items": [],
            }
        ],
    }


def test_a_canonical_scope_keeps_the_explicit_non_defaults() -> None:
    (data,) = document_to_json(parse_changeset(reconcile_doc(full_scope())))["reconcile"]  # type: ignore[misc]
    assert data["complete"] is True and data["allow_empty"] is True
    assert data["retire"] == {"transition": "removed", "attr": "state", "revive": "present"}
    assert data["allow_retire"] == 40
    assert data["expect"] == {"min_items": 1, "max_items": 500}
    assert data["moves"] == [
        {"from": "host_a/old_name", "to": "host_a/curl"},
        {"id": ID, "to": "host_a/jq"},
    ]
    assert data["items"][0]["edges"][1]["target"] == {  # type: ignore[index]
        "type": "ex:Host",
        "attr": "hostname",
        "value": "host-a",
    }
    assert data["items"][0]["valid_to"] == "2026-12-01T00:00:00.000Z"  # type: ignore[index]


def test_the_hash_ignores_key_order_and_explicit_defaults_but_not_item_order() -> None:
    first = reconcile_doc(
        scope(items=[item("a/x", attrs={"a": 1, "b": 2}), item("a/y")], fields=["a", "b"])
    )
    shuffled = {
        "reconcile": [
            {
                "items": [item("a/x", attrs={"b": 2, "a": 1}), {"key": "a/y", "edges": []}],
                "fields": ["b", "a"],
                "complete": False,
                "key": "source_key",
                "type": "ex:Installation",
            }
        ],
        "source": {"run": SOURCE["run"], "version": SOURCE["version"], "name": "inventory"},
    }
    assert document_hash(parse_changeset(first)) == document_hash(parse_changeset(shuffled))
    reordered = copy.deepcopy(first)
    reordered["reconcile"][0]["items"].reverse()
    assert document_hash(parse_changeset(first)) != document_hash(parse_changeset(reordered))
    other_value = copy.deepcopy(first)
    other_value["reconcile"][0]["items"][0]["attrs"]["a"] = 3
    assert document_hash(parse_changeset(first)) != document_hash(parse_changeset(other_value))
    other_source = copy.deepcopy(first)
    other_source["source"]["run"] = "2026-11-18T10:00:00Z"
    assert document_hash(parse_changeset(first)) != document_hash(parse_changeset(other_source))


def test_the_tx_form_holds_the_frame_and_ops_only() -> None:
    cs = parse_changeset(
        reconcile_doc(
            full_scope(),
            reason="r",
            ops=[{"op": "create", "type": "ex:Package"}],
        )
    )
    assert changeset_to_json(cs) == {
        "schema": CHANGESET_SCHEMA,
        "reason": "r",
        "ops": [{"op": "create", "type": "ex:Package"}],
    }
    expanded = ChangeSet(ops=cs.ops, reason=cs.reason)
    assert changeset_hash(expanded) == changeset_hash(cs)  # the part the tx records is the same
    assert parse_changeset(changeset_to_json(cs)) == expanded


def test_a_document_without_source_or_reconcile_keeps_its_bytes() -> None:
    document = {
        "ops": [
            {
                "op": "create",
                "type": "ex:Thing",
                "local": "a",
                "attrs": {"n": 1},
                "edges": [{"rel": "ex:r", "target": ID}],
            },
            {"op": "retract", "id": "@a"},
        ],
        "reason": "r",
        "idempotency_key": "k1",
    }
    cs = parse_changeset(document)
    # computed by the code before the document grammar grew
    pinned = "sha256:bd6ef4ff13fffb18d2fabbbc8fcf70b6013d0ef08ddb7ec4138f21600e340898"
    assert changeset_hash(cs) == pinned
    assert document_hash(cs) == pinned
    assert document_to_json(cs) == changeset_to_json(cs)


def test_a_source_with_plain_ops_is_in_the_document_form_only() -> None:
    cs = parse_changeset(
        {"source": {"name": "inventory"}, "ops": [{"op": "create", "type": "ex:T"}]}
    )
    assert "source" not in changeset_to_json(cs)
    assert document_to_json(cs)["source"] == {"name": "inventory"}
    assert parse_changeset(document_to_json(cs)) == cs
