"""P for the key store (plan T16, step 3): processes racing for keys get one key per subject
and lose none."""

from pathlib import Path
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.ids import SeededIdProvider
from support.procs import release, result, spawn

from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.sealing import Kid
from graph_cli.storage.files.keystore import KeyCheck, KeyStore, create_kek_file

pytestmark = pytest.mark.slow

GRAPH = GraphId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000a1"))
ENTITY = "0192f6d4-2a10-7b43-a5f6-0000000000b1"


def test_racing_processes_share_subject_keys_and_lose_none(tmp_path: Path) -> None:
    clock = FixedClock()
    kek = create_kek_file(tmp_path / "kek")
    path = tmp_path / "keys.sqlite"
    KeyStore.create(path, GRAPH, kek, clock=clock, ids=SeededIdProvider(clock)).close()
    barrier = tmp_path / "go"
    procs = [
        spawn(
            "keys",
            {
                "path": str(path),
                "graph_id": str(GRAPH),
                "kek_file": str(tmp_path / "kek"),
                "mode": "race",
                "barrier": str(barrier),
                "entity": ENTITY,
                "txs": [f"0192f6d4-2a10-7b43-a5f6-{n:06d}{t:06d}" for t in range(10)],
                "files": 10,
            },
        )
        for n in range(6)
    ]
    release(barrier)
    outcomes = [result(p) for p in procs]
    shared = {kid for o in outcomes for kid in o["shared"]}
    assert len(shared) == 1
    own = [kid for o in outcomes for kid in o["own"] + o["files"]]
    assert len(set(own)) == len(own) == 6 * 20
    kids = [Kid(UUID(k)) for k in (*shared, *own)]
    with KeyStore.load(path, GRAPH, (kek,), clock=clock, ids=SeededIdProvider(clock)) as store:
        assert store.check(kids, {}) == KeyCheck((), (), ())
        assert all(store.mac_key(kid) for kid in kids)
