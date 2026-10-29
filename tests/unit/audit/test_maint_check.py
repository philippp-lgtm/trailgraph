"""Maintenance intents judged by their effects, the pin walk and graph-create anchors.

Contracts B5.6, B5.11, B5.12; plan M4 (P1 item 5).
"""

import pytest
from support.audit_events import ENV, REQUEST, allow, graph_entry, maint_actor, sealed_lines
from support.clock import StepClock
from support.ids import SeededIdProvider

from graph_cli.audit import events as builders
from graph_cli.audit import finding_codes as codes
from graph_cli.audit.schema import event_problems
from graph_cli.audit.verify import (
    InstalledSlots,
    MaintEffects,
    check_anchors,
    check_maintenance,
    effect_anchors,
    verify_graph,
)
from graph_cli.kernel.canonical import JsonValue, hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, GraphId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditLine, AuditReceipt, graph_stream
from graph_cli.kernel.records import BlobInfo, OntologyPin

Event = dict[str, JsonValue]


def pin(n: int) -> OntologyPin:
    return OntologyPin("example", f"1.{n}.0", hash_value({"pin": n}))


class Stream:
    """One graph stream built with the real builders: genesis at P0, then maintenance."""

    def __init__(self, *, genesis: bool = True) -> None:
        self.clock = StepClock()
        self.ids = SeededIdProvider(self.clock, 3)
        self.graph_id = GraphId(self.ids.new_id())
        self.drafts: list[AuditEventDraft] = []
        if genesis:
            self.drafts.append(
                builders.graph_genesis(
                    actor=maint_actor(),
                    decisions=[allow("maint.graph_create", "installation")],
                    request=REQUEST,
                    ts=self.clock.now(),
                    event_id=self.event_id(),
                    env=ENV,
                    graph=graph_entry(self.graph_id, pin(0)),
                )
            )

    def event_id(self) -> EventId:
        return EventId(self.ids.new_id())

    def maint(self, action: str, change: dict[str, JsonValue]) -> int:
        self.drafts.append(
            builders.maintenance(
                actor=maint_actor(),
                decisions=[allow(action, f"graph:{self.graph_id}")],
                request=REQUEST,
                ts=self.clock.now(),
                event_id=self.event_id(),
                env=ENV,
                graph=graph_entry(self.graph_id, pin(0)),
                change=change,
            )
        )
        return len(self.drafts)

    def pin(self, old: int, new: int) -> int:
        return self.maint("maint.ontology_pin", builders.pin_change(pin(old), pin(new)))

    def release(self, n: int) -> int:
        change = builders.release_change(pin(n).version, pin(n).artifact_hash, self.ids.new_id())
        return self.maint("maint.ontology_release", change)

    def events(self) -> list[Event]:
        return [dict(as_object(parse_json(line), "e")) for line in sealed_lines(self.drafts)]

    def check(
        self,
        manifest: int | None,
        *,
        releases: tuple[int, ...] = (),
        anchored: tuple[int, ...] = (),
    ) -> list[tuple[str, JsonValue]]:
        effects = MaintEffects(
            manifest_pin=None if manifest is None else pin(manifest),
            releases={pin(n).version: pin(n).artifact_hash for n in releases},
            anchored=frozenset(anchored),
        )
        findings = check_maintenance(self.graph_id, self.events(), effects)
        return [(f.code, f.target.get("seq", f.target.get("path"))) for f in findings]


IWE = codes.INTENT_WITHOUT_EFFECT
PIN = codes.PIN_MISMATCH


def test_no_pin_events_need_the_genesis_pin() -> None:
    stream = Stream()
    assert stream.check(0) == []
    assert stream.check(1) == [(PIN, "manifest.toml")]


@pytest.mark.parametrize(
    ("pins", "manifest", "anchored", "expected"),
    [
        ([(0, 1)], 1, (), []),
        ([(0, 1)], 0, (), [(IWE, 2)]),  # interrupted before the manifest rename
        ([(0, 1)], 0, (2,), [(PIN, "manifest.toml")]),  # anchored: it did switch
        ([(0, 1)], 5, (), [(PIN, "manifest.toml")]),
        ([(0, 1), (1, 2)], 2, (), []),
        ([(0, 1), (0, 2)], 2, (), [(IWE, 2)]),  # the first attempt died, the retry worked
        ([(0, 1), (0, 2)], 0, (), [(IWE, 2), (IWE, 3)]),
        ([(0, 1), (3, 4)], 4, (), [(PIN, 3)]),  # the manifest moved between the ops
        ([(5, 1)], 1, (), [(PIN, 2)]),  # the first op did not start at the genesis pin
        ([(0, 1), (0, 2)], 0, (2,), [(PIN, 3)]),  # anchored first op, second starts at 0
    ],
)
def test_pin_walk(
    pins: list[tuple[int, int]],
    manifest: int,
    anchored: tuple[int, ...],
    expected: list[tuple[str, JsonValue]],
) -> None:
    stream = Stream()
    for old, new in pins:
        stream.pin(old, new)
    assert stream.check(manifest, anchored=anchored) == expected


def test_no_manifest_pin_skips_the_walk() -> None:
    stream = Stream()
    stream.pin(3, 4)
    assert stream.check(None) == []


def test_without_genesis_the_first_op_sets_the_start() -> None:
    stream = Stream(genesis=False)
    stream.pin(7, 1)
    assert stream.check(1) == []
    assert stream.check(7) == [(IWE, 1)]


def test_release_intents() -> None:
    stream = Stream()
    stream.release(1)
    stream.release(2)
    stream.release(3)
    assert stream.check(None, releases=(1, 2, 3)) == []
    assert stream.check(None, releases=(1,), anchored=(3,)) == [(IWE, 4)]


def test_a_release_with_another_hash_is_no_effect() -> None:
    stream = Stream()
    stream.release(1)
    effects = MaintEffects(None, {pin(1).version: pin(2).artifact_hash})
    (finding,) = check_maintenance(stream.graph_id, stream.events(), effects)
    assert finding.code == IWE


def slot_check(
    key: str,
    published: list[tuple[str | None, str]],
    found: str | None,
    anchored: tuple[int, ...] = (),
) -> list[tuple[str, JsonValue]]:
    """The slot walk over ``change.<key>`` intents (seq 2, 3, ...) against the slot found;
    a mismatch of the slot itself is reported as ``"slot"``."""
    stream = Stream()
    for old, new in published:
        stream.maint("maint.graph_configure", {key: {"from": old, "to": new}})
    other = None
    slots = InstalledSlots(found, other) if key == "skill" else InstalledSlots(other, found)
    effects = MaintEffects(pin(0), {}, anchored=frozenset(anchored), slots=slots)
    findings = check_maintenance(stream.graph_id, stream.events(), effects)
    return [(f.code, "slot" if "path" in f.target else f.target.get("seq")) for f in findings]


A, B, C = (hash_value({"slot": n}) for n in "abc")
SLOT = codes.SLOT_MISMATCH


@pytest.mark.parametrize("key", ["skill", "functions"])
@pytest.mark.parametrize(
    ("published", "found", "anchored", "expected"),
    [
        ([], None, (), []),
        ([(None, A)], A, (), []),
        ([(None, A), (A, B)], B, (), []),
        ([(None, A), (A, A)], A, (), []),  # republished against a new pin: same files
        ([(None, A)], None, (), [(IWE, 2)]),  # interrupted before the rename
        ([(None, A)], None, (2,), [(SLOT, "slot")]),  # anchored: it did switch, then vanished
        ([(None, A), (A, B)], A, (), [(IWE, 3)]),
        ([(None, A), (A, B)], C, (), [(SLOT, "slot")]),  # replaced by hand
        ([], A, (), [(SLOT, "slot")]),  # placed by hand
        ([(None, A)], "unreadable", (), [(SLOT, "slot")]),
        ([(None, A), (C, B)], B, (), [(SLOT, 3)]),  # the second op did not start at the slot
        ([(None, A), (None, B)], A, (), [(SLOT, "slot")]),  # the last op recorded another hash
        ([(None, A), (None, A)], A, (), [(IWE, 2)]),  # the first attempt died, the retry worked
    ],
)
def test_slot_walk(
    key: str,
    published: list[tuple[str | None, str]],
    found: str | None,
    anchored: tuple[int, ...],
    expected: list[tuple[str, JsonValue]],
) -> None:
    assert slot_check(key, published, found, anchored) == expected


def test_without_slots_the_walk_is_skipped() -> None:
    stream = Stream()
    stream.maint("maint.graph_configure", {"skill": {"from": None, "to": A}})
    effects = MaintEffects(pin(0), {})
    assert check_maintenance(stream.graph_id, stream.events(), effects) == []


def test_verify_graph_runs_the_maintenance_check() -> None:
    stream = Stream()
    stream.pin(0, 1)
    lines = [AuditLine(f"memory:{n}", line) for n, line in enumerate(sealed_lines(stream.drafts))]
    effects = MaintEffects(pin(0), {})
    report = verify_graph(lines, graph_id=stream.graph_id, txs=None, effects=effects)
    assert [f.code for f in report.findings] == [IWE]
    assert verify_graph(lines, graph_id=stream.graph_id, txs=None).findings == ()


def test_change_shapes_are_checked() -> None:
    stream = Stream()
    with pytest.raises(GraphCliError, match=r"change\.pin"):
        stream.maint("maint.ontology_pin", {"pin": {"from": pin(0).to_json()}})
    with pytest.raises(GraphCliError, match=r"change\.release"):
        stream.maint("maint.ontology_release", {})
    stream.pin(0, 1)
    event = stream.events()[-1]
    event["change"] = {"pin": {"from": pin(0).to_json(), "to": "1.1.0"}}
    assert event_problems(event) == ["a pin intent needs change.pin{from, to}"]


# ---- installation anchors ------------------------------------------------------


def _create_event(stream: Stream, graph_id: GraphId) -> Event:
    receipt = AuditReceipt(
        graph_stream(str(graph_id)), stream.event_id(), 1, pin(9).artifact_hash, None
    )
    draft = builders.maintenance(
        actor=maint_actor(),
        decisions=[allow("maint.graph_create", "installation")],
        request=REQUEST,
        ts=stream.clock.now(),
        event_id=stream.event_id(),
        env=ENV,
        graph=graph_entry(graph_id, pin(0)),
        anchors=[receipt],
    )
    return dict(as_object(parse_json(sealed_lines([draft])[0]), "e"))


def test_graph_create_without_its_graph_is_an_interrupted_create() -> None:
    stream = Stream()
    missing = GraphId(stream.ids.new_id())
    (finding,) = check_anchors([_create_event(stream, missing)], {})
    assert (finding.code, finding.severity.value) == (IWE, "info")


def test_other_anchors_into_a_missing_graph_mismatch() -> None:
    stream = Stream()
    event = _create_event(stream, GraphId(stream.ids.new_id()))
    event["actions"] = ["maint.audit_anchor"]
    (finding,) = check_anchors([event], {})
    assert finding.code == codes.ANCHOR_MISMATCH


def test_effect_anchors() -> None:
    stream = Stream()
    first, second = GraphId(stream.ids.new_id()), GraphId(stream.ids.new_id())
    head = _create_event(stream, GraphId(stream.ids.new_id()))
    head["actions"] = ["maint.audit_verify"]  # anchors a stream head, proves no effect
    events = [_create_event(stream, first), _create_event(stream, second), head, {"anchors": "x"}]
    assert effect_anchors(events) == {str(first): frozenset({1}), str(second): frozenset({1})}


def test_file_removals_name_hashes_and_sizes_only() -> None:
    stream = Stream()
    blob = BlobInfo("sha256:" + "a" * 64, 9)
    stream.maint("maint.repair", builders.blobs_removed([blob], 2))
    stream.maint("maint.repair", builders.blobs_removed([], 1))
    assert stream.check(None) == []
    for change in (
        {"blobs_removed": [{"hash": blob.hash, "size": 9, "name": "a.pdf"}], "staging_removed": 0},
        {"blobs_removed": [], "staging_removed": -1},
        {"blobs_removed": [], "staging_removed": True},
        {"blobs_removed": [{"hash": blob.hash}]},
    ):
        with pytest.raises(GraphCliError):
            Stream().maint("maint.repair", change)  # type: ignore[arg-type]


def test_reindex_intents_name_the_seq() -> None:
    stream = Stream()
    stream.maint("maint.reindex", builders.reindexed(7))
    assert stream.check(None) == []
    for change in ({"reindex": {"seq": -1}}, {"reindex": {"seq": True}}, {"reindex": {}}):
        with pytest.raises(GraphCliError):
            Stream().maint("maint.reindex", change)  # type: ignore[arg-type]


# ---- enrollment codes replayed against their file (B14.21) ------------------------------


def _enrollment_events(*steps: tuple[str, dict[str, JsonValue], str]) -> list[Event]:
    return [
        {"seq": seq, "ts": ts, "actions": [action], "result": {"status": "ok"}, "change": change}
        for seq, (action, change, ts) in enumerate(steps, start=1)
    ]


CODE: dict[str, JsonValue] = {
    "id": "0123456789ab",
    "hash_prefix": "sha256:0123456789abcdef",
    "project": "p1",
    "scope": "app",
    "expires_at": "2026-10-25T12:15:00.000Z",
}
USED: dict[str, JsonValue] = {"id": "0123456789ab", "hash_prefix": "sha256:0123456789abcdef"}


def _token(project: str = "p1", **extra: JsonValue) -> dict[str, JsonValue]:
    return {
        "name": "p1.a",
        "hash_prefix": "sha256:fedcba9876543210",
        "project": project,
        "expires": None,
        **extra,
    }


def test_an_exchange_must_follow_from_its_code() -> None:
    from graph_cli.audit.verify import check_enrollments

    created = ("maint.token_create", {"enrollment": CODE}, "2026-10-25T12:00:00.000Z")
    good = (
        "maint.token_create",
        {"token": _token(), "enrollment": USED},
        "2026-10-25T12:01:00.000Z",
    )
    assert check_enrollments(_enrollment_events(created, good), {}) == []
    for steps in (
        (good,),  # a code never created
        (created, good, good),  # used twice
        (
            created,
            (
                "maint.token_create",
                {"token": _token("p2"), "enrollment": USED},
                "2026-10-25T12:01:00.000Z",
            ),
        ),
        (
            created,
            (
                "maint.token_create",
                {"token": _token(scope="admin"), "enrollment": USED},
                "2026-10-25T12:01:00.000Z",
            ),
        ),
        (
            created,
            (
                "maint.token_create",
                {"token": _token(), "enrollment": USED},
                "2026-10-25T12:15:00.000Z",
            ),
        ),
    ):
        found = check_enrollments(_enrollment_events(*steps), {})
        assert [f.code for f in found] == [codes.ENROLLMENT_MISMATCH], steps
