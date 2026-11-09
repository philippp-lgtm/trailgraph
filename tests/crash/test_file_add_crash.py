"""K through ``FileService.add``: kill -9 at the blob points and K1, then retry with the key.

Files slice 1, section 4.1 KB0-KB2. The child stages the file, publishes it
at W.11b and commits through the real write pipeline; it dies at the point.
The retry with the same idempotency key commits exactly once and leaves no
orphan blob. A staging file of KB0/KB1 stays until ``files gc``; the orphan
intent of K1 stays an orphan (K.f).
"""

from pathlib import Path

import pytest
from support.procs import spawn
from support.services import ServiceEnv

from graph_cli.audit.finding_codes import INTENT_WITHOUT_EFFECT
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
from graph_cli.storage import finding_codes
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileStoreMaintenance

pytestmark = pytest.mark.slow

KEY = "k-file-crash"
CONTENT = b"%PDF-1.4\n% synthetic crash document\n%%EOF\n"
LEFTOVER = [finding_codes.BLOB_STAGING_LEFTOVER]
# point -> (blob findings after the retry, audit findings after the retry)
ROWS = {
    CrashPoint.BLOB_AFTER_STAGE: (LEFTOVER, []),  # KB0
    CrashPoint.BLOB_BEFORE_RENAME: (LEFTOVER, []),  # KB1
    CrashPoint.BLOB_AFTER_RENAME: ([], []),  # KB2: the orphan is referenced after the retry
    CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC: ([], [INTENT_WITHOUT_EFFECT]),  # K1
}


def blob_codes(svc: ServiceEnv) -> list[str]:
    maintenance = FileStoreMaintenance(svc.root)
    with maintenance.maintenance_session(svc.entry.id, timeout_s=5) as session:
        return [f.code for f in session.verify_blobs(rehash=True)]


@pytest.mark.parametrize("point", list(ROWS), ids=str)
def test_kill_then_retry_adds_once(tmp_path: Path, point: CrashPoint) -> None:
    svc = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.1.0")
    source = tmp_path / "input file.pdf"
    source.write_bytes(CONTENT)
    spec = {"path": str(source), "idempotency_key": KEY}
    child = spawn(
        "write", {"root": str(svc.root), "graph": svc.name, "file": spec, "point": point.value}
    )
    _, err = child.communicate(timeout=60)
    assert child.returncode == -9, err
    assert svc.head_seq() == 0

    result = svc.make_files().add(
        FileAddRequest(
            actor=svc.actor,
            graph=svc.name,
            path=source,
            request=RequestMeta.of("file add", {"args": spec}),
            idempotency_key=KEY,
        )
    )
    assert (result.existing, result.seq) == (False, 1)
    assert [tx.idempotency_key for tx in svc.txs()] == [KEY]
    blobs, audit = ROWS[point]
    assert blob_codes(svc) == blobs
    assert [f.code for f in svc.verify().findings] == audit
