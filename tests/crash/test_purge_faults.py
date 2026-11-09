"""Interrupted purges (plan T16, step 6): a fault at each point, then the same purge again.

A fault leaves what a kill -9 at that point would leave (the key store's own
SIGKILL table is in ``test_keystore_crash``). Running the purge again
completes it: the entity reads shredded, its file is gone, and ``audit
verify`` reports at most the orphan intent of the interrupted run (info).
"""

from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.services import ServiceEnv
from support.stores import FaultSwitch

from graph_cli.audit.finding_codes import INTENT_WITHOUT_EFFECT
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
from graph_cli.storage import finding_codes
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.services_admin import audit
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.purge import purge

CONTENT = b"%PDF-1.4\n% synthetic purge document\n%%EOF\n"
# point -> (the error, what audit verify reports after the interrupted run)
POINTS = {
    CrashPoint.AUDIT_AFTER_SYNC: ("audit.unavailable", []),  # the intent is undone
    CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC: ("backend.unavailable", [INTENT_WITHOUT_EFFECT]),
    CrashPoint.PURGE_AFTER_COMMIT: ("backend.unavailable", []),  # retracted, keys live
    CrashPoint.SHRED_BEFORE_COMMIT: ("backend.unavailable", []),
    CrashPoint.SHRED_BEFORE_VACUUM: ("backend.unavailable", [finding_codes.KEYS_BLOB_NOT_REMOVED]),
    CrashPoint.PURGE_AFTER_SHRED: ("backend.unavailable", [finding_codes.KEYS_BLOB_NOT_REMOVED]),
}


def setup(tmp_path: Path, faults: FaultSwitch) -> tuple[ServiceEnv, AdminContext, EntityId, str]:
    svc = ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root", version="1.1.0")
    path = tmp_path / "input.pdf"
    path.write_bytes(CONTENT)
    request = RequestMeta.of("file add", {"args": {"path": path.name}})
    result = svc.make_files().add(FileAddRequest(svc.actor, svc.name, path, request))
    (stored,) = svc.txs()[0].blobs
    entity = svc.snapshot().get(next(iter(svc.txs()[0].revisions)).id)
    assert entity is not None and str(entity.id) == result.entity["id"]
    return svc, admin_context(svc.root, faults), entity.id, stored


def run(ctx: AdminContext, entity: EntityId) -> dict[str, object]:
    outcome = purge(
        ctx,
        graph_ref="example_graph",
        refs=[str(entity)],
        with_files=True,
        reason="ticket 7",
        dry_run=False,
    )
    return outcome.data


def codes(ctx: AdminContext) -> list[str]:
    verification = audit.verify(ctx)
    verification.raise_on_errors()
    return sorted({f.code for f in verification.findings})


@pytest.mark.parametrize("point", list(POINTS), ids=str)
def test_a_purge_run_again_completes_an_interrupted_one(tmp_path: Path, point: CrashPoint) -> None:
    faults = FaultSwitch()
    svc, ctx, entity, stored = setup(tmp_path, faults)
    faults.armed.add(point)
    with pytest.raises(GraphCliError) as caught:
        run(ctx, entity)
    error, found = POINTS[point]
    assert caught.value.code == error
    faults.armed.clear()
    assert codes(ctx) == found

    data = run(ctx, entity)
    assert data["purged"]
    revision = svc.snapshot().get(entity)
    assert revision is not None and revision.shredded and revision.record_status == "retracted"
    assert svc.store.blob(stored) is None
    assert set(codes(ctx)) <= {INTENT_WITHOUT_EFFECT}
    assert not run(ctx, entity)["purged"]  # complete: nothing left to do
