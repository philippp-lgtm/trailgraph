"""Revision and tx record JSON form and content hashes (contracts B4, C.9)."""

from dataclasses import replace
from uuid import UUID

import pytest
from support.clock import FixedClock

from graph_cli.kernel.actor import ABSENT, ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.canonical import GENESIS_HASH, hash_document, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, GraphId, TermRef, TxId
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.records import (
    ActionRef,
    EdgeRecord,
    OntologyPin,
    RequestMeta,
    Revision,
    TxRecord,
    revision_content_hash,
    revision_from_json,
    revision_meta,
    revision_to_json,
    strip_revision,
    tx_from_json,
    tx_to_json,
)

NOW = FixedClock("2026-10-14T19:21:05.123Z").now()


def uid(n: int) -> UUID:
    return UUID(f"0192f6d4-2a10-7b43-a5f6-{n:012x}")


def make_revision(**changes: object) -> Revision:
    base = Revision(
        id=EntityId(uid(1)),
        rev=1,
        prev_rev=None,
        type=TermRef("ex:Thing"),
        record_status="active",
        attrs={"name": "one", "count": 1},
        edges=(EdgeRecord(EdgeId(uid(2)), TermRef("ex:rel"), EntityId(uid(3))),),
        alias="THING-0001",
        valid_from=None,
        valid_to=None,
        created_at=NOW,
        recorded_at=NOW,
        tx_id=TxId(uid(4)),
        tx_seq=1,
        ontology_version="1.0.0",
        content_hash="",
    )
    revision = replace(base, **changes)  # type: ignore[arg-type]
    return replace(revision, content_hash=revision_content_hash(revision))


def test_revision_round_trip_omits_absent_optionals() -> None:
    revision = make_revision()
    data = revision_to_json(revision)
    assert data["schema"] == "graph_cli.revision/1"
    assert "prev_rev" not in data and "valid_from" not in data
    assert data["edges"] == [{"edge_id": str(uid(2)), "rel": "ex:rel", "target": str(uid(3))}]
    assert data["created_at"] == "2026-10-14T19:21:05.123Z"
    assert revision_from_json(data) == revision


def test_edge_qualifiers_and_target_rev_round_trip() -> None:
    edge = EdgeRecord(EdgeId(uid(5)), TermRef("ex:cites"), EntityId(uid(6)), {"page": "4"}, 2)
    assert EdgeRecord.from_json(edge.to_json()) == edge


def test_content_hash_excludes_tx_metadata() -> None:
    a = make_revision()
    b = make_revision(tx_id=TxId(uid(9)), tx_seq=7, rev=2, prev_rev=1)
    assert a.content_hash == b.content_hash
    assert make_revision(attrs={"count": 1, "name": "one"}).content_hash == a.content_hash
    assert make_revision(attrs={"name": "two"}).content_hash != a.content_hash


def test_strip_revision_drops_hidden_edges_keeps_the_rest_and_rehashes() -> None:
    keep = EdgeRecord(EdgeId(uid(2)), TermRef("ex:rel"), EntityId(uid(3)), {"page": "4"})
    drop = EdgeRecord(EdgeId(uid(5)), TermRef("ex:rel"), EntityId(uid(6)))
    last = EdgeRecord(EdgeId(uid(7)), TermRef("ex:other"), EntityId(uid(8)))
    revision = make_revision(edges=(keep, drop, last), rev=3, prev_rev=2, tx_seq=9)
    stripped = strip_revision(
        revision, lambda target: target != EntityId(uid(6)), None, rehash=True
    )
    assert stripped.edges == (keep, last)  # original order, ids and qualifiers
    expected = make_revision(edges=(keep, last), rev=3, prev_rev=2, tx_seq=9)
    assert stripped == expected  # including the recomputed content hash
    assert stripped.content_hash != revision.content_hash
    assert strip_revision(revision, lambda target: False, None, rehash=True).edges == ()


def test_strip_revision_returns_the_same_object_when_nothing_is_dropped() -> None:
    revision = make_revision()
    assert strip_revision(revision, lambda target: True, None, rehash=True) is revision
    assert strip_revision(revision, None, {"name", "count", "other"}, rehash=True) is revision
    assert strip_revision(revision, None, None, rehash=True) is revision
    bare = make_revision(edges=())
    assert strip_revision(bare, lambda target: False, None, rehash=True).edges == ()


def test_strip_revision_drops_attributes_outside_keep_in_the_same_copy() -> None:
    drop = EdgeRecord(EdgeId(uid(5)), TermRef("ex:rel"), EntityId(uid(6)))
    revision = make_revision(edges=(drop,), rev=2, prev_rev=1, tx_seq=4)
    stripped = strip_revision(revision, lambda target: False, {"name"}, rehash=True)
    expected = make_revision(edges=(), attrs={"name": "one"}, rev=2, prev_rev=1, tx_seq=4)
    assert stripped == expected  # edges and attributes in one copy, hash recomputed
    assert strip_revision(revision, None, frozenset(), rehash=True).attrs == {}
    assert strip_revision(revision, None, {"count"}, rehash=True).edges == (drop,)


def test_strip_revision_without_rehash_keeps_the_stored_hash() -> None:
    revision = make_revision()
    stripped = strip_revision(revision, None, {"name"}, rehash=False)
    assert stripped.attrs == {"name": "one"}
    assert stripped.content_hash == revision.content_hash  # stale: never handed out


def test_revision_meta() -> None:
    revision = make_revision()
    meta = revision_meta(revision)
    assert (meta.rev, meta.tx_seq, meta.record_status) == (1, 1, "active")


def test_unknown_record_status_and_schema_are_refused() -> None:
    data = revision_to_json(make_revision())
    for bad in ({**data, "record_status": "deleted"}, {**data, "schema": "graph_cli.revision/2"}):
        with pytest.raises(GraphCliError) as info:
            revision_from_json(bad)
        assert info.value.code == "format.unsupported_version"


def test_tx_round_trip_and_hash() -> None:
    actor = ActorContext(
        principal=Facet(Assurance.VERIFIED, id="os:1000", name="user", source="os"),
        agent=ABSENT,
        session=ABSENT,
        device=ABSENT,
        project=ABSENT,
        client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
        pid=None,
    )
    revision = make_revision()
    tx = TxRecord(
        graph_id=GraphId(uid(10)),
        tx_id=TxId(uid(4)),
        seq=1,
        recorded_at=NOW,
        event_id=EventId(uid(11)),
        actor=actor,
        ontology=OntologyPin("example", "1.0.0", "sha256:" + "a" * 64),
        request=RequestMeta("create", "sha256:" + "b" * 64),
        reason=None,
        idempotency_key="key-1",
        changeset={"schema": "graph_cli.changeset/1", "ops": []},
        changeset_hash="sha256:" + "c" * 64,
        resolution={"locals": {}, "refs": {}},
        revisions=(revision,),
        alias_allocations=((revision.id, "THING-0001"),),
        prev_hash=GENESIS_HASH,
        hash="",
    )
    data = tx_to_json(tx)
    tx = replace(tx, hash=hash_document(data))
    data = tx_to_json(tx)
    assert "reason" not in data
    assert data["alias_allocations"] == [{"entity": str(uid(1)), "alias": "THING-0001"}]
    assert tx_from_json(data) == tx
    assert hash_document(data) == tx.hash
    assert "action" not in data["request"]  # type: ignore[operator]
    named = replace(
        tx,
        request=RequestMeta(
            "do", "sha256:" + "b" * 64, ActionRef("move_item", "sha256:" + "d" * 64)
        ),
    )
    named_data = tx_to_json(named)
    assert named_data["request"]["action"] == {  # type: ignore[index]
        "name": "move_item",
        "hash": "sha256:" + "d" * 64,
    }
    assert tx_from_json(named_data) == named


def test_the_action_of_a_request_is_in_the_tx_record_only_when_there_is_one() -> None:
    plain = RequestMeta("create", "sha256:" + "b" * 64)
    assert plain.to_json() == {"command": "create", "args_digest": "sha256:" + "b" * 64}
    named = RequestMeta(
        "do", "sha256:" + "b" * 64, ActionRef("assign_position", "sha256:" + "d" * 64)
    )
    assert named.to_json() == {
        "command": "do",
        "args_digest": "sha256:" + "b" * 64,
        "action": {"name": "assign_position", "hash": "sha256:" + "d" * 64},
    }
    for meta in (plain, named):
        assert RequestMeta.from_json(meta.to_json()) == meta
    with pytest.raises(JsonShapeError):
        RequestMeta.from_json({"command": "do", "args_digest": "x", "action": {"name": "a"}})


def test_request_meta_digest_covers_command_and_args() -> None:
    meta = RequestMeta.of("get", {"graph": "g", "ref": "x"})
    assert meta.command == "get"
    assert meta.args_digest == hash_value({"command": "get", "args": {"graph": "g", "ref": "x"}})
    assert RequestMeta.of("get", {"ref": "x", "graph": "g"}) == meta
    assert RequestMeta.of("find", {"graph": "g", "ref": "x"}).args_digest != meta.args_digest
