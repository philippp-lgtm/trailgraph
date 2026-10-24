"""K through the write path: kill -9 at every crash point, then retry with the key.

Contracts section 4.1 (K.a, K.e, K.f), B3.12. The child runs the real
WriteService (system clock, UUIDv7, FileCatalog with a SIGKILL fault hook)
and dies at the point. The caller never got an answer, so it retries the
same change set with the same idempotency key: the tx exists exactly once
afterwards, as a replay when the kill came after the commit point. The
verify column: an orphan intent (``intent_without_effect``, info) remains
when the kill hit between intent and tx, everything else verifies clean.
"""

from pathlib import Path

import pytest
from support.procs import spawn
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.audit.finding_codes import INTENT_WITHOUT_EFFECT
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.storage.common.faults import CrashPoint

pytestmark = pytest.mark.slow

KEY = "k-crash"
COMMITTED = {
    CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC,  # K.e: the line is complete after kill -9
    CrashPoint.TX_AFTER_SYNC,
    CrashPoint.MATERIALIZE_AFTER_FIRST_FILE,
    CrashPoint.MATERIALIZE_BEFORE_DIR_SYNC,
    CrashPoint.HEAD_BEFORE_RENAME,
    CrashPoint.HEAD_AFTER_RENAME,
    CrashPoint.INDEX_BEFORE_RENAME,
}
ORPHAN = {
    CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC,
    CrashPoint.AUDIT_AFTER_SYNC,
    CrashPoint.TX_BEFORE_WRITE,
}
POINTS = [CrashPoint.AUDIT_BEFORE_WRITE, *sorted(ORPHAN), *sorted(COMMITTED)]


def verify_codes(svc: ServiceEnv) -> list[str]:
    return [f.code for f in svc.verify().findings]


@pytest.mark.parametrize("point", POINTS, ids=str)
def test_kill_then_retry_commits_once(tmp_path: Path, point: CrashPoint) -> None:
    svc = ServiceEnv("files", "inventory", tmp_path / "root")
    cls = load_roles("inventory").term("lifecycle_type")
    svc.create(cls, "base")
    base = svc.head_seq()
    document = {"ops": svc.factory.create(cls, "k"), "idempotency_key": KEY}
    child = spawn(
        "write",
        {"root": str(svc.root), "graph": svc.name, "documents": [document], "point": point.value},
    )
    _, err = child.communicate(timeout=60)
    assert child.returncode == -9, err

    committed = point in COMMITTED
    assert svc.head_seq() == base + committed  # lock-free reader (B4.10)
    expected = [INTENT_WITHOUT_EFFECT] if point in ORPHAN else []
    assert verify_codes(svc) == expected

    result = svc.service.apply(
        svc.request(*document["ops"], idempotency_key=KEY)  # type: ignore[arg-type]
    )
    assert result.idempotent_replay is committed
    assert svc.head_seq() == base + 1
    assert [tx.idempotency_key for tx in svc.txs()].count(KEY) == 1
    assert verify_codes(svc) == expected  # the orphan stays an orphan (K.f)
    assert check_graph(svc.model, svc.snapshot()) == []
