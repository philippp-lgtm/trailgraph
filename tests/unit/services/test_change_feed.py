"""The change feed on the memory store (change feed plan section 7, step 2).

Through ``change_page`` on the store (a reader that sees ``nas:Note`` but not ``nas:Topic``):
a tx of hidden entities only has no entry and ``next`` passes it, a mixed tx shows its visible
part, a split tx resumes after the last entity delivered, forged cursor ids answer alike, a page
is empty with ``more`` after ``SCAN_CAP`` hidden txs, a migration always has an entry, and
``since_tx_id`` tells a log that was replaced. Through ``ReadService.changes``: ``--since`` above
the head and as a time, the limit, the ``view`` digest per view, marked attributes, the bound of
the opened snapshot, the tx-level keys and no read signal.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.markings import BADGE, HIDDEN_NAMES
from support.services import RecordingActivity, ServiceEnv
from support.stores import StoreEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, Facet
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read import GraphRead, ReadService, change_page
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.temporal_knowledge.changes import (
    SCAN_CAP,
    TARGET_CAP,
    FeedPosition,
    parse_since,
)

NOTE = "nas:Note"
TOPIC = "nas:Topic"  # hidden from the reader of the store tests
PAGE = "nas:WebPage"
ABOUT = "nas:about"
CITES = "nas:cites"
SEEN = frozenset({NOTE, PAGE})
NOTHING_CHANGED = {"attrs": [], "edges": {"added": [], "removed": [], "changed": []}, "record": []}
REQUEST = RequestMeta("changes", "sha256:" + "0" * 64)

Page = dict[str, Any]


@pytest.fixture
def env(tmp_path: Path) -> StoreEnv:
    return StoreEnv("memory", "notes_and_sources", tmp_path)


def migration_of(raw: Snapshot) -> Callable[[int], Mapping[str, JsonValue] | None]:
    """The migration record of a seq, read from the raw tx as the mapped layer reads it."""

    def record(seq: int) -> Mapping[str, JsonValue] | None:
        tx = raw.tx(seq)
        found = tx.changeset.get("migration") if tx is not None else None
        return found if isinstance(found, Mapping) else None

    return record


def feed(
    env: StoreEnv,
    since: str = "tx:0",
    *,
    limit: int = 100,
    cap: int = SCAN_CAP,
    types: frozenset[str] | None = SEEN,
) -> Page:
    """A page of the feed at the head, for a reader that sees ``types`` (all for ``None``)."""
    raw = env.store.snapshot()
    view = raw if types is None else VisibleSnapshot(raw, ReadView(types))
    start = parse_since(since)
    assert isinstance(start, FeedPosition)
    return change_page(
        view, raw, start, limit, session=None, migration=migration_of(raw), scan_cap=cap
    )


def entities(page: Page) -> list[tuple[str, int]]:
    return [(i["id"], i["rev"]) for entry in page["items"] for i in entry["entities"]]


def absent(page: Page, *texts: object) -> None:
    """None of ``texts`` appears anywhere in ``page``."""
    shown = json.dumps(page)
    assert [str(t) for t in texts if str(t) in shown] == []


def test_a_tx_of_hidden_entities_only_has_no_entry_and_next_passes_it(env: StoreEnv) -> None:
    ids: dict[str, EntityId] = {}
    env.write(lambda d: ids.update(note=d.create(NOTE)))
    env.write(lambda d: ids.update(topic=d.create(TOPIC)))
    last, _ = env.write(lambda d: d.update(ids["topic"]))
    page = feed(env)
    assert [entry["tx_seq"] for entry in page["items"]] == [1]
    assert (page["head"], page["next"], page["more"]) == (3, "tx:3", False)
    assert page["next_tx_id"] == str(last.tx_id)  # the id of a hidden tx tells only its seq
    assert (page["since"], page["since_tx_id"], page["view"]) == ("tx:0", None, None)
    absent(page, ids["topic"], TOPIC)
    # the reader caught up: nothing after tx 3, and nothing again from a position inside it
    assert feed(env, "tx:3")["items"] == [] and feed(env, "tx:2")["items"] == []
    assert [entry["tx_seq"] for entry in feed(env, types=None)["items"]] == [1, 2, 3]


def test_a_mixed_tx_shows_its_visible_part(env: StoreEnv) -> None:
    ids: dict[str, EntityId] = {}

    def mixed(d: Any) -> None:
        ids["topic"] = d.create(TOPIC)
        ids["page"] = d.create(PAGE)
        ids["note"] = d.create(NOTE, edges=[(ABOUT, ids["topic"]), (CITES, ids["page"])])

    env.write(mixed)
    env.write(
        lambda d: d.unlink(
            ids["note"], next(e for e in d.current(ids["note"]).edges if e.rel == ABOUT).edge_id
        )
    )
    page = feed(env)
    first, second = page["items"]
    assert [i["id"] for i in first["entities"]] == sorted([str(ids["note"]), str(ids["page"])])
    note = next(i for i in first["entities"] if i["id"] == str(ids["note"]))
    assert (note["type"], note["rev"], note["record_status"]) == (NOTE, 1, "active")
    assert (note["changed"], note["targets"]) == (None, [str(ids["page"])])
    assert "targets_more" not in note
    # the only change was an edge to a hidden entity: listed, with nothing named
    (unlinked,) = second["entities"]
    assert (unlinked["rev"], unlinked["changed"], unlinked["targets"]) == (2, NOTHING_CHANGED, [])
    absent(page, ids["topic"], TOPIC, ABOUT)
    full = feed(env, types=None)
    assert len(full["items"][0]["entities"]) == 3
    (removed,) = full["items"][1]["entities"]
    assert removed["changed"]["edges"]["removed"] == [ABOUT]
    assert removed["targets"] == [str(ids["topic"])]


def test_targets_are_capped_and_counted(env: StoreEnv) -> None:
    pages: list[EntityId] = []

    def many(d: Any) -> None:
        pages.extend(d.create(PAGE) for _ in range(TARGET_CAP + 2))
        d.create(NOTE, edges=[(CITES, p) for p in pages])

    env.write(many)
    item = next(i for i in feed(env)["items"][0]["entities"] if i["type"] == NOTE)
    assert item["targets"] == sorted(str(p) for p in pages)[:TARGET_CAP]
    assert item["targets_more"] == 2


def test_a_split_tx_resumes_after_the_last_entity_delivered(env: StoreEnv) -> None:
    notes: list[EntityId] = []

    def five_and_five(d: Any) -> None:
        for _ in range(5):
            notes.append(d.create(NOTE))
            d.create(TOPIC)

    env.write(lambda d: notes.append(d.create(NOTE)))
    env.write(five_and_five)
    whole = sorted(str(n) for n in notes[1:])
    # tx 2 does not fit behind tx 1: the page ends before it
    first = feed(env, limit=2)
    assert entities(first) == [(str(notes[0]), 1)]
    assert (first["next"], first["more"]) == ("tx:1", True)
    # alone on a page it is split, and each part resumes after the last entity delivered
    shown: list[str] = []
    page = feed(env, first["next"], limit=2)
    cursors = []
    while True:
        (entry,) = page["items"]
        assert entry["tx_seq"] == 2
        shown += [i["id"] for i in entry["entities"]]
        if not page["more"]:
            assert "partial" not in entry and page["next"] == "tx:2"
            break
        assert entry["partial"] is True
        assert page["next"] == f"tx:2/{shown[-1]}"
        cursors.append(page["next"])
        page = feed(env, page["next"], limit=2)
    assert shown == whole and len(cursors) == 2
    assert page["since_tx_id"] == page["next_tx_id"]  # inside tx 2 and after it: the same tx


def test_forged_cursor_ids_answer_alike(env: StoreEnv) -> None:
    notes: list[EntityId] = []
    topics: list[EntityId] = []

    def six_and_six(d: Any) -> None:
        for _ in range(6):
            notes.append(d.create(NOTE))
            topics.append(d.create(TOPIC))

    env.write(six_and_six)
    visible = sorted(notes)
    hidden = next(t for t in sorted(topics) if t > visible[0] and t < visible[-1])
    below = max(v for v in visible if v < hidden)  # the visible id just before the hidden one
    unknown = EntityId(UUID(int=hidden.int + 1))
    assert unknown not in (*notes, *topics) and not any(hidden < v < unknown for v in visible)
    answers = {}
    for name, entity in (("hidden", hidden), ("unknown", unknown), ("visible", below)):
        page = feed(env, f"tx:1/{entity}", limit=2)
        assert page.pop("since") == f"tx:1/{entity}"  # the caller's own text, canonical
        answers[name] = page
    assert answers["hidden"] == answers["unknown"] == answers["visible"]
    assert [i["id"] for i in answers["hidden"]["items"][0]["entities"]] == [
        str(v) for v in visible if v > hidden
    ][:2]
    absent(answers["hidden"], hidden, TOPIC)
    # beyond every visible id: nothing of the tx is left, for any id
    last = [
        feed(env, f"tx:1/{e}")
        for e in (max(topics + notes), EntityId(UUID(int=max(topics + notes).int + 1)))
    ]
    for page in last:
        page.pop("since")
        assert page["items"] == [] and page["next"] == "tx:1"
    assert last[0] == last[1]


def test_an_empty_page_says_more_after_the_scan_cap(env: StoreEnv) -> None:
    env.write(lambda d: d.create(NOTE))
    for _ in range(5):
        env.write(lambda d: d.create(TOPIC))
    env.write(lambda d: d.create(NOTE))
    page = feed(env, "tx:1", cap=3)
    assert (page["items"], page["next"], page["more"]) == ([], "tx:4", True)
    rest = feed(env, page["next"], cap=3)
    assert [entry["tx_seq"] for entry in rest["items"]] == [7]
    assert (rest["next"], rest["more"]) == ("tx:7", False)
    assert feed(env, "tx:1", cap=SCAN_CAP)["items"] == rest["items"]


def test_a_migration_always_has_an_entry(env: StoreEnv) -> None:
    ids: dict[str, EntityId] = {}
    env.write(lambda d: ids.update(note=d.create(NOTE), topic=d.create(TOPIC)))
    both, _ = env.migrate(lambda m: (m.revise(ids["note"]), m.revise(ids["topic"])))
    hidden_only, _ = env.migrate(lambda m: m.revise(ids["topic"]))
    page = feed(env, "tx:1")
    first, second = page["items"]
    migration = {"from": "1.0.0", "to": "2.0.0", "summary": "example migration"}
    for entry, tx in ((first, both), (second, hidden_only)):
        assert (entry["tx_seq"], entry["tx_id"]) == (tx.seq, str(tx.tx_id))
        assert (entry["command"], entry["reason"]) == ("ontology release", "example release")
        assert entry["migration"] == migration
    assert [(i["id"], i["rev"], i["changed"]) for i in first["entities"]] == [
        (str(ids["note"]), 2, NOTHING_CHANGED)
    ]
    assert second["entities"] == []  # the consumer's signal to re-read what it holds
    absent(page, ids["topic"], TOPIC)
    # the remainder of a migration tx delivered through its last visible entity has no entry
    assert feed(env, f"tx:{both.seq}/{ids['note']}")["items"] == [second]
    # a plain tx has no migration key
    assert "migration" not in feed(env)["items"][0]


def test_since_tx_id_tells_a_log_that_was_replaced(tmp_path: Path) -> None:
    first = StoreEnv("memory", "notes_and_sources", tmp_path / "first", seed=1)
    for _ in range(3):
        first.write(lambda d: d.create(NOTE))
    stored = feed(first)
    assert stored["next"] == "tx:3"
    assert feed(first, "tx:3")["since_tx_id"] == stored["next_tx_id"]
    # restored from an older backup and written again: the same seq names another tx
    second = StoreEnv("memory", "notes_and_sources", tmp_path / "second", seed=2)
    for _ in range(4):
        second.write(lambda d: d.create(NOTE))
    again = feed(second, "tx:3")
    assert again["since_tx_id"] != stored["next_tx_id"]
    assert again["since_tx_id"] is not None


# ---- through the service ---------------------------------------------------------------------


class Fixed:
    """A policy source with one fixed policy."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def hiding(svc: ServiceEnv, *types: str) -> Fixed:
    """The builtin policy plus a deny rule per hidden type."""
    denies: list[JsonValue] = [
        {
            "id": f"hide_{n}",
            "effect": "deny",
            "action": ["read.get"],
            "resource": f"graph:{svc.entry.id}/type:{t}",
        }
        for n, t in enumerate(types)
    ]
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], *denies], "obligations": []}  # type: ignore[misc]
    return Fixed(policy_set(document))


@pytest.fixture
def svc(tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv("memory", "notes_and_sources", tmp_path / "root")
    assert env.apply_seed().error is None
    return env


def open_view(svc: ServiceEnv, reader: ReadService, actor: ActorContext | None = None) -> GraphRead:
    return reader.open(actor or svc.actor, svc.catalog.resolve(svc.name), "read.history", REQUEST)


def refused(call: Callable[[], object]) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        call()
    return info.value


def test_since_above_the_head_and_since_a_time(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    head = view.snapshot.seq
    error = refused(lambda: reader.changes(view, FeedPosition(head + 1), limit=10))
    assert error.code == "usage.invalid_argument"
    assert error.message.startswith("--since: ")
    assert error.details == {"argument": "--since", "actual": f"tx:{head + 1}", "head_seq": head}
    some = EntityId(UUID("0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"))
    inside = refused(lambda: reader.changes(view, FeedPosition(head + 1, some), limit=10))
    assert inside.details == {**error.details, "actual": f"tx:{head + 1}/{some}"}
    assert reader.changes(view, FeedPosition(head), limit=10)["items"] == []
    txs = svc.txs()
    middle = txs[len(txs) // 2]
    for at, since in (
        (txs[0].recorded_at - timedelta(days=1), "tx:0"),
        (middle.recorded_at, f"tx:{middle.seq}"),
        (txs[-1].recorded_at + timedelta(days=1), f"tx:{head}"),
    ):
        page = reader.changes(view, at, limit=10)
        assert page["since"] == since
        assert page["since_tx_id"] == (
            None if since == "tx:0" else str(txs[int(since[3:]) - 1].tx_id)
        )


def test_the_limit_is_one_to_five_hundred(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    for limit in (0, 501):
        error = refused(lambda n=limit: reader.changes(view, FeedPosition(0), limit=n))
        assert (error.code, error.details["argument"]) == ("usage.invalid_argument", "--limit")
    assert len(entities(reader.changes(view, FeedPosition(0), limit=500))) <= 500


def test_the_view_digest_names_the_callers_view(svc: ServiceEnv) -> None:
    unlimited = svc.make_reader()
    assert unlimited.changes(open_view(svc, unlimited), FeedPosition(0), limit=5)["view"] is None
    digests = []
    for hidden in (TOPIC, NOTE):
        reader = svc.make_reader(policy=hiding(svc, hidden))
        page = reader.changes(open_view(svc, reader), FeedPosition(0), limit=500)
        assert page["view"] == reader.skill_digest(svc.actor, svc.entry) is not None
        assert all(i["type"] != hidden for entry in page["items"] for i in entry["entities"])
        absent(page, hidden)
        digests.append(page["view"])
    assert digests[0] != digests[1]


HOLDER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal, stf:pay]}
no_project: {"*": write}
"""


def test_marked_attributes_are_in_no_changed_list(tmp_path: Path) -> None:
    """Without an installation policy no app caller holds a marking: the plain reader sees no
    marked attribute; a holder wrote them."""
    svc = ServiceEnv("memory", "staff", tmp_path / "root")
    document = compile_policy(
        HOLDER.encode(), file="i.yaml", graphs={"example_graph": svc.entry.id}
    ).document
    writer = svc.make_service(Fixed(policy_set(document)))
    created = writer.apply(svc.request(*svc.factory.create(BADGE), reason="test"))
    badge = str(next(e.id for e in created.entities if e.local == "x"))
    for attrs in ({"holder_name": "Someone"}, {"number": 7}):
        writer.apply(svc.request({"op": "update", "id": badge, "set": attrs}, reason="test"))
    reader = svc.make_reader()
    page = reader.changes(open_view(svc, reader), FeedPosition(0), limit=50)
    items = [i for entry in page["items"] for i in entry["entities"] if i["id"] == badge]
    assert [(i["rev"], i["changed"] and i["changed"]["attrs"]) for i in items] == [
        (1, None),
        (2, []),  # only a marked attribute changed: listed, nothing named
        (3, ["number"]),
    ]
    assert page["view"] is not None
    absent(page, *HIDDEN_NAMES)
    full = svc.make_reader(policy=Fixed(policy_set(document)))
    shown = full.changes(open_view(svc, full), FeedPosition(0), limit=50)
    assert ["holder_name"] in [
        i["changed"] and i["changed"]["attrs"] for e in shown["items"] for i in e["entities"]
    ]


def test_the_feed_reads_up_to_the_snapshot_it_opened(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    head = view.snapshot.seq
    svc.create(NOTE)  # written after the snapshot was opened
    page = reader.changes(view, FeedPosition(head - 1), limit=500)
    assert (page["head"], page["next"], page["more"]) == (head, f"tx:{head}", False)
    assert [entry["tx_seq"] for entry in page["items"]] == [head]
    later = reader.changes(open_view(svc, reader), FeedPosition(head), limit=500)
    assert [entry["tx_seq"] for entry in later["items"]] == [head + 1]


def in_session(actor: ActorContext, session: str) -> ActorContext:
    return replace(actor, session=Facet(Assurance.ASSERTED, id=session))


def test_the_tx_keys_are_those_of_history_and_no_ids_are_signalled(svc: ServiceEnv) -> None:
    mine = in_session(svc.actor, "s-1")
    svc.apply(*svc.factory.create(NOTE), reason="why", actor=mine)
    activity = RecordingActivity()
    reader = svc.make_reader(activity=activity)
    head = svc.head_seq()
    for actor, own in ((mine, True), (svc.actor, False)):
        page = reader.changes(open_view(svc, reader, actor), FeedPosition(head - 1), limit=50)
        (entry,) = page["items"]
        assert {k: entry[k] for k in ("command", "reason", "own_session")} == {
            "command": "apply",
            "reason": "why",
            "own_session": own,
        }
        assert set(entry) == {
            "tx_seq",
            "tx_id",
            "recorded_at",
            "command",
            "reason",
            "own_session",
            "entities",
        }  # no actor beyond own_session (B8.15)
    assert activity.received == []
