"""A graph with stored files to move between the data root and the fake object store
(``graph-admin files move``, object storage O7): an admin context whose maintenance port talks
to the fake on its clock, file adds and writes through the services, and what the move tests
check after a move (manifest, audit stream, verification, readable files)."""

import dataclasses
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.audit.verify import verify_stream
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.services.files import FileAddRequest, FileService
from graph_cli.services.write import WriteConfig, WriteRequest, WriteService
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.settings import ObjectStoreSettings
from graph_cli_admin.services_admin import audit, files, files_move, graphs
from graph_cli_admin.services_admin.context import AdminContext
from support.admin_context import admin_context
from support.clock import FixedClock, StepClock
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.fixtures import ONTOLOGIES
from support.ids import SeededIdProvider
from support.services import app_actor

FIRST = b"%PDF-1.4\nthe first stored file\n"
SECOND = b"%PDF-1.4\nthe second stored file, a little longer\n"
LOOSE = b"bytes no tx names\n"
RACING = b"%PDF-1.4\na file added while the graph moves\n"
SEEDS = iter(range(100, 10_000))


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def context(
    root: Path,
    settings: ObjectStoreSettings | None,
    clock: FixedClock,
    *,
    faults: Any = None,
    seed: int = 21,
) -> AdminContext:
    """An admin context whose maintenance port talks to the fake on its clock; ``seed``
    keeps the event ids of several contexts on one root apart."""
    ctx = admin_context(root, faults)

    def client(values: ObjectStoreSettings) -> ObjectStoreClient:
        return ObjectStoreClient(
            values,
            clock=clock,
            timeouts=Timeouts(connect_s=2.0, read_s=2.0),
            retry=RetryPolicy(max_retries=0),
        )

    step = StepClock()
    return dataclasses.replace(
        ctx,
        catalog=FileCatalog(root, object_store=settings),
        maintenance=FileStoreMaintenance(
            root,
            syncer=ctx.maintenance.syncer,
            faults=faults,
            object_store=settings,
            client_factory=client,
        ),
        clock=step,
        ids=SeededIdProvider(step, seed),
    )


def create(ctx: AdminContext) -> GraphId:
    manifest = graphs.create(
        ctx,
        name="example_graph",
        source=ONTOLOGIES / "notes_and_sources" / "1.1.0",
        module=None,
        title=None,
        language=None,
    )
    return manifest.entry.id


def writer(
    root: Path, settings: ObjectStoreSettings | None, faults: Any = None
) -> tuple[FileCatalog, WriteService]:
    clock = StepClock(start="2030-01-01T00:00:00Z")
    catalog = FileCatalog(root, object_store=settings, faults=faults)
    return catalog, WriteService(
        catalog=catalog,
        audit=FileAuditLog(root),
        policy=BuiltinPolicySource(),
        clock=clock,
        ids=SeededIdProvider(clock, next(SEEDS)),
        env=EnvInfo("<data root>", "default"),
        config=WriteConfig(lock_timeout_s=5.0),
    )


def add_file(
    root: Path, settings: ObjectStoreSettings | None, path: Path, faults: Any = None
) -> str:
    """``graph file add`` through the services; the new entity's id."""
    catalog, service = writer(root, settings, faults)
    files_service = FileService(catalog=catalog, writer=service)
    result = files_service.add(
        FileAddRequest(
            actor=app_actor(),
            graph="example_graph",
            path=path,
            request=RequestMeta("file add", hash_value({"path": path.name})),
        )
    )
    assert result.entity["id"] is not None
    return str(result.entity["id"])


def apply(root: Path, settings: ObjectStoreSettings | None, *ops: dict[str, Any]) -> None:
    _, service = writer(root, settings)
    document = {"ops": list(ops)}
    request = WriteRequest(
        app_actor(), "example_graph", service.parse(document), False, RequestMeta.of("apply", {})
    )
    service.apply(request)


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def populated(tmp_path: Path, fake: FakeS3, clock: FixedClock) -> tuple[AdminContext, GraphId]:
    """A local graph with two files, each named by two revisions (a title change lists the
    blob again), and an orphan in ``blobs/``."""
    root = tmp_path / "root"
    settings = fake.settings()
    ctx = context(root, settings, clock)
    graph_id = create(ctx)
    first = add_file(root, settings, write(tmp_path, "first.pdf", FIRST))
    second = add_file(root, settings, write(tmp_path, "second.pdf", SECOND))
    apply(root, settings, {"op": "update", "id": first, "set": {"title": "First, renamed"}})
    apply(root, settings, {"op": "update", "id": second, "set": {"title": "Second, renamed"}})
    layout = GraphLayout(root / "graphs" / str(graph_id))
    orphan = layout.blob_path(digest(LOOSE))
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(LOOSE)
    return ctx, graph_id


def move(ctx: AdminContext, to: str, **kw: Any) -> files_move.MoveOutcome:
    options: dict[str, Any] = {
        "refs": ["example_graph"],
        "all_graphs": False,
        "dry_run": False,
        "step": None,
        "reason": "test",
        **kw,
    }
    return files_move.move(ctx, to=to, **options)


def moved(ctx: AdminContext, to: str, **kw: Any) -> dict[str, Any]:
    outcome = move(ctx, to, **kw)
    assert outcome.error is None, outcome.error
    (row,) = outcome.data["graphs"]  # type: ignore[union-attr]
    assert isinstance(row, dict)
    return row


def refused(call: Callable[[], files_move.MoveOutcome]) -> GraphCliError:
    outcome = call()
    assert outcome.error is not None, outcome.data
    return outcome.error


def manifest_of(ctx: AdminContext, graph_id: GraphId) -> Any:
    return read_manifest(GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id)))[0]


def stream_events(ctx: AdminContext, graph_id: GraphId) -> list[dict[str, Any]]:
    stream = graph_stream(str(graph_id))
    report = verify_stream(ctx.audit.read_lines(stream), stream=stream)
    assert report.findings == ()
    return [dict(event) for event in report.events]


def audit_codes(ctx: AdminContext) -> list[str]:
    report = audit.verify(ctx)
    report.raise_on_errors()
    return sorted(f.code for f in report.findings)


def verify_codes(ctx: AdminContext, *, rehash: bool = True) -> list[str]:
    data = files.verify(ctx, graph_ref="example_graph", rehash=rehash).data
    return sorted(str(f["code"]).rsplit(".", 1)[-1] for f in data["findings"])  # type: ignore[union-attr, index]


def readable(ctx: AdminContext, graph_id: GraphId) -> set[bytes]:
    catalog = ctx.catalog
    store, _ = catalog.store_for(graph_id)
    return {b"".join(store.read_blob(digest(d))) for d in (FIRST, SECOND)}


def object_keys(fake: FakeS3) -> list[str]:
    return [key for key in sorted(fake.keys()) if "/blobs/" in key]


def local_blobs(ctx: AdminContext, graph_id: GraphId) -> list[str]:
    base = GraphLayout(ctx.maintenance.data_root / "graphs" / str(graph_id)).blobs / "sha256"
    return sorted(p.name for p in base.rglob("*") if p.is_file())


def no_secret(*values: Any) -> None:
    text = json.dumps(values, default=repr)
    assert ACCESS_KEY_ID not in text and SECRET_ACCESS_KEY not in text
