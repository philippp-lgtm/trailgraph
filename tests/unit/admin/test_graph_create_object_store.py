"""``graph create`` with ``[files] store = "s3"`` (object storage O7, "genesis means local")
when the switch after the creation fails: the graph stays local and the error names it."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fake_s3 import FakeS3
from support.fixtures import ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.audit.verify import verify_stream
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.files.store_marker import read_store_marker
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli_admin.services_admin import graphs
from graph_cli_admin.services_admin.context import AdminContext


@pytest.fixture
def fake() -> Iterator[FakeS3]:
    with FakeS3() as store:
        yield store


def s3_context(root: Path, fake: FakeS3, faults: FaultSwitch | None = None) -> AdminContext:
    ctx = admin_context(root, faults)
    return dataclasses.replace(
        ctx,
        config=dataclasses.replace(ctx.config, files_store="s3"),
        catalog=FileCatalog(root, object_store=fake.settings()),
    )


def create(ctx: AdminContext) -> None:
    graphs.create(
        ctx,
        name="example_graph",
        source=ONTOLOGIES / "inventory" / "1.0.0",
        module=None,
        title=None,
        language=None,
    )


def test_a_failed_switch_leaves_the_graph_local_and_says_so(tmp_path: Path, fake: FakeS3) -> None:
    """The intent is written, the manifest rename fails: the graph keeps format 1 and its
    files in the data root, holds no file, and the error names it; the intent without its
    effect is what ``audit verify`` will see as an interrupted switch (step 6)."""
    root = tmp_path / "root"
    faults = FaultSwitch()
    faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    with pytest.raises(GraphCliError) as caught:
        create(s3_context(root, fake, faults))
    error = caught.value
    entry = FileCatalog(root).resolve("example_graph")
    assert error.code == "backend.unavailable"
    assert "example_graph was created" in error.message
    assert "keeps its stored files in the data root" in error.message
    assert error.details["graph_id"] == str(entry.id)
    assert error.details["blob_location"] == "local"
    manifest = read_manifest(GraphLayout(root / "graphs" / str(entry.id)))[0]
    assert manifest.store.blob_location == "local" and manifest.store.store_id is None
    marker = read_store_marker(ObjectStoreClient(fake.settings()))
    assert marker is not None
    stream = graph_stream(str(entry.id))
    report = verify_stream(FileAuditLog(root).read_lines(stream), stream=stream)
    assert report.findings == ()
    intent = dict(report.events[-1])
    assert intent["actions"] == ["maint.graph_configure"]
    assert intent["change"] == {
        "blob_store": {
            "from": "local",
            "to": "s3",
            "store_id": marker.store_id,
            "blobs": 0,
            "bytes": 0,
        }
    }


def test_a_lock_that_cannot_be_taken_leaves_the_graph_local(tmp_path: Path, fake: FakeS3) -> None:
    root = tmp_path / "root"
    ctx = s3_context(root, fake)

    class Busy:
        """The maintenance port, whose graph lock is always taken."""

        def __getattr__(self, name: str) -> object:
            return getattr(ctx.maintenance, name)

        def maintenance_session(self, *args: object, **kwargs: object) -> object:
            raise GraphCliError("backend.lock_timeout", "the graph is locked")

    with pytest.raises(GraphCliError) as caught:
        create(dataclasses.replace(ctx, maintenance=Busy()))  # type: ignore[arg-type]
    assert caught.value.code == "backend.lock_timeout"
    entry = FileCatalog(root).resolve("example_graph")
    assert caught.value.details["graph_id"] == str(entry.id)
    stream = graph_stream(str(entry.id))
    report = verify_stream(FileAuditLog(root).read_lines(stream), stream=stream)
    assert [e["actions"] for e in report.events] == [["maint.graph_create"]]
