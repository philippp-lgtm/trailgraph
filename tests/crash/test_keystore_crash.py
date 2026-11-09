"""K for the key store (plan T16, step 3): kill -9 before and after the commit of a new key
and of a shred.

The child loads the key store and sends itself SIGKILL at the point. A key is durable once
its commit returned and absent before; a shred is complete once its commit returned (no
byte of the wrapped key is left, ``VACUUM`` or not) and absent before.
"""

import signal
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.ids import SeededIdProvider
from support.procs import spawn

from graph_cli.kernel.ids import EntityId, EventId, GraphId
from graph_cli.kernel.sealing import KeyUnavailable, Kid
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.keystore import KeyStore, create_kek_file, read_kek_file

pytestmark = pytest.mark.slow

GRAPH = GraphId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000a1"))
ENTITY = EntityId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000b1"))
PURGE = EventId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000d1"))


def run(tmp_path: Path, mode: str, point: CrashPoint, **extra: str) -> int:
    proc = spawn(
        "keys",
        {
            "path": str(tmp_path / "keys.sqlite"),
            "graph_id": str(GRAPH),
            "kek_file": str(tmp_path / "kek"),
            "mode": mode,
            "point": point.value,
            **extra,
        },
    )
    proc.communicate(timeout=60)
    return proc.returncode


def create(tmp_path: Path) -> None:
    clock = FixedClock()
    kek = create_kek_file(tmp_path / "kek")
    KeyStore.create(
        tmp_path / "keys.sqlite", GRAPH, kek, clock=clock, ids=SeededIdProvider(clock)
    ).close()


def load(tmp_path: Path) -> KeyStore:
    clock = FixedClock()
    kek = read_kek_file(tmp_path / "kek")
    return KeyStore.load(
        tmp_path / "keys.sqlite", GRAPH, (kek,), clock=clock, ids=SeededIdProvider(clock, 1)
    )


def wrapped(tmp_path: Path, kid: Kid) -> bytes:
    with closing(sqlite3.connect(tmp_path / "keys.sqlite")) as conn:
        row = conn.execute("SELECT wrapped FROM dek WHERE kid = ?", (kid.bytes,)).fetchone()
    return bytes(row[0])


@pytest.mark.parametrize(
    ("point", "durable"),
    [(CrashPoint.KEYS_BEFORE_COMMIT, False), (CrashPoint.KEYS_AFTER_COMMIT, True)],
    ids=str,
)
def test_kill_while_creating_a_key(tmp_path: Path, point: CrashPoint, durable: bool) -> None:
    create(tmp_path)
    assert run(tmp_path, "mint", point, entity=str(ENTITY)) == -signal.SIGKILL
    with load(tmp_path) as store:
        found = store.check([], {})
        assert len(found.orphans) == int(durable)
        kid = store.entity_key(ENTITY)  # the durable key, or a new one
        assert (kid in found.orphans) == durable
        assert store.check([kid], {}).orphans == ()
        store.mac_key(kid)


@pytest.mark.parametrize(
    ("point", "shredded"),
    [(CrashPoint.SHRED_BEFORE_COMMIT, False), (CrashPoint.SHRED_BEFORE_VACUUM, True)],
    ids=str,
)
def test_kill_while_shredding(tmp_path: Path, point: CrashPoint, shredded: bool) -> None:
    create(tmp_path)
    with load(tmp_path) as store:
        kid = store.entity_key(ENTITY)
        sealed = store.seal(kid, b"marker", b"aad")
    secret = wrapped(tmp_path, kid)
    assert run(tmp_path, "shred", point, kid=str(kid), event=str(PURGE)) == -signal.SIGKILL
    with load(tmp_path) as store:
        assert store.is_shredded(kid) == shredded
        if shredded:
            with pytest.raises(KeyUnavailable):
                store.open(sealed, b"aad")
            # secure_delete zeroed the wrapped key at the commit, before any VACUUM.
            assert secret not in (tmp_path / "keys.sqlite").read_bytes()
            store.shred([kid], PURGE)  # a re-run completes the purge
        else:
            assert store.open(sealed, b"aad") == b"marker"
        assert store.check([kid], {PURGE: [kid]}).shredded_unrecorded == ()
