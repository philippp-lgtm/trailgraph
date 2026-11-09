"""Sealed records (store format 2, plan T16): the codec at the record choke points."""

import json
from dataclasses import replace
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.ids import SeededIdProvider

from graph_cli.kernel.actor import ABSENT, ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.canonical import GENESIS_HASH, JsonValue, canonical_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, GraphId, TermRef, TxId
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.record_codec import RecordCodec, revision_aad, tx_aad
from graph_cli.kernel.records import (
    EdgeRecord,
    OntologyPin,
    RequestMeta,
    Revision,
    TxRecord,
    revision_content_hash,
    revision_to_json,
    tx_to_json,
)
from graph_cli.kernel.sealing import Kid, Sealed
from graph_cli.storage.memory.keyring import MemoryKeyRing

NOW = FixedClock("2026-11-04T10:00:00Z").now()
MARKER = "MARKER-5d1f"
GRAPH = GraphId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000aa"))


def uid(n: int) -> UUID:
    return UUID(f"0192f6d4-2a10-7b43-a5f6-{n:012x}")


def revision(entity: int = 1, **changes: object) -> Revision:
    base = Revision(
        id=EntityId(uid(entity)),
        rev=1,
        prev_rev=None,
        type=TermRef("ex:Thing"),
        record_status="active",
        attrs={"name": MARKER, "count": 1},
        edges=(
            EdgeRecord(EdgeId(uid(2)), TermRef("ex:rel"), EntityId(uid(3)), {"note": MARKER}),
            EdgeRecord(EdgeId(uid(5)), TermRef("ex:rel"), EntityId(uid(6))),
        ),
        alias="THING-0001",
        valid_from="2026-01-01",
        valid_to=None,
        created_at=NOW,
        recorded_at=NOW,
        tx_id=TxId(uid(4)),
        tx_seq=1,
        ontology_version="1.0.0",
        content_hash="sha256:" + "e" * 64,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def tx(*revisions: Revision) -> TxRecord:
    actor = ActorContext(
        principal=Facet(Assurance.VERIFIED, id="os:1000", name="user", source="os"),
        agent=ABSENT,
        session=ABSENT,
        device=ABSENT,
        project=ABSENT,
        client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
        pid=None,
    )
    return TxRecord(
        graph_id=GRAPH,
        tx_id=TxId(uid(4)),
        seq=1,
        recorded_at=NOW,
        event_id=EventId(uid(11)),
        actor=actor,
        ontology=OntologyPin("example", "1.0.0", "sha256:" + "a" * 64),
        request=RequestMeta("create", "sha256:" + "b" * 64),
        reason=f"because {MARKER}",
        idempotency_key="key-1",
        changeset={"schema": "graph_cli.changeset/1", "ops": [{"op": "create", "x": MARKER}]},
        changeset_hash="sha256:" + "c" * 64,
        resolution={"locals": {"x": str(uid(1))}, "refs": {}},
        revisions=revisions or (revision(),),
        alias_allocations=((EntityId(uid(1)), "THING-0001"),),
        prev_hash=GENESIS_HASH,
        hash="sha256:" + "d" * 64,
    )


@pytest.fixture
def codec() -> RecordCodec:
    return RecordCodec(GRAPH, MemoryKeyRing(SeededIdProvider(FixedClock(NOW), 3)))


def reparse(codec: RecordCodec, record: TxRecord) -> TxRecord:
    return codec.parse_tx(as_object(json.loads(json.dumps(tx_to_json(record))), "tx"))


def test_a_sealed_tx_keeps_structure_in_the_clear_and_values_sealed(codec: RecordCodec) -> None:
    sealed = codec.seal(tx())
    data = tx_to_json(sealed)
    text = json.dumps(data)
    assert MARKER not in text
    assert data["schema"] == "graph_cli.tx/2"
    assert {"changeset", "resolution", "reason"}.isdisjoint(data)
    assert data["idempotency_key"] == "key-1"  # documented: carries no data
    (stored,) = data["revisions"]  # type: ignore[misc]
    assert stored["schema"] == "graph_cli.revision/2"
    assert {"attrs", "valid_from", "valid_to"}.isdisjoint(stored)
    assert stored["alias"] == "THING-0001"
    assert all("qualifiers" not in edge for edge in stored["edges"])
    assert reparse(codec, sealed) == tx()


def test_a_revision_serializes_to_the_bytes_it_was_stored_with(codec: RecordCodec) -> None:
    """Revision files must equal the tx copy, so a record never seals twice."""
    stored = codec.seal(tx()).revisions[0]
    again = codec.parse_revision(as_object(json.loads(json.dumps(revision_to_json(stored))), "r"))
    assert again == stored and again.sealed == stored.sealed
    assert revision_to_json(again) == revision_to_json(stored)
    resealed = codec.seal(tx()).revisions[0]
    assert resealed.sealed != stored.sealed  # fresh nonces: sealing is not deterministic


def test_a_plain_codec_leaves_records_as_they_are() -> None:
    plain = RecordCodec(GRAPH)
    record = tx()
    assert plain.seal(record) is record
    assert tx_to_json(record)["schema"] == "graph_cli.tx/1"
    assert reparse(plain, record) == record
    assert plain.revision_mac(record.revisions[0]) is None


def test_each_codec_reads_only_its_own_schema(codec: RecordCodec) -> None:
    plain = RecordCodec(GRAPH)
    with pytest.raises(GraphCliError) as info:
        reparse(codec, tx())
    assert info.value.code == "format.unsupported_version"
    with pytest.raises(GraphCliError) as info:
        reparse(plain, codec.seal(tx()))
    assert info.value.code == "format.unsupported_version"


def test_a_shredded_key_reads_as_shredded_with_the_structure_kept(codec: RecordCodec) -> None:
    sealed = codec.seal(tx())
    entity_kid = sealed.revisions[0].sealed.kid  # type: ignore[union-attr]
    tx_kid = sealed.sealed.kid  # type: ignore[union-attr]
    codec.ring.shred([entity_kid, tx_kid], EventId(uid(99)))
    read = reparse(codec, sealed)
    assert read.shredded and read.changeset == {} and read.reason is None
    (gone,) = read.revisions
    assert gone.shredded and gone.attrs == {} and gone.valid_from is None
    assert [e.qualifiers for e in gone.edges] == [{}, {}]
    assert (gone.alias, gone.record_status, [e.target for e in gone.edges]) == (
        "THING-0001",
        "active",
        [EntityId(uid(3)), EntityId(uid(6))],
    )
    assert tx_to_json(read) == tx_to_json(sealed)  # the stored bytes are unchanged


def test_an_envelope_moved_to_another_place_does_not_open(codec: RecordCodec) -> None:
    first = codec.seal(tx()).revisions[0]
    other = codec.seal(tx(revision(entity=7, alias=None))).revisions[0]
    data = revision_to_json(other)
    data["sealed"] = first.sealed.to_json()  # type: ignore[union-attr]
    with pytest.raises(GraphCliError) as info:
        codec.parse_revision(data)
    assert info.value.code == "backend.corrupt"
    bumped = revision_to_json(first)
    bumped["rev"] = 2
    with pytest.raises(GraphCliError) as info:
        codec.parse_revision(bumped)
    assert info.value.code == "backend.corrupt"


def test_a_missing_key_is_damage_not_a_shred(codec: RecordCodec) -> None:
    data = revision_to_json(codec.seal(tx()).revisions[0])
    stranger = RecordCodec(GRAPH, MemoryKeyRing(SeededIdProvider(FixedClock(NOW), 4)))
    with pytest.raises(GraphCliError) as info:
        stranger.parse_revision(data)
    assert info.value.code == "backend.corrupt"
    assert info.value.details["reason"] == "missing"


def test_qualifiers_in_the_clear_part_are_refused(codec: RecordCodec) -> None:
    data = revision_to_json(codec.seal(tx()).revisions[0])
    edges = data["edges"]
    assert isinstance(edges, list) and isinstance(edges[0], dict)
    edges[0]["qualifiers"] = {"note": "x"}
    with pytest.raises(JsonShapeError):  # storage turns it into backend.corrupt
        codec.parse_revision(data)


def test_content_hashes_are_keyed_under_the_entity_key(codec: RecordCodec) -> None:
    record = revision()
    mac = codec.entity_mac(record.id)
    assert mac is not None
    keyed = revision_content_hash(record, mac)
    assert keyed != revision_content_hash(record)
    stored = codec.seal(tx(replace(record, content_hash=keyed))).revisions[0]
    assert codec.revision_mac(stored) == mac
    assert revision_content_hash(stored, codec.revision_mac(stored)) == stored.content_hash


def test_a_request_is_keyed_again_from_its_arguments() -> None:
    args: dict[str, JsonValue] = {"graph": "example_graph", "name": MARKER}
    plain = RequestMeta.of("create", args)
    key = bytes(32)
    assert plain.keyed(None) == plain
    assert plain.keyed(key) == RequestMeta.of("create", args, key=key)
    assert plain.keyed(key).args_digest != plain.args_digest
    with pytest.raises(ValueError):
        RequestMeta.from_json(plain.to_json()).keyed(key)


def test_sealed_payloads_are_never_part_of_equality() -> None:
    record = revision()
    envelope = Sealed(Kid(uid(8)), b"\x00" * 12, b"\x01" * 16)  # equality ignores it
    assert replace(record, sealed=envelope) == record


def test_the_associated_data_is_the_jcs_form_of_the_place() -> None:
    entity, tx_id = EntityId(uid(1)), TxId(uid(4))
    assert revision_aad(GRAPH, entity, 12) == canonical_bytes(
        {"graph": str(GRAPH), "record": "revision", "id": str(entity), "rev": 12}
    )
    assert tx_aad(GRAPH, tx_id, 3) == canonical_bytes(
        {"graph": str(GRAPH), "record": "tx", "id": str(tx_id), "seq": 3}
    )
