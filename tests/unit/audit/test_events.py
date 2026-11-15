"""Audit event builders: routing, content rules, golden lines (contracts B5.2-B5.8, B5.11)."""

import dataclasses
from collections.abc import Callable

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
from support.memlog import MemLog, model
from support.txbuilder import FuncState, TxBuilder, actor

from graph_cli.audit import events
from graph_cli.audit.schema import event_problems
from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import (
    JsonValue,
    canonical_bytes,
    hash_document,
    hash_value,
    parse_json,
)
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


def _migration_change() -> dict[str, JsonValue]:
    """The ``change`` of a ``maint.migrate`` intent of a tx on the memory log."""
    log = MemLog("inventory")
    draft = log.draft()
    entity = draft.create()
    log.commit(draft.build())
    migration = log.migration()
    migration.revise(entity)
    tx = migration.build()
    return events.migration_change(
        tx, revisions_hash=events.revisions_hash(tx.revisions), counts={"entities": 1, "edges": 0}
    )


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
        "maint.migrate": _migration_change(),
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


@pytest.mark.parametrize("key", ["skill", "functions", "actions"])
def test_slot_changes_name_both_tree_hashes(world: World, key: str) -> None:
    """D15 Q2, K6: a skill or function catalog publish intent carries ``change.<key>{from, to}``."""
    pin = world.graph.pin
    catalog = events.functions_change if key == "functions" else events.actions_change
    build: Callable[[str | None, str], dict[str, JsonValue]] = (
        events.skill_change
        if key == "skill"
        else lambda old, new: catalog(old, new, pin.artifact_hash)
    )
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
        *([{key: {"from": None, "to": pin.artifact_hash}}] if key != "skill" else []),
    ):
        with pytest.raises(GraphCliError) as caught:
            events.maintenance(
                ts=world.clock.now(),
                event_id=world.event_id(),
                change=bad,
                **common,  # type: ignore[arg-type]
            )
        assert f"change.{key}" in caught.value.message


STORE_ID = "4b0c9f3e-2d1a-4c5b-9e8f-7a6b5c4d3e2f"


def test_a_blob_store_switch_names_both_locations(world: World) -> None:
    """Object storage O7: ``change.blob_store{from, to, store_id, blobs, bytes}`` of a
    ``maint.graph_configure`` intent; the schema refuses other shapes."""
    common = dict(
        actor=maint_actor(),
        decisions=[allow("maint.graph_configure", f"graph:{world.graph_id}")],
        request=REQUEST,
        env=ENV,
        graph=world.graph,
    )
    change = events.blob_store_change("local", "s3", STORE_ID, blobs=0, size=0)
    assert change == {
        "blob_store": {"from": "local", "to": "s3", "store_id": STORE_ID, "blobs": 0, "bytes": 0}
    }
    back = events.blob_store_change("s3", "local", STORE_ID, blobs=3, size=1024)
    for good in (change, back):
        draft = events.maintenance(
            ts=world.clock.now(),
            event_id=world.event_id(),
            change=good,
            **common,  # type: ignore[arg-type]
        )
        assert draft.body["change"] == good
    valid = change["blob_store"]
    assert isinstance(valid, dict)
    for bad in (
        {**valid, "to": "local"},  # no switch
        {**valid, "to": "gcs"},
        {**valid, "store_id": "not-a-uuid"},
        {**valid, "store_id": STORE_ID.upper()},
        {**valid, "store_id": None},
        {**valid, "blobs": -1},
        {**valid, "bytes": True},
        {k: v for k, v in valid.items() if k != "bytes"},
        {**valid, "path": "/somewhere"},
    ):
        with pytest.raises(GraphCliError) as caught:
            events.maintenance(
                ts=world.clock.now(),
                event_id=world.event_id(),
                change={"blob_store": bad},
                **common,  # type: ignore[arg-type]
            )
        assert "change.blob_store" in caught.value.message
    with pytest.raises(GraphCliError) as refused:
        events.blob_store_change("local", "local", STORE_ID, blobs=0, size=0)
    assert refused.value.code == "internal.error"


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


def test_token_events_carry_roles(world: World) -> None:
    """B14.11: a created token and a created code name their roles when they have any; an
    update names the token's full role set after the change, possibly empty."""
    digest = "sha256:" + "c" * 64
    prefix = digest[:23]
    plain = events.token_change("ci", digest, created=True, project="p1")
    assert "roles" not in plain["token"]  # type: ignore[operator]
    held = events.token_change("ci", digest, created=True, roles=("reader",))
    assert held == {
        "token": {
            "name": "ci",
            "hash_prefix": prefix,
            "project": None,
            "expires": None,
            "roles": ["reader"],
        }
    }
    assert events.token_update_change("ci", digest, ()) == {
        "token": {"name": "ci", "hash_prefix": prefix, "roles": []}
    }
    code = events.enrollment_change(
        "0123456789ab",
        digest,
        project="p1",
        scope="app",
        expires_at="2026-10-25T12:00:00.000Z",
        roles=("reader",),
    )
    assert code["roles"] == ["reader"]
    used = events.enrollment_change("0123456789ab", digest)
    exchanged = events.token_change("p1.a", digest, created=True, project="p1", roles=("reader",))

    def update(**token: object) -> dict[str, object]:
        return {"token": {"name": "ci", "hash_prefix": prefix, **token}}

    good: list[tuple[str, dict[str, object]]] = [
        ("maint.token_create", held),
        ("maint.token_create", {"enrollment": code}),
        ("maint.token_create", {**exchanged, "enrollment": used}),
        ("maint.token_update", events.token_update_change("ci", digest, ("importer", "reader"))),
        ("maint.token_update", events.token_update_change("ci", digest, ())),
    ]
    many = [f"role_{n:02d}" for n in range(17)]
    bad: list[tuple[str, dict[str, object]]] = [
        # an update names the full role set, and nothing else
        ("maint.token_update", update()),
        ("maint.token_update", update(roles=["b", "a"])),
        ("maint.token_update", update(roles=["a", "a"])),
        ("maint.token_update", update(roles=["Bad"])),
        ("maint.token_update", update(roles="a")),
        ("maint.token_update", update(roles=["a"], project=None)),
        ("maint.token_update", {"enrollment": used}),
        ("maint.token_update", {}),
        # a created token or code lists roles only when it has some, sorted, at most 16
        ("maint.token_create", {"token": {**plain["token"], "roles": []}}),  # type: ignore[dict-item]
        ("maint.token_create", {"token": {**held["token"], "roles": many}}),  # type: ignore[dict-item]
        ("maint.token_create", {"enrollment": {**code, "roles": []}}),
        ("maint.token_create", {"enrollment": {**code, "roles": ["b", "a"]}}),
        ("maint.token_revoke", update(roles=["a"])),
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


# ---- migration events and sibling keys (plan M5) -------------------------------


def test_release_and_pin_changes_are_byte_identical_without_the_sibling_keys(
    world: World,
) -> None:
    pin = world.graph.pin
    release_id = world.graph_id
    assert events.release_change("1.0.0", pin.artifact_hash, release_id) == {
        "release": {
            "version": "1.0.0",
            "artifact_hash": pin.artifact_hash,
            "release_id": str(release_id),
        }
    }
    assert events.release_change("1.0.0", pin.artifact_hash, release_id, None) == (
        events.release_change("1.0.0", pin.artifact_hash, release_id)
    )
    plain = events.pin_change(pin, pin)
    assert plain == {"pin": {"from": pin.to_json(), "to": pin.to_json()}}
    assert events.pin_change(pin, pin, None) == plain
    lines = [
        canonical_bytes(events.release_change("1.0.0", pin.artifact_hash, release_id)),
        canonical_bytes(plain),
    ]
    assert all(b"migration_hash" not in line and b"alias_prefixes" not in line for line in lines)


def test_the_sibling_keys_sit_beside_release_and_pin_never_inside(world: World) -> None:
    pin = world.graph.pin
    release = events.release_change(
        "2.0.0", pin.artifact_hash, world.graph_id, "sha256:" + "a" * 64
    )
    assert set(release) == {"release", "migration_hash"}
    assert set(release["release"]) == {"version", "artifact_hash", "release_id"}  # type: ignore[call-overload]
    prefixes: dict[str, JsonValue] = {
        "added": {},
        "moved": {"area_b:Thing": {"from": "area_a:Thing", "prefix": "TH"}},
    }
    moved = events.pin_change(pin, pin, prefixes)
    assert set(moved) == {"pin", "alias_prefixes"}
    assert set(moved["pin"]) == {"from", "to"}  # type: ignore[call-overload]
    assert moved["alias_prefixes"] == prefixes
    for change in (release, moved):  # a builder refuses nothing here: the event is checked whole
        draft = events.maintenance(
            actor=maint_actor(),
            decisions=[
                allow(
                    "maint.ontology_pin" if "pin" in change else "maint.ontology_release", "graph"
                )
            ],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
            change=change,
        )
        assert draft.body["change"] == change


def test_a_malformed_sibling_key_is_refused_by_the_builder(world: World) -> None:
    pin = world.graph.pin
    with pytest.raises(GraphCliError) as info:
        events.maintenance(
            actor=maint_actor(),
            decisions=[allow("maint.ontology_pin", "graph")],
            request=REQUEST,
            ts=world.clock.now(),
            event_id=world.event_id(),
            env=ENV,
            graph=world.graph,
            change=events.pin_change(pin, pin, {"added": {}}),
        )
    assert info.value.code == "internal.error"


def test_revisions_hash_is_order_independent_and_covers_id_rev_and_content(
    world: World,
) -> None:
    first, second, _ = world.create_and_update()
    revisions = [*first.revisions, *second.revisions]
    digest = events.revisions_hash(revisions)
    assert digest == events.revisions_hash(reversed(revisions))
    expected = hash_value(
        [
            [str(r.id), r.rev, r.content_hash]
            for r in sorted(revisions, key=lambda r: (str(r.id), r.rev))
        ]
    )
    assert digest == expected
    changed = dataclasses.replace(revisions[0], content_hash="sha256:" + "0" * 64)
    assert events.revisions_hash([changed, *revisions[1:]]) != digest
    assert (
        events.revisions_hash([dataclasses.replace(revisions[0], rev=9), *revisions[1:]]) != digest
    )
    assert events.revisions_hash(revisions[1:]) != digest


def test_migration_change_names_the_tx_and_nothing_per_entity() -> None:
    log = MemLog("inventory")
    draft = log.draft()
    entities = [draft.create(), draft.create()]
    log.commit(draft.build())
    migration = log.migration()
    for entity in entities:
        migration.revise(entity)
    tx = migration.build()
    change = events.migration_change(
        tx, revisions_hash=events.revisions_hash(tx.revisions), counts={"entities": 2, "edges": 5}
    )
    record = tx.changeset["migration"]
    assert isinstance(record, dict)
    assert change == {
        "migration": {
            "tx_id": str(tx.tx_id),
            "tx_seq": tx.seq,
            "changeset_hash": tx.changeset_hash,
            "changeset_size": events.changeset_size(tx.changeset),
            "from": record["from"],
            "to": record["to"],
            "migration_hash": record["migration_hash"],
            "revisions_hash": events.revisions_hash(tx.revisions),
            "counts": {"entities": 2, "edges": 5},
        }
    }
    assert all(str(e) not in str(change) for e in entities)
    committed = log.commit(tx, migration=True)  # the record of the commit names the same
    assert (
        events.migration_change(
            committed,
            revisions_hash=events.revisions_hash(committed.revisions),
            counts={"entities": 2, "edges": 5},
        )
        == change
    )


def test_the_migration_intent_routes_to_the_graph_stream_with_the_tx_event_id(
    world: World,
) -> None:
    log = MemLog("inventory")
    draft = log.draft()
    entity = draft.create()
    log.commit(draft.build())
    migration = log.migration()
    migration.revise(entity)
    tx = migration.build()
    draft_event = events.maintenance(
        actor=maint_actor(),
        decisions=[allow("maint.migrate", f"graph:{log.graph_id}")],
        request=REQUEST,
        ts=world.clock.now(),
        event_id=tx.event_id,
        env=ENV,
        graph=graph_entry(log.graph_id, tx.ontology),
        change=events.migration_change(
            tx,
            revisions_hash=events.revisions_hash(tx.revisions),
            counts={"entities": 1, "edges": 0},
        ),
    )
    assert draft_event.stream == graph_stream(str(log.graph_id))
    assert draft_event.body["event_id"] == str(tx.event_id)
    assert draft_event.body["actions"] == ["maint.migrate"]


@pytest.mark.parametrize(
    "counts",
    [{"entities": 1}, {"entities": -1, "edges": 0}, {"entities": 1, "edges": None}],
)
def test_migration_change_refuses_counts_that_do_not_fit(counts: dict[str, int]) -> None:
    log = MemLog("inventory")
    draft = log.draft()
    entity = draft.create()
    log.commit(draft.build())
    migration = log.migration()
    migration.revise(entity)
    tx = migration.build()
    with pytest.raises(GraphCliError) as info:
        events.migration_change(
            tx, revisions_hash=events.revisions_hash(tx.revisions), counts=counts
        )
    assert info.value.code == "internal.error"


def test_migration_change_refuses_a_tx_that_is_not_a_migration(world: World) -> None:
    first, _, _ = world.create_and_update()
    with pytest.raises(GraphCliError):
        events.migration_change(
            first, revisions_hash="sha256:" + "0" * 64, counts={"entities": 1, "edges": 0}
        )
