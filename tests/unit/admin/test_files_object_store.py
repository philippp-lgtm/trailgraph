"""``files verify``, ``files gc``, ``audit verify`` and ``files store check`` for an installation
with an object store (object storage O5, O7, O8, step 6), as services with the fake store.

The graph is put into the store by ``graph create`` with ``[files] store = "s3"`` (the
audited switch); the maintenance port gets a client factory sharing the fake's clock, so a
test can move the store's time. Every message and detail is checked for the fake's secrets.
"""

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.admin_context import admin_context
from support.clock import FixedClock
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.fixtures import ONTOLOGIES
from support.s3_graphs import blob_key, create_marker, object_store_manifest
from support.stores import FaultSwitch

from graph_cli.audit.verify import verify_stream
from graph_cli.catalog.config import S3Table
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage import finding_codes
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.files.store_marker import MARKER_KEY, reset_identity_cache
from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.settings import ObjectStoreSettings
from graph_cli_admin.services_admin import audit, files, graphs, store_check
from graph_cli_admin.services_admin.context import AdminContext

CONTENT = b"%PDF-1.4\nbytes of a stored file\n"
LOOSE = b"bytes no tx names\n"
OTHER_ID = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime.now(UTC))


@pytest.fixture
def fake(clock: FixedClock) -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3(clock=clock) as store:
        yield store
    reset_identity_cache()


def context(
    root: Path,
    settings: ObjectStoreSettings | None,
    clock: FixedClock,
    *,
    files_store: str = "s3",
    problem: GraphCliError | None = None,
    faults: FaultSwitch | None = None,
    ctx_clock: FixedClock | None = None,
) -> AdminContext:
    ctx = admin_context(root, faults)

    def client(values: ObjectStoreSettings) -> ObjectStoreClient:
        return ObjectStoreClient(
            values,
            clock=clock,
            timeouts=Timeouts(connect_s=2.0, read_s=2.0),
            retry=RetryPolicy(max_retries=0),
        )

    maintenance = ctx.maintenance
    return dataclasses.replace(
        ctx,
        config=dataclasses.replace(ctx.config, files_store=files_store),  # type: ignore[arg-type]
        catalog=FileCatalog(root, object_store=settings, object_store_problem=problem),
        maintenance=FileStoreMaintenance(
            root,
            syncer=maintenance.syncer,
            faults=faults,
            object_store=settings,
            object_store_problem=problem,
            client_factory=client,
        ),
        clock=ctx_clock or ctx.clock,
    )


def create(ctx: AdminContext, name: str = "example_graph") -> GraphId:
    manifest = graphs.create(
        ctx,
        name=name,
        source=ONTOLOGIES / "notes_and_sources" / "1.1.0",
        module=None,
        title=None,
        language=None,
    )
    return manifest.entry.id


def store_of(root: Path, graph_id: GraphId, settings: ObjectStoreSettings | None) -> FileStore:
    return FileStoreMaintenance(root, object_store=settings).store(graph_id)


def local_file(root: Path, graph_id: GraphId, data: bytes) -> Path:
    """A file in the graph's ``blobs/`` (no tx names it)."""
    path = store_of(root, graph_id, None).layout.blob_path(digest(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def no_secret(*values: Any) -> None:
    text = json.dumps(values, default=repr)
    assert ACCESS_KEY_ID not in text
    assert SECRET_ACCESS_KEY not in text
    assert "AWS4-HMAC-SHA256" not in text


def refused(call: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        call()
    return caught.value


# ---- files verify, files gc, audit verify ---------------------------------------------


def test_a_graph_created_in_the_store_verifies_clean(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock)
    create(ctx)
    verified = files.verify(ctx, graph_ref="example_graph", rehash=True)
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert verified.data["stored"] == {"files": 0, "bytes": 0}
    report = audit.verify(ctx)
    assert report.data["counts"] == {"error": 0, "warning": 0, "info": 0}, report.findings


def test_an_unusable_store_fails_files_verify_with_a_finding(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock)
    graph_id = create(ctx)
    document = {"schema": "graph_cli.object_store/1", "store_id": OTHER_ID, "created_at": "x"}
    fake.put(MARKER_KEY, json.dumps(document).encode())
    reset_identity_cache()
    error = refused(lambda: files.verify(ctx, graph_ref="example_graph", rehash=False))
    assert error.code == "admin.integrity_errors"
    (finding,) = error.details["findings"]  # type: ignore[misc]
    assert finding["code"] == finding_codes.BLOB_STORE_UNAVAILABLE  # type: ignore[index]
    assert "not in that bucket" in finding["message"]  # type: ignore[index]
    report = audit.verify(ctx)  # no tx names a file: audit verify asks the store nothing
    assert report.findings == ()
    assert report.data["graphs"] == [{"id": str(graph_id), "clean": True, "seq": 2}]
    no_secret(error.details)


def test_gc_of_a_graph_in_the_store(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> None:
    """Young orphans stay, old ones go (store's clock); a local copy goes only when the
    store holds its hash and size, the others are kept and listed; audited with
    ``misplaced_removed``; ``audit verify`` stays clean."""
    root = tmp_path / "root"
    settings = fake.settings()
    ctx = context(root, settings, clock)
    graph_id = create(ctx)
    stored = digest(CONTENT)
    fake.put(blob_key(graph_id, stored), CONTENT)
    fake.put(blob_key(graph_id, digest(LOOSE)), LOOSE)
    copies = {
        digest(d): local_file(root, graph_id, d) for d in (CONTENT, b"a copy the store lacks\n")
    }
    young = files.gc(ctx, graph_ref="example_graph", reason="tidy", dry_run=True).data
    assert young["blobs_removed"] == []
    assert young["misplaced_removed"] == [{"hash": stored, "size": len(CONTENT)}]
    kept = [{"hash": digest(b"a copy the store lacks\n"), "size": 23}]
    assert young["misplaced_kept"] == kept
    assert copies[stored].exists()  # a dry run removes nothing
    clock.advance(timedelta(hours=25))
    done = files.gc(ctx, graph_ref="example_graph", reason="tidy", dry_run=False).data
    assert done["removed"] is True
    orphans = sorted(
        [{"hash": digest(LOOSE), "size": len(LOOSE)}, {"hash": stored, "size": len(CONTENT)}],
        key=lambda b: str(b["hash"]),
    )
    assert done["blobs_removed"] == orphans
    assert done["misplaced_removed"] == [{"hash": stored, "size": len(CONTENT)}]
    assert done["misplaced_kept"] == kept
    assert not copies[stored].exists() and copies[digest(b"a copy the store lacks\n")].exists()
    assert [k for k in fake.objects if "/blobs/" in k] == []
    stream = graph_stream(str(graph_id))
    events = verify_stream(ctx.audit.read_lines(stream), stream=stream)
    assert events.findings == ()
    change = events.events[-1]["change"]
    assert change == {
        "blobs_removed": orphans,
        "staging_removed": 0,
        "misplaced_removed": [{"hash": stored, "size": len(CONTENT)}],
    }
    report = audit.verify(ctx)
    assert report.data["counts"]["error"] == 0, report.findings  # type: ignore[index]


def test_gc_of_a_local_graph_without_a_store_records_what_it_did_before(
    tmp_path: Path, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, None, clock, files_store="local")
    graph_id = create(ctx)
    local_file(root, graph_id, LOOSE)
    done = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    assert (done["misplaced_removed"], done["misplaced_kept"]) == ([], [])
    stream = graph_stream(str(graph_id))
    change = verify_stream(ctx.audit.read_lines(stream), stream=stream).events[-1]["change"]
    assert change == {"blobs_removed": [{"hash": digest(LOOSE), "size": 18}], "staging_removed": 0}


def test_an_interrupted_switch_is_reported_by_audit_verify(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    faults = FaultSwitch()
    faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    ctx = context(root, fake.settings(), clock, faults=faults)
    error = refused(lambda: create(ctx))
    assert error.details["blob_location"] == "local"
    faults.armed.clear()
    report = audit.verify(ctx)
    (finding,) = report.findings
    assert finding.code == "integrity.audit.intent_without_effect"
    assert finding.severity.value == "info"
    assert "files move " in finding.message and "--to s3" in finding.message
    report.raise_on_errors()  # info: the graph is local and whole


def test_a_manifest_switched_behind_the_audits_back_is_an_error(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock, files_store="local")
    graph_id = create(ctx)
    object_store_manifest(
        store_of(root, graph_id, None).layout, graph_id, create_marker(fake.settings())
    )
    report = audit.verify(ctx)
    assert [f.code for f in report.findings] == ["integrity.audit.blob_store_mismatch"]
    assert refused(report.raise_on_errors).code == "admin.integrity_errors"


# ---- files store check ------------------------------------------------------------------


def statuses(data: dict[str, Any]) -> dict[str, str]:
    return {c["name"]: c["status"] for c in data["checks"]}


def failed(error: GraphCliError) -> dict[str, Any]:
    assert error.code == "admin.store_check_failed", error
    details = dict(error.details)
    no_secret(error.message, details)
    return details


def test_store_check_is_green_against_the_fake(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock, ctx_clock=clock)
    first = store_check.check(ctx)
    assert statuses(first)["marker"] == "warning"  # no marker before the first graph
    assert first["store_id"] is None
    graph_id = create(ctx)
    before = set(fake.keys())
    data = store_check.check(ctx)
    assert statuses(data) == {
        "settings": "ok",
        "credentials": "ok",
        "marker": "ok",
        "list": "ok",
        "write": "ok",
        "read": "ok",
        "delete": "ok",
        "missing_key": "ok",
        "clock": "ok",
        "addressing": "ok",
    }
    marker = json.loads(fake.get(MARKER_KEY) or b"{}")
    assert data["store_id"] == marker["store_id"]
    assert data["graphs"] == [{"id": str(graph_id), "name": "example_graph"}]
    assert data["store"] == {
        "bucket": fake.bucket,
        "prefix": "",
        "region": fake.region,
        "endpoint_host": f"127.0.0.1:{fake.port}",
        "addressing": "path",
    }
    assert data["skew_s"] == 0
    assert set(fake.keys()) == before  # the probe is gone
    assert [c.key for c in fake.calls("PUT")][-1].startswith("graph_cli_probe/")
    events = list(ctx.audit.read(graph_stream(str(graph_id))))
    assert [e["actions"] for e in events] == [["maint.graph_create"], ["maint.graph_configure"]]
    no_secret(first, data)


def test_store_check_without_a_store_says_so(tmp_path: Path, clock: FixedClock) -> None:
    problem = GraphCliError(
        "config.invalid", "files.s3.bucket is missing", details={"key": "files.s3.bucket"}
    )
    for given in (None, problem):
        ctx = context(tmp_path / "root", None, clock, files_store="local", problem=given)
        data = store_check.check(ctx)
        assert data["configured"] is False
        ((check,),) = [data["checks"]]
        assert check["status"] == "skipped"
        assert "no object store is configured" in check["message"]


def test_store_check_reports_invalid_settings_and_missing_credentials(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    problem = GraphCliError(
        "config.invalid",
        "config.toml: files.s3.region is missing; set it or AWS_REGION",
        details={"key": "files.s3.region"},
    )
    ctx = context(root, None, clock, files_store="local", problem=problem)
    ctx = dataclasses.replace(
        ctx, config=dataclasses.replace(ctx.config, files_s3=S3Table(bucket="example-bucket"))
    )
    details = failed(refused(lambda: store_check.check(ctx)))
    assert details["configured"] is True
    assert details["checks"] == [
        {"name": "settings", "status": "error", "message": problem.message}
    ]
    unset = dataclasses.replace(fake.settings(), credentials=None)
    details = failed(refused(lambda: store_check.check(context(root, unset, clock))))
    found = statuses(details)
    assert found["credentials"] == "error"
    assert {found[n] for n in ("marker", "list", "write", "read", "delete", "missing_key")} == {
        "skipped"
    }
    message = next(c["message"] for c in details["checks"] if c["name"] == "credentials")
    assert "AWS_ACCESS_KEY_ID" in message and "AWS_SECRET_ACCESS_KEY" in message


def test_store_check_names_a_missing_list_bucket_permission(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """A store denying HEAD of a missing key (the 403 instead of 404 of a key without
    ListBucket): only the missing-key check fails and says why."""
    ctx = context(tmp_path / "root", fake.settings(), clock, ctx_clock=clock)
    create(ctx)  # the marker exists: its GET is not the missing object
    fake.without_list_bucket = True
    details = failed(refused(lambda: store_check.check(ctx)))
    found = statuses(details)
    assert found["missing_key"] == "error"
    assert [n for n, s in found.items() if s == "error"] == ["missing_key"]
    message = next(c["message"] for c in details["checks"] if c["name"] == "missing_key")
    assert "ListBucket" in message and "403" in message


def test_store_check_reports_clock_skew(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> None:
    ours = FixedClock(clock.now() - timedelta(minutes=10))
    ctx = context(tmp_path / "root", fake.settings(), clock, ctx_clock=ours)
    data = store_check.check(ctx)
    assert data["skew_s"] == 600
    check = next(c for c in data["checks"] if c["name"] == "clock")
    assert check["status"] == "warning"
    assert "600 s ahead" in check["message"]
    far = FixedClock(clock.now() - timedelta(minutes=20))
    details = failed(
        refused(
            lambda: store_check.check(
                context(tmp_path / "root", fake.settings(), clock, ctx_clock=far)
            )
        )
    )
    assert statuses(details)["clock"] == "error"


def test_store_check_reports_a_foreign_marker_and_a_refused_write(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock, ctx_clock=clock)
    create(ctx)
    document = {"schema": "graph_cli.object_store/1", "store_id": OTHER_ID, "created_at": "x"}
    fake.put(MARKER_KEY, json.dumps(document).encode())
    details = failed(refused(lambda: store_check.check(ctx)))
    marker = next(c for c in details["checks"] if c["name"] == "marker")
    assert marker["status"] == "error" and "example_graph" in marker["message"]
    del fake.objects[MARKER_KEY]
    fake.fail(403, "AccessDenied", method="PUT")
    details = failed(refused(lambda: store_check.check(ctx)))
    found = statuses(details)
    assert (found["marker"], found["write"], found["read"], found["delete"]) == (
        "error",
        "error",
        "skipped",
        "skipped",
    )
    assert "not their bucket" in next(
        c["message"] for c in details["checks"] if c["name"] == "marker"
    )


def test_store_check_skips_what_an_unreachable_store_cannot_answer(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    settings = dataclasses.replace(fake.settings(), endpoint_url="http://127.0.0.1:9")
    details = failed(
        refused(lambda: store_check.check(context(tmp_path / "root", settings, clock)))
    )
    found = statuses(details)
    assert found["marker"] == "error"
    assert {found[n] for n in ("list", "write", "read", "delete", "missing_key")} == {"skipped"}
    assert (found["clock"], found["addressing"]) == ("skipped", "skipped")


def test_gc_keeps_a_copy_whose_counterpart_is_damaged(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock)
    graph_id = create(ctx)
    fake.put(blob_key(graph_id, digest(CONTENT)), b"X" + CONTENT[1:])  # same size, rotten
    copy = local_file(root, graph_id, CONTENT)
    done = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    assert done["misplaced_removed"] == []
    assert done["misplaced_kept"] == [{"hash": digest(CONTENT), "size": len(CONTENT)}]
    assert done["removed"] is False and copy.read_bytes() == CONTENT


def test_objects_of_a_local_graph_never_in_the_store_are_report_only(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """A cloned or restored installation shares the bucket (another marker) and the graph
    id: files verify reports the objects, files gc keeps them, whatever they hold."""
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock, files_store="local")
    graph_id = create(ctx)
    document = {"schema": "graph_cli.object_store/1", "store_id": OTHER_ID, "created_at": "x"}
    fake.put(MARKER_KEY, json.dumps(document).encode())
    key = blob_key(graph_id, digest(CONTENT))
    fake.put(key, CONTENT)
    local_file(root, graph_id, CONTENT)  # the data root holds the same bytes
    verified = files.verify(ctx, graph_ref="example_graph", rehash=True).data
    (finding,) = [
        f
        for f in verified["findings"]  # type: ignore[union-attr]
        if f["code"] == finding_codes.BLOB_MISPLACED  # type: ignore[index]
    ]
    assert "not this graph's recorded store" in finding["message"]  # type: ignore[index]
    done = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    assert done["misplaced_removed"] == []
    assert done["misplaced_kept"] == [{"hash": digest(CONTENT), "size": len(CONTENT)}]
    assert fake.get(key) == CONTENT


def switch_to_local(ctx: AdminContext, graph_id: GraphId, store_id: str) -> None:
    """A recorded move from the object store back to the data root, as step 7's switch
    will write it (intent with change.blob_store, manifest by one rename, anchor)."""
    from graph_cli.audit import events as builders
    from graph_cli.kernel.records import RequestMeta

    meta = RequestMeta.of("files move", {"graph": str(graph_id)})
    decisions = ctx.authorize(
        [("maint.graph_configure", f"graph:{graph_id}")], request=meta, graph=graph_id
    )
    with ctx.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        current = session.manifest()
        change = builders.blob_store_change("s3", "local", store_id, blobs=1, size=len(CONTENT))
        receipt = ctx.intent(session, current.entry, decisions, meta, change)
        local = dataclasses.replace(current.store, blob_location="local", store_id=None)
        session.replace_manifest(dataclasses.replace(current, store=local), receipt)
        ctx.anchor(current.entry, decisions, meta, receipt)


@pytest.mark.parametrize("intact", [True, False])
def test_gc_removes_the_objects_after_a_recorded_move_back_with_the_hash_check(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, intact: bool
) -> None:
    root = tmp_path / "root"
    ctx = context(root, fake.settings(), clock)
    graph_id = create(ctx)
    store_id = json.loads(fake.get(MARKER_KEY) or b"{}")["store_id"]
    key = blob_key(graph_id, digest(CONTENT))
    fake.put(key, CONTENT)
    switch_to_local(ctx, graph_id, store_id)
    path = local_file(root, graph_id, CONTENT)
    if not intact:  # same size, other bytes under the hash's name: a damaged named copy
        path.write_bytes(b"X" + CONTENT[1:])
    verified = files.verify(ctx, graph_ref="example_graph", rehash=False).data
    messages = [f["message"] for f in verified["findings"]]  # type: ignore[index, union-attr]
    assert not any("not this graph's recorded store" in m for m in messages)
    done = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    item = [{"hash": digest(CONTENT), "size": len(CONTENT)}]
    if intact:
        assert (done["misplaced_removed"], done["misplaced_kept"]) == (item, [])
        assert key not in fake.objects
    else:
        assert (done["misplaced_removed"], done["misplaced_kept"]) == ([], item)
        assert fake.get(key) == CONTENT
    report = audit.verify(ctx)
    assert report.data["counts"]["error"] == 0, report.findings  # type: ignore[index]
