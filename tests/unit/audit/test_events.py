"""Audit event builders: routing, content rules, golden lines (contracts B5.2-B5.8, B5.11)."""

import dataclasses

import pytest
from support.audit_events import (
    ENV,
    REQUEST,
    allow,
    authorized,
    deny,
    graph_entry,
    intent,
    maint_actor,
    sealed_lines,
)
from support.clock import StepClock
from support.fixtures import GOLDENS, assert_golden
from support.ids import SeededIdProvider
from support.memlog import model
from support.txbuilder import FuncState, TxBuilder, actor

from graph_cli.audit import events
from graph_cli.audit.schema import event_problems
from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import canonical_bytes, hash_document, parse_json
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import EntityId, EventId, GraphId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditReceipt, graph_stream
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import EMPTY_HEAD, Head
from graph_cli.kernel.records import BlobInfo, Revision

MARKER = "MARKER-7f3a-attribute-value"


class World:
    """One graph with a create tx and an update tx on the inventory toy ontology."""

    def __init__(self) -> None:
        self.clock = StepClock()
        self.ids = SeededIdProvider(self.clock, 7)
        self.graph_id = GraphId(self.ids.new_id())
        self.builder = TxBuilder(
            model("inventory"), graph_id=self.graph_id, ids=self.ids, clock=self.clock
        )
        self.graph: GraphEntry = graph_entry(self.graph_id, self.builder.pin)
        self.revisions: dict[EntityId, Revision] = {}

    def tx(self, edit: object, head: Head = EMPTY_HEAD) -> AuthorizedTx:
        state = FuncState(self.revisions.get, lambda: sorted(self.revisions))
        draft = self.builder.begin(state, head, {})
        edit(draft)  # type: ignore[operator]
        tx = draft.build(reason="stock count")
        self.revisions.update({r.id: r for r in tx.revisions})
        return tx

    def event_id(self) -> EventId:
        return EventId(self.ids.new_id())

    def create_and_update(self) -> tuple[AuthorizedTx, AuthorizedTx, dict[EntityId, str]]:
        cls = self.builder.concrete_classes[0]
        text = self.builder.text_attribute(cls)
        assert text is not None
        first = self.tx(lambda d: d.create(cls, attrs={text: MARKER}))
        (entity,) = (r.id for r in first.revisions)
        before = {entity: self.revisions[entity].content_hash}
        head = Head(first.seq, first.tx_id, "sha256:" + "0" * 64, first.recorded_at)
        second = self.tx(lambda d: d.update(entity, {text: MARKER + "-2"}), head)
        return first, second, before


@pytest.fixture
def world() -> World:
    return World()


def _receipt(stream: str, seq: int) -> AuditReceipt:
    return AuditReceipt(graph_stream(stream), EventId(stream), seq, "sha256:" + "a" * 64, None)


def test_write_intent_summarizes_the_change(world: World) -> None:
    first, second, before = world.create_and_update()
    created = intent(first).body
    updated = intent(second, before=before).body
    assert created["stream"] == graph_stream(str(world.graph_id))
    assert created["actions"] == ["write.create"] and updated["actions"] == ["write.update"]
    (rev1,), (rev2,) = first.revisions, second.revisions
    assert created["change"] == {
        "tx_id": str(first.tx_id),
        "tx_seq": 1,
        "changeset_hash": first.changeset_hash,
        "changeset_size": len(canonical_bytes(dict(first.changeset))),
        "entities": [{"id": str(rev1.id), "rev": 1, "after": rev1.content_hash}],
    }
    change = as_object(updated["change"], "change")
    assert change["entities"] == [
        {"id": str(rev2.id), "rev": 2, "before": rev1.content_hash, "after": rev2.content_hash}
    ]
    assert created["target"] == {"entities": [str(rev1.id)], "types": [rev1.type]}
    assert created["ts"] == format_timestamp(first.recorded_at)
    assert created["reason"] == "stock count"
    assert created["actor"] == first.actor.to_json()  # B2.9: same serialization as the tx


def test_events_never_carry_attribute_values(world: World) -> None:
    """B5.8: marker strings seeded into attributes appear in no event."""
    first, second, before = world.create_and_update()
    assert MARKER.encode() in canonical_bytes(dict(first.revisions[0].attrs))
    assert MARKER.encode() in canonical_bytes(dict(second.changeset))
    for draft in (intent(first), intent(second, before=before)):
        assert MARKER.encode() not in canonical_bytes(dict(draft.body))


def test_write_intent_names_stored_files_by_hash_and_size(world: World) -> None:
    first, _, _ = world.create_and_update()
    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    blobs = [BlobInfo(b, 7), BlobInfo(a, 3), BlobInfo(b, 7)]
    body = events.write_intent(first, graph=world.graph, env=ENV, blobs=blobs).body
    change = as_object(body["change"], "change")
    assert change["blobs"] == [{"hash": a, "size": 3}, {"hash": b, "size": 7}]
    assert "blobs" not in as_object(intent(first).body["change"], "change")


@pytest.mark.parametrize(
    "blobs",
    [
        [],
        [{"hash": "sha256:" + "a" * 64}],
        [{"hash": "sha256:" + "a" * 64, "size": -1}],
        [{"hash": "sha256:" + "a" * 64, "size": True}],
        [{"hash": "sha256:" + "a" * 64, "size": 1, "name": "x.pdf"}],
        [{"hash": "sha256:" + "b" * 64, "size": 1}, {"hash": "sha256:" + "a" * 64, "size": 1}],
        "sha256:" + "a" * 64,
    ],
)
def test_malformed_blob_lists_are_refused(world: World, blobs: object) -> None:
    first, _, _ = world.create_and_update()
    body = dict(intent(first).body)
    body["change"] = {**as_object(body["change"], "change"), "blobs": blobs}  # type: ignore[dict-item]
    assert any("blobs?" in p for p in event_problems(body, sealed=False))


@pytest.mark.parametrize("size", [-1, True, "147", None])
def test_a_malformed_changeset_size_is_refused(world: World, size: object) -> None:
    first, _, _ = world.create_and_update()
    body = dict(intent(first).body)
    body["change"] = {**as_object(body["change"], "change"), "changeset_size": size}  # type: ignore[dict-item]
    assert any("changeset_size?" in p for p in event_problems(body, sealed=False))


def test_an_intent_without_a_changeset_size_keeps_its_shape(world: World) -> None:
    """Intents recorded before the size was added stay well-formed."""
    first, _, _ = world.create_and_update()
    body = dict(intent(first).body)
    change = dict(as_object(body["change"], "change"))
    del change["changeset_size"]
    body["change"] = change
    assert event_problems(body, sealed=False) == []


def test_updates_need_their_before_hash(world: World) -> None:
    _, second, _ = world.create_and_update()
    with pytest.raises(GraphCliError) as info:
        intent(second)
    assert info.value.code == "internal.error"


# ---- routing (B5.2): one case per table row ---------------------------------


def test_route_write_intent_invalid_and_conflict_to_the_graph(world: World) -> None:
    stream = graph_stream(str(world.graph_id))
    first, _, _ = world.create_and_update()
    assert intent(first).stream == stream
    for status in ("invalid", "conflict"):
        draft = events.refusal(
            status=status,  # type: ignore[arg-type]
            code="write.invalid" if status == "invalid" else "write.conflict",
            actor=actor(),
            decisions=[allow("write.create", f"graph:{world.graph_id}")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
        )
        assert draft.stream == stream


def test_route_denials_to_the_installation(world: World) -> None:
    for action in ("write.create", "read.get", "maint.repair"):
        draft = events.refusal(
            status="denied",
            code="access.denied",
            actor=actor(),
            decisions=[deny(action, f"graph:{world.graph_id}")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
        )
        assert draft.stream == INSTALLATION_STREAM
        assert as_object(draft.body["graph"], "graph")["id"] == str(world.graph_id)


def test_route_graph_create_to_installation_plus_genesis(world: World) -> None:
    decisions = [allow("maint.graph_create", "installation")]
    common = dict(
        actor=maint_actor(), decisions=decisions, request=REQUEST, env=ENV, graph=world.graph
    )
    genesis = events.graph_genesis(ts=world.clock.now(), event_id=world.event_id(), **common)  # type: ignore[arg-type]
    assert genesis.stream == graph_stream(str(world.graph_id))
    receipt = _receipt(str(world.graph_id), 1)
    created = events.maintenance(
        ts=world.clock.now(),
        event_id=world.event_id(),
        anchors=[receipt],
        **common,  # type: ignore[arg-type]
    )
    assert created.stream == INSTALLATION_STREAM
    assert created.body["anchors"] == [
        {"graph_id": str(world.graph_id), "seq": 1, "hash": receipt.hash}
    ]


@pytest.mark.parametrize(
    "action",
    [
        "maint.graph_configure",
        "maint.graph_archive",
        "maint.graph_restore",
        "maint.ontology_release",
        "maint.ontology_pin",
        "maint.migrate",
        "maint.repair",
        "maint.reindex",
        "maint.purge",
    ],
)
def test_route_graph_scoped_maint_to_the_graph_then_an_anchor(world: World, action: str) -> None:
    common = dict(
        actor=maint_actor(),
        decisions=[allow(action, f"graph:{world.graph_id}")],
        request=REQUEST,
        env=ENV,
        graph=world.graph,
    )
    pin = world.graph.pin
    change = {
        "maint.ontology_release": events.release_change("1.0.0", pin.artifact_hash, world.graph_id),
        "maint.ontology_pin": events.pin_change(pin, pin),
        "maint.graph_configure": events.skill_change(None, pin.artifact_hash),
    }.get(action)
    graph_event = events.maintenance(
        ts=world.clock.now(),
        event_id=world.event_id(),
        change=change,
        **common,  # type: ignore[arg-type]
    )
    assert graph_event.stream == graph_stream(str(world.graph_id))
    anchored = events.maintenance(
        ts=world.clock.now(),
        event_id=world.event_id(),
        anchors=[_receipt(str(world.graph_id), 9)],
        **common,  # type: ignore[arg-type]
    )
    assert anchored.stream == INSTALLATION_STREAM


@pytest.mark.parametrize(
    "action", ["maint.policy_change", "maint.audit_verify", "maint.audit_anchor"]
)
def test_route_installation_maint_to_the_installation(world: World, action: str) -> None:
    draft = events.maintenance(
        actor=maint_actor(),
        decisions=[allow(action, "installation")],
        request=REQUEST,
        ts=world.clock.now(),
        event_id=world.event_id(),
        env=ENV,
    )
    assert draft.stream == INSTALLATION_STREAM


# ---- refusals of the builders themselves ------------------------------------


def test_builders_refuse_bad_input(world: World) -> None:
    first, _, _ = world.create_and_update()
    other = graph_entry(GraphId(world.ids.new_id()), world.builder.pin)
    base = dict(actor=maint_actor(), request=REQUEST, ts=world.clock.now(), env=ENV)
    attempts = [
        lambda: events.write_intent(
            dataclasses.replace(first, decisions=()), graph=world.graph, env=ENV
        ),  # no decisions
        lambda: events.write_intent(authorized(first), graph=other, env=ENV),
        lambda: events.maintenance(
            decisions=[allow("maint.repair", "installation")], event_id=world.event_id(), **base
        ),  # graph-scoped without a graph
        lambda: events.maintenance(
            decisions=[allow("maint.audit_read", "installation")], event_id=world.event_id(), **base
        ),
        lambda: events.maintenance(
            decisions=[allow("write.create", "installation")], event_id=world.event_id(), **base
        ),
        lambda: events.maintenance(
            decisions=[allow("maint.teleport", "installation")], event_id=world.event_id(), **base
        ),
        lambda: events.maintenance(
            decisions=[
                allow("maint.audit_verify", "installation"),
                dataclasses.replace(allow("maint.audit_anchor", "installation"), policy_hash="x"),
            ],
            event_id=world.event_id(),
            **base,
        ),
        lambda: events.refusal(
            status="invalid",
            code="write.invalid",
            decisions=[allow("write.create", "installation")],
            event_id=world.event_id(),
            **base,
        ),  # invalid needs its graph
        lambda: events.anchor(AuditReceipt(INSTALLATION_STREAM, world.event_id(), 1, "h", None)),
    ]
    for attempt in attempts:
        with pytest.raises(GraphCliError) as info:
            attempt()  # type: ignore[no-untyped-call]
        assert info.value.code == "internal.error"


@pytest.mark.parametrize("key", ["skill", "functions"])
def test_slot_changes_name_both_tree_hashes(world: World, key: str) -> None:
    """D15 Q2, K6: a skill or function catalog publish intent carries ``change.<key>{from, to}``."""
    pin = world.graph.pin
    build = {"skill": events.skill_change, "functions": events.functions_change}[key]
    common = dict(
        actor=maint_actor(),
        decisions=[allow("maint.graph_configure", f"graph:{world.graph_id}")],
        request=REQUEST,
        env=ENV,
        graph=world.graph,
    )
    for change in (build(None, pin.artifact_hash), build(pin.artifact_hash, pin.artifact_hash)):
        assert set(change) == {key}
        events.maintenance(
            ts=world.clock.now(),
            event_id=world.event_id(),
            change=change,
            **common,  # type: ignore[arg-type]
        )
    for bad in (
        {key: {"from": None}},
        {key: {"from": "x", "to": pin.artifact_hash}},
        {key: {"from": None, "to": None}},
    ):
        with pytest.raises(GraphCliError) as caught:
            events.maintenance(
                ts=world.clock.now(),
                event_id=world.event_id(),
                change=bad,
                **common,  # type: ignore[arg-type]
            )
        assert f"change.{key}" in caught.value.message


def test_invalid_events_keep_twenty_findings(world: World) -> None:
    findings = [
        Finding(Severity.ERROR, "data.structural.missing_required", {"id": f"e{n:02d}"}, "m")
        for n in range(25)
    ]
    draft = events.refusal(
        status="invalid",
        code="write.invalid",
        actor=actor(),
        decisions=[allow("write.create", f"graph:{world.graph_id}")],
        request=REQUEST,
        ts=world.clock.now(),
        event_id=world.event_id(),
        env=ENV,
        graph=world.graph,
        findings=reversed(findings),
    )
    result = as_object(draft.body["result"], "result")
    assert result["findings"] == [{"code": f.code, "target": dict(f.target)} for f in findings[:20]]


# ---- golden lines and hash coverage (B5.3) ----------------------------------


def _golden_streams(world: World) -> tuple[list[bytes], list[bytes]]:
    first, second, before = world.create_and_update()
    graph = [intent(first), intent(second, before=before)]
    graph.append(
        events.maintenance(
            actor=maint_actor(),
            decisions=[allow("maint.reindex", f"graph:{world.graph_id}")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
        )
    )
    graph_lines = sealed_lines(graph)
    sealed = as_object(parse_json(graph_lines[-1]), "event")
    seq, digest = sealed["seq"], sealed["hash"]
    assert isinstance(seq, int) and isinstance(digest, str)
    installation = [
        events.refusal(
            status="denied",
            code="access.ceiling",
            actor=actor(),
            decisions=[deny("maint.repair", f"graph:{world.graph_id}", "access.ceiling")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
        ),
        events.maintenance(
            actor=maint_actor(),
            decisions=[allow("maint.reindex", f"graph:{world.graph_id}")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
            anchors=[
                AuditReceipt(graph_stream(str(world.graph_id)), world.event_id(), seq, digest, None)
            ],
        ),
    ]
    return graph_lines, sealed_lines(installation)


def test_golden_event_lines(world: World) -> None:
    graph_lines, installation_lines = _golden_streams(world)
    for line in (*graph_lines, *installation_lines):
        assert event_problems(as_object(parse_json(line), "event")) == []
    assert_golden(GOLDENS / "audit" / "graph_stream.jsonl", b"".join(graph_lines))
    assert_golden(GOLDENS / "audit" / "installation_stream.jsonl", b"".join(installation_lines))


def test_hash_covers_every_key(world: World) -> None:
    """B5.3: changing any key, ``stream`` and the chain fields included, changes the hash."""
    graph_lines, installation_lines = _golden_streams(world)
    for line in (graph_lines[1], installation_lines[1]):
        event = dict(as_object(parse_json(line), "event"))
        assert hash_document(event) == event["hash"]
        for key in event:
            if key == "hash":
                continue
            changed = {**event, key: "changed"}
            assert hash_document(changed) != event["hash"], key


def test_policy_changes_route_by_layer(world: World) -> None:
    """B5.15: the installation policy in the installation stream, a graph policy W.e."""
    digest = world.graph.pin.artifact_hash
    base = dict(
        actor=maint_actor(), request=REQUEST, env=ENV, change=events.policy_change(None, digest)
    )
    installation = events.maintenance(
        decisions=[allow("maint.policy_change", "installation")],
        ts=world.clock.now(),
        event_id=world.event_id(),
        **base,  # type: ignore[arg-type]
    )
    assert installation.stream == INSTALLATION_STREAM
    assert installation.body["change"] == {"policy": {"from": None, "to": digest}}
    graph_event = events.maintenance(
        decisions=[allow("maint.policy_change", f"graph:{world.graph_id}")],
        ts=world.clock.now(),
        event_id=world.event_id(),
        graph=world.graph,
        **base,  # type: ignore[arg-type]
    )
    assert graph_event.stream == graph_stream(str(world.graph_id))
    for bad in ({"policy": {"from": None}}, {"policy": {"from": "x", "to": digest}}):
        for graph in (None, world.graph):
            resource = "installation" if graph is None else f"graph:{world.graph_id}"
            with pytest.raises(GraphCliError) as caught:
                events.maintenance(
                    actor=maint_actor(),
                    decisions=[allow("maint.policy_change", resource)],
                    request=REQUEST,
                    ts=world.clock.now(),
                    event_id=world.event_id(),
                    env=ENV,
                    graph=graph,
                    change=bad,
                )
            assert "change.policy" in caught.value.message


def test_enrollment_changes_name_the_code_by_id(world: World) -> None:
    """B14.21: a code created or revoked names ``change.enrollment``; an exchange names the
    token it created and the enrollment it used."""
    digest = "sha256:" + "c" * 64
    created = events.enrollment_change(
        "0123456789ab", digest, project="p1", scope="app", expires_at="2026-10-25T12:00:00.000Z"
    )
    used = events.enrollment_change("0123456789ab", digest)
    token = events.token_change("p1.laptop", digest, created=True, project="p1")
    good = [
        ("maint.token_create", {"enrollment": created}),
        ("maint.token_revoke", {"enrollment": used}),
        ("maint.token_create", {**token, "enrollment": used}),
    ]
    bad = [
        ("maint.token_create", {"enrollment": used}),  # a created code names its project
        ("maint.token_revoke", {"enrollment": created}),
        (
            "maint.token_revoke",
            {**events.token_change("x", digest, created=False), "enrollment": used},
        ),
        ("maint.token_create", {"enrollment": {**created, "scope": "root"}}),
        ("maint.token_create", {}),
    ]

    def append(action: str, change: dict[str, object]) -> object:
        return events.maintenance(
            actor=maint_actor(),
            decisions=[allow(action, "installation")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            change=change,  # type: ignore[arg-type]
        )

    for action, change in good:
        assert append(action, change).stream == INSTALLATION_STREAM  # type: ignore[attr-defined]
    for action, change in bad:
        with pytest.raises(GraphCliError):
            append(action, change)
