"""Purge intents (plan T16, step 6): their shape, the keys they name, the tx they stand for."""

import dataclasses
from typing import Any
from uuid import UUID

import pytest
from support.audit_events import ENV, REQUEST, allow, graph_entry, maint_actor
from support.clock import StepClock
from support.ids import SeededIdProvider
from support.memlog import model
from support.txbuilder import FuncState, TxBuilder

from graph_cli.audit import events
from graph_cli.audit import finding_codes as codes
from graph_cli.audit.schema import is_tx_intent
from graph_cli.audit.verify import cross_check, purges
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, GraphId
from graph_cli.kernel.ports.storage import EMPTY_HEAD
from graph_cli.kernel.records import BlobInfo, TxRecord
from graph_cli.kernel.sealing import Kid

GRAPH = GraphId(UUID("01920000-0000-7000-8000-000000000001"))
ENTITY = EntityId(UUID("01920000-0000-7000-8000-000000000002"))
KIDS = [Kid(UUID(f"01920000-0000-7000-8000-00000000001{n}")) for n in range(3)]
BLOB = {"hash": "sha256:" + "b" * 64, "size": 9}


def pin() -> Any:
    clock = StepClock()
    return TxBuilder(
        model("inventory"), graph_id=GRAPH, ids=SeededIdProvider(clock, 3), clock=clock
    ).pin


def intent(change: dict[str, Any], event_id: str = "01920000-0000-7000-8000-000000000009") -> Any:
    clock = StepClock()
    return events.maintenance(
        actor=maint_actor(),
        decisions=[allow("maint.purge", f"graph:{GRAPH}")],
        request=REQUEST,
        ts=clock.now(),
        event_id=EventId(UUID(event_id)),
        env=ENV,
        graph=graph_entry(GRAPH, pin()),
        change=change,
        reason="ticket 1",
    ).body


def test_a_purge_intent_names_entities_keys_and_files() -> None:
    change = events.purged([ENTITY, ENTITY], [KIDS[1], KIDS[0]], [], None)
    assert change == {
        "purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[0]), str(KIDS[1])], "blobs": []}
    }
    body = intent(change)
    assert not is_tx_intent(body)
    assert purges([body]) == {EventId(UUID(body["event_id"])): [KIDS[0], KIDS[1]]}


@pytest.mark.parametrize(
    "change",
    [
        {"purge": {"entities": [], "kids": [str(KIDS[0])], "blobs": []}},
        {"purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[1]), str(KIDS[0])], "blobs": []}},
        {"purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[0])], "blobs": [BLOB, BLOB]}},
        {"purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[0])]}},
        {"purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[0])], "blobs": []}, "tx_seq": 3},
        {"purge": {"entities": [str(ENTITY)], "kids": [str(KIDS[0])], "blobs": []}, "extra": 1},
    ],
)
def test_a_malformed_purge_intent_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(GraphCliError) as caught:
        intent(change)
    assert caught.value.code == "internal.error"


def test_a_purge_intent_with_a_tx_is_its_intent() -> None:
    clock = StepClock()
    builder = TxBuilder(
        model("inventory"), graph_id=GRAPH, ids=SeededIdProvider(clock, 3), clock=clock
    )
    draft = builder.begin(FuncState(lambda _e: None, list), EMPTY_HEAD, {})
    draft.create()
    tx = draft.build()
    change = events.purged([ENTITY], KIDS, [BlobInfo(str(BLOB["hash"]), 9)], tx)
    body = intent(change, event_id=str(tx.event_id))
    assert is_tx_intent(body)  # the builder checked its shape
    record = TxRecord(
        graph_id=tx.graph_id,
        tx_id=tx.tx_id,
        seq=tx.seq,
        recorded_at=tx.recorded_at,
        event_id=tx.event_id,
        actor=tx.actor,
        ontology=tx.ontology,
        request=tx.request,
        reason=None,
        idempotency_key=None,
        changeset=tx.changeset,
        changeset_hash=tx.changeset_hash,
        resolution=tx.resolution,
        revisions=tx.revisions,
        alias_allocations=tx.alias_allocations,
        prev_hash="sha256:" + "0" * 64,
        hash="sha256:" + "1" * 64,
    )
    assert cross_check(GRAPH, [body], [record]) == []
    # a shredded change set reads empty: its size is not compared, its hash still is
    shredded = dataclasses.replace(record, changeset={}, shredded=True)
    assert cross_check(GRAPH, [body], [shredded]) == []
    forged = dataclasses.replace(record, changeset_hash="sha256:" + "f" * 64, shredded=True)
    assert [f.code for f in cross_check(GRAPH, [body], [forged])] == [codes.TX_WITHOUT_INTENT]
    # an intent whose tx was never committed: an interrupted purge
    assert [f.code for f in cross_check(GRAPH, [body], [])] == [codes.INTENT_WITHOUT_EFFECT]
