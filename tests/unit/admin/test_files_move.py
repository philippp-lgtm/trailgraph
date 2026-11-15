"""``graph-admin files move`` as a service, against the fake object store (object storage O7).

Graphs with files (referenced, an orphan, each hash named by two revisions; the model keeps
``content`` unique across entities) move from the data root to the object store and back:
copy passes, then the switch under the lock with its ``change.blob_store`` intent (real
counts), manifest format 1 <-> 2, ``audit verify`` and ``files verify --rehash`` clean (the
old copies are ``blob_misplaced`` info), every file readable, and ``files gc`` removes the
old copies after the hash check. Refusals: an unusable target, a graph mid-migration, a
quota 403; a graph at the target and a dry run write nothing. A file add racing a move
commits where the manifest names, in both directions (fault-hook driven: the move runs
inside the add's window).
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.clock import FixedClock
from support.fake_s3 import FakeS3
from support.fixtures import ONTOLOGIES
from support.move_scene import (
    FIRST,
    LOOSE,
    RACING,
    SECOND,
    add_file,
    audit_codes,
    context,
    digest,
    local_blobs,
    manifest_of,
    move,
    moved,
    no_secret,
    object_keys,
    populated,
    readable,
    refused,
    stream_events,
    verify_codes,
    write,
)
from support.s3_graphs import blob_key

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TxId, parse_id
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.blob_backend import LocalBlobs
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.store_marker import MARKER_KEY, reset_identity_cache
from graph_cli_admin.services_admin import files, files_move, graphs


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime.now(UTC))


@pytest.fixture
def fake(clock: FixedClock) -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3(clock=clock) as store:
        yield store
    reset_identity_cache()


# ---- the round trip ---------------------------------------------------------------------


def test_a_graph_moves_to_the_object_store_and_back(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    every = sorted([FIRST, SECOND, LOOSE], key=digest)
    total = {"blobs": 3, "bytes": sum(len(d) for d in every)}
    events_before = len(stream_events(ctx, graph_id))

    planned = moved(ctx, "s3", dry_run=True)
    assert planned["status"] == "planned" and planned["store_id"] is None  # no marker yet
    assert planned["stored"] == planned["remaining"] == total
    assert planned["copied"] == {"blobs": 0, "bytes": 0}
    assert fake.keys() == [] and len(stream_events(ctx, graph_id)) == events_before

    row = moved(ctx, "s3")
    marker = json.loads(fake.get(MARKER_KEY) or b"{}")
    store_id = marker["store_id"]
    assert row["status"] == "moved" and row["from"] == "local"
    assert row["store_id"] == store_id
    assert row["stored"] == row["copied"] == total and row["remaining"]["blobs"] == 0
    assert row["passes"] == 1
    assert object_keys(fake) == sorted(blob_key(graph_id, digest(d)) for d in every)
    manifest = manifest_of(ctx, graph_id)
    assert (manifest.store.blob_location, manifest.store.store_id) == ("s3", store_id)
    switch = stream_events(ctx, graph_id)[-1]
    assert switch["actions"] == ["maint.graph_configure"]
    assert switch["request"]["command"] == "files move"
    assert switch["reason"] == "test"
    assert switch["change"] == {
        "blob_store": {"from": "local", "to": "s3", "store_id": store_id, **total}
    }
    assert audit_codes(ctx) == []
    assert verify_codes(ctx) == ["blob_misplaced"] * 3 + ["blob_orphan"]
    assert readable(ctx, graph_id) == {FIRST, SECOND}
    assert moved(ctx, "s3")["status"] == "at_target"  # no event, nothing copied
    assert len(stream_events(ctx, graph_id)) == events_before + 1

    gc = files.gc(ctx, graph_ref="example_graph", reason="tidy", dry_run=False).data
    assert gc["misplaced_removed"] == [{"hash": digest(d), "size": len(d)} for d in every]
    assert gc["blobs_removed"] == []  # the orphan object is younger than the grace period
    assert local_blobs(ctx, graph_id) == []
    assert verify_codes(ctx) == ["blob_orphan"]

    back = moved(ctx, "local")
    assert back["status"] == "moved" and back["from"] == "s3"
    assert back["store_id"] == store_id  # the store the graph leaves: gc may clean it
    assert back["copied"] == total
    manifest = manifest_of(ctx, graph_id)
    assert (manifest.store.blob_location, manifest.store.store_id) == ("local", None)
    layout = GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id))
    assert layout.manifest.read_text().startswith("format_version = 1\n")
    for data in every:
        path = layout.blob_path(digest(data))
        assert path.read_bytes() == data and path.stat().st_mode & 0o777 == 0o444
    assert stream_events(ctx, graph_id)[-1]["change"] == {
        "blob_store": {"from": "s3", "to": "local", "store_id": store_id, **total}
    }
    assert audit_codes(ctx) == []
    assert verify_codes(ctx) == ["blob_misplaced"] * 3 + ["blob_orphan"]
    assert readable(ctx, graph_id) == {FIRST, SECOND}
    assert fake.get(MARKER_KEY) is not None  # the bucket keeps its marker

    gc = files.gc(ctx, graph_ref="example_graph", reason="tidy", dry_run=False).data
    assert len(gc["misplaced_removed"]) == 3  # type: ignore[arg-type]
    assert gc["blobs_removed"] == [{"hash": digest(LOOSE), "size": len(LOOSE)}]  # local: no grace
    assert object_keys(fake) == []
    assert verify_codes(ctx) == []
    assert audit_codes(ctx) == []
    no_secret(planned, row, back, gc)


def test_a_dry_run_writes_nothing(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    moved(ctx, "s3")
    files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False)
    before = (fake.keys(), local_blobs(ctx, graph_id), len(stream_events(ctx, graph_id)))
    row = moved(ctx, "local", dry_run=True)
    assert row["status"] == "planned" and row["remaining"]["blobs"] == 3
    assert (fake.keys(), local_blobs(ctx, graph_id), len(stream_events(ctx, graph_id))) == before
    assert manifest_of(ctx, graph_id).store.blob_location == "s3"


def test_all_moves_every_graph_not_at_the_target(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    other = graphs.create(
        ctx,
        name="other_graph",
        source=ONTOLOGIES / "notes_and_sources" / "1.1.0",
        module=None,
        title=None,
        language=None,
    ).entry.id
    moved(ctx, "s3", refs=["other_graph"])
    outcome = move(ctx, "s3", refs=[], all_graphs=True)
    assert outcome.error is None
    rows = {r["name"]: r["status"] for r in outcome.data["graphs"]}  # type: ignore[union-attr, index]
    assert rows == {"example_graph": "moved", "other_graph": "at_target"}
    assert manifest_of(ctx, other).store.blob_location == "s3"
    assert manifest_of(ctx, graph_id).store.blob_location == "s3"


def test_usage(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> None:
    ctx, _ = populated(tmp_path, fake, clock)
    for kw in ({"refs": []}, {"all_graphs": True}, {"dry_run": True, "step": "pass"}):
        with pytest.raises(GraphCliError) as caught:
            move(ctx, "s3", **kw)
        assert caught.value.code == "usage.invalid_argument"


# ---- refusals ---------------------------------------------------------------------------


def test_an_unusable_target_is_refused_before_any_copy(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    root = ctx.maintenance.data_root
    unconfigured = context(root, None, clock, seed=31)
    assert refused(lambda: move(unconfigured, "s3")).code == "config.invalid"
    keyless = context(root, fake.settings(credentials=None), clock, seed=32)
    error = refused(lambda: move(keyless, "s3"))
    assert error.code == "backend.unavailable"
    assert error.details["missing"] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    assert fake.keys() == [] and manifest_of(ctx, graph_id).store.blob_location == "local"
    moved(ctx, "s3")
    fake.put(
        MARKER_KEY,
        b'{"created_at":"2030-01-01T00:00:00Z","schema":"graph_cli.object_store/1","store_id":"0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"}\n',
    )
    reset_identity_cache()
    foreign = refused(lambda: move(context(root, fake.settings(), clock, seed=33), "local"))
    assert foreign.code == "backend.unavailable"
    assert manifest_of(ctx, graph_id).store.blob_location == "s3"
    assert local_blobs(ctx, graph_id) == sorted(
        digest(d).removeprefix("sha256:") for d in (FIRST, SECOND, LOOSE)
    )  # the old copies; nothing new


def test_a_graph_mid_migration_is_refused(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    head = ctx.maintenance.store(graph_id).changes_since(0, limit=1000)[-1]
    marker = MigrationMarker(
        head.seq, TxId(head.tx_id), "1.1.0", "2.0.0", "sha256:" + "1" * 64, "sha256:" + "2" * 64
    )
    with ctx.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        session.write_migration_marker(marker)
    events = len(stream_events(ctx, graph_id))
    for kw in ({}, {"dry_run": True}, {"step": "pass"}):
        error = refused(lambda kw=kw: move(ctx, "s3", **kw))  # type: ignore[misc]
        assert error.code == "admin.migration_incomplete"
        assert "2.0.0" in error.message
    assert object_keys(fake) == [] and len(stream_events(ctx, graph_id)) == events
    with ctx.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        session.remove_migration_marker()
    assert moved(ctx, "s3")["status"] == "moved"


def test_a_refused_upload_stops_the_pass_and_switches_nothing(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    fake.fail(403, "QuotaExceeded", method="PUT", key=blob_key(graph_id, digest(SECOND)))
    events = len(stream_events(ctx, graph_id))
    error = refused(lambda: move(ctx, "s3"))
    assert error.code == "files.store_refused"
    assert error.details["s3_code"] == "QuotaExceeded"
    no_secret(error.message, error.details)
    assert manifest_of(ctx, graph_id).store.blob_location == "local"
    assert len(stream_events(ctx, graph_id)) == events
    assert blob_key(graph_id, digest(SECOND)) not in sorted(fake.keys())
    assert audit_codes(ctx) == []
    assert moved(ctx, "s3")["status"] == "moved"  # once the store takes it


# ---- passes and steps -------------------------------------------------------------------


def test_steps_copy_within_the_budget_and_switch_last(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no time left after the first copy, each pass copies one file and says what
    remains; a switch with copies left under the lock switches nothing; then it does."""
    ctx, graph_id = populated(tmp_path, fake, clock)
    monkeypatch.setattr(files_move, "PASS_BUDGET_S", 0.0)
    first = moved(ctx, "s3", step="pass")
    assert first["status"] == "copying" and first["copied"]["blobs"] == 1
    assert first["remaining"]["blobs"] == 2 and first["passes"] == 1
    early = moved(ctx, "s3", step="switch")
    assert early["status"] == "copying" and early["remaining"]["blobs"] == 1
    assert manifest_of(ctx, graph_id).store.blob_location == "local"
    assert moved(ctx, "s3", step="pass")["remaining"]["blobs"] == 0
    last = moved(ctx, "s3", step="switch")
    assert last["status"] == "moved" and last["copied"]["blobs"] == 0
    assert last["stored"]["blobs"] == 3
    assert stream_events(ctx, graph_id)[-1]["change"]["blob_store"]["blobs"] == 3
    assert audit_codes(ctx) == []
    assert moved(ctx, "s3", step="switch")["status"] == "at_target"


def test_a_copy_removed_by_gc_before_the_switch_is_copied_again(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """A pass copies; a gc meanwhile removes the copy as misplaced (its counterpart is in
    the data root); the switch lists the target under the lock and copies it again."""
    ctx, graph_id = populated(tmp_path, fake, clock)
    moved(ctx, "s3")
    files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False)
    assert moved(ctx, "local", step="pass")["remaining"]["blobs"] == 0
    layout = GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id))
    copy = layout.blob_path(digest(FIRST))
    copy.unlink()  # what a gc removing the misplaced copy leaves
    row = moved(ctx, "local", step="switch")
    assert row["status"] == "moved" and row["copied"]["blobs"] == 1
    assert copy.read_bytes() == FIRST
    assert verify_codes(ctx) == ["blob_misplaced"] * 3 + ["blob_orphan"]


def test_a_damaged_local_copy_of_another_size_is_replaced(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    moved(ctx, "s3")
    layout = GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id))
    damaged = layout.blob_path(digest(SECOND))
    damaged.chmod(0o644)
    damaged.write_bytes(b"short")
    moved(ctx, "local")
    assert damaged.read_bytes() == SECOND
    assert verify_codes(ctx) == ["blob_misplaced"] * 3 + ["blob_orphan"]


def test_a_referenced_file_in_neither_location_stops_the_move(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id)).blob_path(
        digest(FIRST)
    ).unlink()
    error = refused(lambda: move(ctx, "s3"))
    assert error.code == "backend.corrupt" and error.details["blobs"] == [digest(FIRST)]
    assert manifest_of(ctx, graph_id).store.blob_location == "local"


# ---- a file add racing the move ----------------------------------------------------------


class MoveInside:
    """A fault hook of the add's store that moves the graph at ``point``: once the add has
    staged (and, for an object store, promoted) its blob, before it takes the lock."""

    def __init__(self, point: CrashPoint, action: Callable[[], None]) -> None:
        self.point = point
        self.action = action
        self.ran = False

    def __call__(self, point: CrashPoint) -> None:
        if point is self.point and not self.ran:
            self.ran = True
            self.action()


@pytest.mark.parametrize(
    ("start", "to", "point"),
    [
        ("local", "s3", "promote"),
        ("local", "s3", CrashPoint.BLOB_AFTER_STAGE),
        ("s3", "local", CrashPoint.BLOB_AFTER_UPLOAD),
        ("s3", "local", CrashPoint.BLOB_AFTER_STAGE),
    ],
    ids=[
        "to-s3-after-promote",
        "to-s3-before-promote",
        "to-local-after-promote",
        "to-local-before-promote",
    ],
)
def test_an_add_racing_the_move_commits_where_the_manifest_names(
    tmp_path: Path,
    fake: FakeS3,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    to: str,
    point: CrashPoint | str,
) -> None:
    """After the promote, the graph has moved before the add takes the lock: the publish
    under the lock uploads from (or renames) the staging file into the bound location.
    Before the promote, the promote already goes to the new location."""
    ctx, graph_id = populated(tmp_path, fake, clock)
    root = ctx.maintenance.data_root
    if start == "s3":
        moved(ctx, "s3")
    other = context(root, fake.settings(), clock, seed=41)
    hook = MoveInside(
        point if isinstance(point, CrashPoint) else CrashPoint.BLOB_AFTER_COPY,
        lambda: moved(other, to),
    )
    if point == "promote":  # a local promote has no crash point: the move runs right after it
        promote = LocalBlobs.promote

        def promote_then_move(self: LocalBlobs, staged: Any) -> None:
            promote(self, staged)
            hook(CrashPoint.BLOB_AFTER_COPY)

        monkeypatch.setattr(LocalBlobs, "promote", promote_then_move)
    added = add_file(root, fake.settings(), write(tmp_path, "racing.pdf", RACING), faults=hook)
    assert hook.ran
    puts = [c for c in fake.calls("PUT") if c.key == blob_key(graph_id, digest(RACING))]
    if point == "promote":
        assert len(puts) == 1  # the publish fallback under the lock
    assert manifest_of(ctx, graph_id).store.blob_location == to
    racing = digest(RACING)
    layout = GraphLayout(root / "graphs" / str(graph_id))
    if to == "s3":
        assert fake.get(blob_key(graph_id, racing)) == RACING
    else:
        assert layout.blob_path(racing).read_bytes() == RACING
    store, _ = ctx.catalog.store_for(graph_id)
    assert b"".join(store.read_blob(racing)) == RACING
    revision = store.snapshot().get(EntityId(parse_id(added)))
    assert revision is not None and revision.attrs["content"] == racing
    assert not [p for p in layout.blobs.iterdir() if p.name.startswith(".tmp-")]
    assert audit_codes(ctx) == []
    codes = verify_codes(ctx)
    assert "blob_missing" not in codes and "blob_hash_mismatch" not in codes
    files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False)
    assert "blob_misplaced" not in verify_codes(ctx)


def test_an_add_after_the_switch_uses_the_new_location(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    ctx, graph_id = populated(tmp_path, fake, clock)
    moved(ctx, "s3")
    add_file(ctx.maintenance.data_root, fake.settings(), write(tmp_path, "late.pdf", RACING))
    assert fake.get(blob_key(graph_id, digest(RACING))) == RACING
    assert digest(RACING).removeprefix("sha256:") not in local_blobs(ctx, graph_id)


def test_an_object_whose_bytes_differ_from_its_hash_is_not_copied(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """The streamed read of the source is verified: a damaged object stops the move to the
    data root with ``backend.corrupt``, nothing is placed under its hash, nothing switched."""
    ctx, graph_id = populated(tmp_path, fake, clock)
    moved(ctx, "s3")
    files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False)
    fake.corrupt_get(blob_key(graph_id, digest(SECOND)), b"%PDF-1.4\nnot the bytes of it\n")
    error = refused(lambda: move(ctx, "local"))
    assert error.code == "backend.corrupt"
    assert digest(SECOND).removeprefix("sha256:") not in local_blobs(ctx, graph_id)
    layout = GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id))
    assert not [p for p in layout.blobs.iterdir() if p.name.startswith(".tmp-")]
    assert manifest_of(ctx, graph_id).store.blob_location == "s3"
