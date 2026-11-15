"""An interrupted ``files move`` (object storage O7; contracts W.e): faults between copies and
around the manifest rename, in both directions, against the fake object store.

- Between copies (``blob.after_copy``), in a pass or in the switch's copies under the lock:
  the graph stays where it was with every file, no intent was written (``audit verify``
  clean), only extra copies exist; running the move again finishes it.
- Before the manifest rename (``manifest.before_rename``): the intent without its effect is
  ``intent_without_effect`` (info) and the graph is whole in the old location; running the
  move again finishes it.
- After the rename, before the installation anchor: the graph is switched and ``audit
  verify`` is clean (the effect proves the intent).
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.clock import FixedClock
from support.fake_s3 import FakeS3
from support.move_scene import (
    audit_codes,
    context,
    manifest_of,
    move,
    moved,
    object_keys,
    populated,
    readable,
    refused,
    stream_events,
    verify_codes,
)

from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.store_marker import reset_identity_cache
from graph_cli_admin.services_admin import files, files_move
from graph_cli_admin.services_admin.context import AdminContext

IWE = "integrity.audit.intent_without_effect"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime.now(UTC))


@pytest.fixture
def fake(clock: FixedClock) -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3(clock=clock) as store:
        yield store
    reset_identity_cache()


class Faults:
    """Raises ``OSError`` at the armed points; ``after`` arms ``then`` once it was seen."""

    def __init__(self) -> None:
        self.armed: set[CrashPoint] = set()
        self.after: tuple[CrashPoint, CrashPoint] | None = None

    def __call__(self, point: CrashPoint) -> None:
        if self.after is not None and point is self.after[0]:
            self.armed.add(self.after[1])
            self.after = None
            return
        if point in self.armed:
            self.armed.discard(point)
            raise OSError(f"injected fault at {point}")


def faulty(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, start: str
) -> tuple[AdminContext, Faults]:
    """A populated graph in ``start``, old copies collected, and a context with faults."""
    ctx, _ = populated(tmp_path, fake, clock)
    if start == "s3":
        moved(ctx, "s3")
        files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False)
    faults = Faults()
    root = ctx.maintenance.data_root
    return context(root, fake.settings(), clock, faults=faults, seed=51), faults


DIRECTIONS = [("local", "s3"), ("s3", "local")]


@pytest.mark.parametrize(("start", "to"), DIRECTIONS)
def test_a_fault_between_copies_of_a_pass_leaves_the_graph_where_it_was(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, start: str, to: str
) -> None:
    ctx, faults = faulty(tmp_path, fake, clock, start)
    graph_id = ctx.catalog.resolve("example_graph").id
    events = len(stream_events(ctx, graph_id))
    faults.armed.add(CrashPoint.BLOB_AFTER_COPY)
    error = refused(lambda: move(ctx, to))
    assert error.code == "backend.unavailable"
    assert manifest_of(ctx, graph_id).store.blob_location == start
    assert len(stream_events(ctx, graph_id)) == events
    assert audit_codes(ctx) == []
    assert readable(ctx, graph_id)
    assert "blob_missing" not in verify_codes(ctx)
    row = moved(ctx, to)
    assert row["status"] == "moved" and row["copied"]["blobs"] == 2  # one was copied before
    assert audit_codes(ctx) == []


@pytest.mark.parametrize(("start", "to"), DIRECTIONS)
def test_a_fault_between_copies_under_the_lock_switches_nothing(
    tmp_path: Path,
    fake: FakeS3,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    to: str,
) -> None:
    ctx, faults = faulty(tmp_path, fake, clock, start)
    graph_id = ctx.catalog.resolve("example_graph").id
    monkeypatch.setattr(files_move, "PASS_BUDGET_S", 0.0)
    assert moved(ctx, to, step="pass")["remaining"]["blobs"] == 2
    events = len(stream_events(ctx, graph_id))
    faults.armed.add(CrashPoint.BLOB_AFTER_COPY)
    assert refused(lambda: move(ctx, to, step="switch")).code == "backend.unavailable"
    assert manifest_of(ctx, graph_id).store.blob_location == start
    assert len(stream_events(ctx, graph_id)) == events
    assert audit_codes(ctx) == []
    monkeypatch.setattr(files_move, "PASS_BUDGET_S", 30.0)
    assert moved(ctx, to, step="switch")["status"] == "moved"
    assert audit_codes(ctx) == []


@pytest.mark.parametrize(("start", "to"), DIRECTIONS)
def test_a_fault_before_the_manifest_rename_is_an_interrupted_switch(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, start: str, to: str
) -> None:
    ctx, faults = faulty(tmp_path, fake, clock, start)
    graph_id = ctx.catalog.resolve("example_graph").id
    faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    assert refused(lambda: move(ctx, to)).code == "backend.unavailable"
    assert manifest_of(ctx, graph_id).store.blob_location == start
    intent = stream_events(ctx, graph_id)[-1]
    assert intent["change"]["blob_store"]["to"] == to
    assert audit_codes(ctx) == [IWE]  # info: the graph is whole where it was
    assert readable(ctx, graph_id)
    codes = verify_codes(ctx)
    assert "blob_missing" not in codes and "blob_store_unavailable" not in codes
    assert moved(ctx, to)["status"] == "moved"
    assert manifest_of(ctx, graph_id).store.blob_location == to
    assert audit_codes(ctx) == [IWE]  # the first intent stays an orphan
    assert readable(ctx, graph_id)


@pytest.mark.parametrize(("start", "to"), DIRECTIONS)
def test_a_fault_after_the_manifest_rename_leaves_the_graph_switched(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, start: str, to: str
) -> None:
    """The anchor append fails after the rename: the graph is at the target, its intent
    has its effect, and ``audit verify`` is clean."""
    ctx, faults = faulty(tmp_path, fake, clock, start)
    graph_id = ctx.catalog.resolve("example_graph").id
    faults.after = (CrashPoint.MANIFEST_BEFORE_RENAME, CrashPoint.AUDIT_BEFORE_WRITE)
    error = refused(lambda: move(ctx, to))
    assert error.code in ("audit.unavailable", "backend.unavailable")
    assert manifest_of(ctx, graph_id).store.blob_location == to
    assert audit_codes(ctx) == []
    assert readable(ctx, graph_id)
    assert "blob_missing" not in verify_codes(ctx)
    assert moved(ctx, to)["status"] == "at_target"
    if to == "local":
        assert len(object_keys(fake)) == 3  # the old copies stay until gc
