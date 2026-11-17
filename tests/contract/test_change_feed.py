"""Projection equivalence of the change feed (change feed plan section 7, step 2), on both backends.

A consumer that follows the feed from ``tx:0`` and records ``(id, rev)`` per entity item ends
with exactly the visible entities and their current revisions at the head, and has seen every
revision of them exactly once. Writes happen between two pages: each page reads a fresh
snapshot, so what was written after a page is on a later one, never lost and never twice.

Through the stores (random transactions of the toy fixtures, random visible types and
attributes, random limits and scan caps) and through ``ReadService.changes`` (a policy that
hides a type, writes through the write service); the service answers also agree with
``history`` for every entity item.
"""

import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.services import ServiceEnv
from support.stores import ALL_STATUSES, StoreEnv
from support.txbuilder import TxDraft

from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.read_view import AttributeMask, ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read import GraphRead, ReadService, change_page
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.storage.files.store import FileStore
from graph_cli.temporal_knowledge.changes import SCAN_CAP, FeedPosition, parse_since

CASES = 12  # graphs per backend and toy ontology
LIMITS = (1, 2, 3, 7, 500)
CAPS = (1, 2, 5, SCAN_CAP)
REQUEST = RequestMeta("changes", "sha256:" + "0" * 64)

Pairs = list[tuple[str, int]]


def position(text: JsonValue) -> FeedPosition:
    found = parse_since(str(text))
    assert isinstance(found, FeedPosition)
    return found


def received(page: Any) -> Pairs:
    """``(id, rev)`` of every entity item of ``page``, in order."""
    return [(item["id"], item["rev"]) for entry in page["items"] for item in entry["entities"]]


def visible_state(
    view: Snapshot, ids: list[EntityId]
) -> tuple[dict[str, int], set[tuple[str, int]]]:
    """The current revision of every entity ``view`` shows, and every revision of them."""
    current = {
        str(r.id): r.rev for r in view.query(Query(record_status=ALL_STATUSES, limit=10**6)).items
    }
    revisions = {(str(e), m.rev) for e in ids if str(e) in current for m in view.history(e)}
    return current, revisions


def random_view(env: StoreEnv, rng: random.Random) -> ReadView:
    """Random visible types (none and all included), now and then a random attribute half."""
    classes = env.builder.concrete_classes
    size = rng.choice((0, len(classes))) if rng.random() < 0.1 else rng.randint(1, len(classes) - 1)
    types = frozenset(rng.sample(classes, size))
    if rng.random() < 0.6:
        return ReadView(types)
    mask: AttributeMask = tuple(
        (
            name,
            frozenset(a for a in env.builder.model.classes[name].attributes if rng.random() < 0.5),
        )
        for name in classes
    )
    return ReadView(types if rng.random() < 0.5 else None, mask)


def links(draft: TxDraft, rng: random.Random) -> None:
    for _ in range(rng.randint(1, 4)):
        active = draft.active_ids()
        if active:
            draft.link(rng.choice(active))
    if not draft.work:
        draft.create()


def snapshot_of(env: StoreEnv, rng: random.Random) -> Snapshot:
    """A snapshot of the head: of the env's store, or, for the file store, of a store opened
    anew (as the server does per request), with or without its index."""
    store = env.store
    if isinstance(store, FileStore) and rng.random() < 0.5:
        store = FileStore(store.layout.root, env.graph_id, use_index=rng.random() < 0.5)
    return store.snapshot()


@pytest.mark.parametrize("seed", range(CASES))
def test_a_consumer_of_the_feed_ends_with_the_visible_head(
    backend: str, ontology: str, tmp_path: Path, seed: int
) -> None:
    rng = random.Random(5000 + seed)
    env = StoreEnv(backend, ontology, tmp_path, seed=seed)
    env.random(rng.randint(4, 12))
    read_view = random_view(env, rng)
    writes = rng.randint(4, 14)
    seen: Pairs = []
    at = FeedPosition(0)
    for _ in range(10_000):
        raw = snapshot_of(env, rng)
        view = VisibleSnapshot(raw, read_view)
        page = change_page(
            view,
            raw,
            at,
            rng.choice(LIMITS),
            session=None,
            migration=lambda _: None,
            scan_cap=rng.choice(CAPS),
        )
        seen += received(page)
        assert page["head"] == raw.seq
        at = position(page["next"])
        if not page["more"]:  # caught up: the projection is the visible state of this head
            ids = sorted(
                {r.id for tx in env.store.changes_since(0, limit=10**6) for r in tx.revisions}
            )
            current, revisions = visible_state(view, ids)
            assert dict(seen) == current, f"seed {seed}"
            assert Counter(seen) == Counter(revisions), f"seed {seed}"
            if writes == 0:
                break
        if writes and rng.random() < 0.6:  # a write between two pages
            writes -= 1
            if rng.random() < 0.5:
                env.random(1)
            else:
                env.write(lambda d: links(d, rng))
    else:
        raise AssertionError("the feed never caught up")


# ---- through the service ---------------------------------------------------------------------


class Source:
    """A policy source with one fixed policy."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def hiding(svc: ServiceEnv, hidden: str) -> Source:
    """The builtin policy plus a deny rule that hides ``hidden`` from every reader."""
    deny: JsonValue = {
        "id": "hide_one",
        "effect": "deny",
        "action": ["read.get"],
        "resource": f"graph:{svc.entry.id}/type:{hidden}",
    }
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], deny], "obligations": []}  # type: ignore[misc]
    return Source(policy_set(document))


def open_view(svc: ServiceEnv, reader: ReadService) -> GraphRead:
    return reader.open(svc.actor, svc.catalog.resolve(svc.name), "read.history", REQUEST)


def follow(svc: ServiceEnv, reader: ReadService, limit: int, between: Callable[[], Any]) -> Pairs:
    """Follow the feed from ``tx:0`` through the service, calling ``between`` after each page
    until it returns ``None``; the pairs of every page."""
    seen: Pairs = []
    at: FeedPosition = FeedPosition(0)
    pending = True
    for _ in range(10_000):
        page = reader.changes(open_view(svc, reader), at, limit=limit)
        seen += received(page)
        at = position(page["next"])
        if pending:
            pending = between() is not None
        elif not page["more"]:
            return seen
    raise AssertionError("the feed never caught up")


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    return env


def test_the_service_feed_projects_the_visible_head_and_agrees_with_history(
    svc: ServiceEnv,
) -> None:
    counts = Counter(r.type for r in svc.all_revisions())
    hidden = str(counts.most_common(1)[0][0])
    shown = sorted({str(t) for t in counts} - {hidden})
    reader = svc.make_reader(policy=hiding(svc, hidden))
    rng = random.Random(7)
    writes = iter(range(6))

    def write() -> object:
        step = next(writes, None)
        if step is not None:
            svc.create(rng.choice([hidden, *shown]) if step % 2 else hidden)
        return step

    seen = follow(svc, reader, 3, write)
    view = open_view(svc, reader)
    ids = sorted({r.id for r in svc.all_revisions()})
    current, revisions = visible_state(view.snapshot, ids)
    assert dict(seen) == current and Counter(seen) == Counter(revisions)
    stored = svc.snapshot()
    assert all(stored.get(EntityId(UUID(e))).type != hidden for e in current)  # type: ignore[union-attr]
    # every entity item is the item ``history`` lists for that revision
    page: Any = reader.changes(view, FeedPosition(0), limit=500)
    while True:
        for entry in page["items"]:
            for item in entry["entities"]:
                listed: Any = reader.history(view, svc.actor, item["id"], limit=500, offset=0)[0]
                same = next(i for i in listed["items"] if i["rev"] == item["rev"])
                for key in ("tx_seq", "recorded_at", "command", "reason", "own_session"):
                    assert same[key] == entry[key], key
                assert same["record_status"] == item["record_status"]
                assert same["changed"] == item["changed"]
        if not page["more"]:
            break
        page = reader.changes(view, position(page["next"]), limit=500)
