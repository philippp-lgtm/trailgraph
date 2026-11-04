"""Key store of sealed graphs (plan T16, step 3): KEK files, key creation, sealing, shredding,
the raw file after a shred, cache invalidation, rotation, check, collect and backup."""

import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.ids import SeededIdProvider

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TxId, is_uuid7
from graph_cli.kernel.sealing import MAC_KEY_BYTES, KeyRing, KeyUnavailable, Kid
from graph_cli.storage.common import crypto
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files import keystore
from graph_cli.storage.files.keystore import (
    KEYSTORE_FORMAT,
    Kek,
    KeyStore,
    create_kek_file,
    read_kek_file,
)

GRAPH = GraphId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000a1"))
OTHER_GRAPH = GraphId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000a2"))
ENTITY = EntityId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000b1"))
OTHER_ENTITY = EntityId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000b2"))
TX = TxId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000c1"))
PURGE = EventId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000d1"))
OTHER_PURGE = EventId(UUID("0192f6d4-2a10-7b43-a5f6-0000000000d2"))
ABSENT = Kid(UUID("0192f6d4-2a10-7b43-a5f6-0000000000e1"))
KEK = Kek.of(bytes(range(32)))
NEW_KEK = Kek.of(bytes(range(1, 33)))
AAD = b"graph entity rev"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "keys" / f"{GRAPH}.sqlite"
        self.clock = FixedClock()
        self.ids = SeededIdProvider(self.clock)

    def create(self, kek: Kek = KEK) -> KeyStore:
        return KeyStore.create(self.path, GRAPH, kek, clock=self.clock, ids=self.ids)

    def load(self, *keks: Kek, graph: GraphId = GRAPH) -> KeyStore:
        return KeyStore.load(self.path, graph, keks or (KEK,), clock=self.clock, ids=self.ids)

    def rows(self) -> list[tuple[object, ...]]:
        with sqlite3.connect(self.path) as conn:
            return conn.execute(
                "SELECT kid, scope, subject, kek_id, wrapped IS NULL, purge_event FROM dek "
                "ORDER BY kid"
            ).fetchall()

    def wrapped(self, kid: Kid) -> bytes:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT wrapped FROM dek WHERE kid = ?", (kid.bytes,)).fetchone()
        assert isinstance(row[0], bytes)
        return row[0]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# KEK files


def test_kek_id_names_the_key_without_revealing_it() -> None:
    assert KEK.kek_id == Kek.of(bytes(range(32))).kek_id != NEW_KEK.kek_id
    assert len(KEK.kek_id) == 32 and KEK.key.hex()[:32] not in KEK.kek_id
    assert KEK.key.hex() not in repr(KEK)
    with pytest.raises(ValueError, match="32 bytes"):
        Kek.of(b"short")


def test_create_kek_file_writes_a_private_file_and_never_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / "config" / "graph_cli" / "kek"
    kek = create_kek_file(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.read_text() == kek.key.hex() + "\n"
    assert read_kek_file(path) == kek
    with pytest.raises(GraphCliError, match="never replaced") as caught:
        create_kek_file(path)
    assert caught.value.code == "config.invalid"
    assert read_kek_file(path) == kek


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o640, 0o440])
def test_kek_file_owner_and_group_may_read(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "kek"
    path.write_text(KEK.key.hex())
    path.chmod(mode)
    assert read_kek_file(path) == KEK


@pytest.mark.parametrize("mode", [0o604, 0o660, 0o644, 0o610])
def test_kek_file_open_to_others_is_refused(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "kek"
    path.write_text(KEK.key.hex())
    path.chmod(mode)
    with pytest.raises(GraphCliError, match="chmod 600") as caught:
        read_kek_file(path)
    assert caught.value.code == "config.invalid"


@pytest.mark.parametrize(
    "content", [b"", KEK.key, KEK.key.hex().upper().encode(), b"ab" * 31 + b"\n", b"ab" * 33]
)
def test_kek_file_with_other_content_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "kek"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(GraphCliError, match="64 lowercase hex digits") as caught:
        read_kek_file(path)
    assert caught.value.code == "config.invalid"
    assert KEK.key.hex() not in caught.value.message


def test_kek_file_behind_a_symlink_is_read_and_a_directory_refused(tmp_path: Path) -> None:
    target = tmp_path / "..data" / "kek"
    target.parent.mkdir()
    target.write_text(KEK.key.hex() + "\n")
    target.chmod(0o400)
    (tmp_path / "kek").symlink_to(target)
    assert read_kek_file(tmp_path / "kek") == KEK
    with pytest.raises(GraphCliError, match="not a file"):
        read_kek_file(tmp_path / "..data")
    with pytest.raises(GraphCliError, match="cannot be read"):
        read_kek_file(tmp_path / "absent")


# Creating and loading


def test_create_makes_a_private_store_and_load_checks_it(env: Env) -> None:
    env.create().close()
    assert env.path.stat().st_mode & 0o777 == 0o600
    assert env.path.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in env.path.parent.iterdir()] == [env.path.name]
    with sqlite3.connect(env.path) as conn:
        assert dict(conn.execute("SELECT key, value FROM meta")) == {
            "format": KEYSTORE_FORMAT,
            "graph_id": str(GRAPH),
            "shreds": "0",
        }
    env.load().close()
    with pytest.raises(GraphCliError, match="already exists"):
        env.create()
    with pytest.raises(GraphCliError, match="belongs to graph") as caught:
        env.load(graph=OTHER_GRAPH)
    assert caught.value.code == "config.invalid"


def test_load_refuses_a_missing_store_without_creating_one(env: Env) -> None:
    with pytest.raises(GraphCliError, match="restore it from a key store backup") as caught:
        env.load()
    assert caught.value.code == "config.invalid"
    assert not env.path.exists()


def test_load_refuses_an_unknown_format(env: Env) -> None:
    env.create().close()
    with sqlite3.connect(env.path) as conn:
        conn.execute("UPDATE meta SET value = 'graph_cli.keystore/9' WHERE key = 'format'")
    with pytest.raises(GraphCliError) as caught:
        env.load()
    assert caught.value.code == "format.unsupported_version"


def test_a_file_that_is_no_database_is_backend_unavailable(env: Env) -> None:
    env.path.parent.mkdir()
    env.path.write_bytes(b"not a database" * 100)
    with pytest.raises(GraphCliError) as caught:
        env.load()
    assert caught.value.code == "backend.unavailable"


# Keys


def test_the_store_is_a_key_ring(env: Env) -> None:
    with env.create() as store:
        ring: KeyRing = store
        assert ring.mac_key(None) is None


def test_subject_keys_are_created_once_and_file_keys_every_time(env: Env) -> None:
    with env.create() as store:
        entity = store.entity_key(ENTITY)
        assert store.entity_key(ENTITY) == entity
        assert store.entity_key(OTHER_ENTITY) != entity
        tx = store.tx_key(TX)
        assert tx not in (entity, store.entity_key(OTHER_ENTITY))
        files = {store.file_key(), store.file_key()}
        assert len(files) == 2
    assert all(is_uuid7(kid) for kid in (entity, *files))
    with env.load() as again:
        assert again.entity_key(ENTITY) == entity
        assert again.tx_key(TX) == tx
    scopes = sorted(str(row[1]) for row in env.rows())
    assert scopes == ["entity", "entity", "file", "file", "tx"]


def test_seal_and_open_round_trip_across_instances(env: Env) -> None:
    with env.create() as store:
        kid = store.entity_key(ENTITY)
        sealed = store.seal(kid, b'{"name":"marker"}', AAD)
        mac = store.mac_key(kid)
        assert sealed.kid == kid and b"marker" not in sealed.ct
    with env.load() as again:
        assert again.open(sealed, AAD) == b'{"name":"marker"}'
        assert again.mac_key(kid) == mac
        assert mac is not None and len(mac) == MAC_KEY_BYTES
        with pytest.raises(crypto.CryptoError):
            again.open(sealed, b"another place")


def test_a_missing_key_is_unavailable(env: Env) -> None:
    with env.create() as store:
        for call in (
            lambda: store.mac_key(ABSENT),
            lambda: store.seal(ABSENT, b"x", AAD),
        ):
            with pytest.raises(KeyUnavailable) as caught:
                call()
            assert (caught.value.kid, caught.value.reason) == (ABSENT, "missing")
        assert not store.is_shredded(ABSENT)


def test_a_damaged_wrapped_key_is_backend_corrupt(env: Env) -> None:
    with env.create() as store:
        kid = store.entity_key(ENTITY)
    with sqlite3.connect(env.path) as conn:
        conn.execute("UPDATE dek SET wrapped = ? WHERE kid = ?", (bytes(40), kid.bytes))
    with env.load() as again, pytest.raises(GraphCliError) as caught:
        again.mac_key(kid)
    assert caught.value.code == "backend.corrupt"


def test_a_fault_before_the_commit_creates_no_key(env: Env) -> None:
    def fail(point: CrashPoint) -> None:
        if point is CrashPoint.KEYS_BEFORE_COMMIT:
            raise OSError("disk full")

    env.create().close()
    store = KeyStore.load(env.path, GRAPH, (KEK,), clock=env.clock, ids=env.ids, faults=fail)
    with store, pytest.raises(OSError, match="disk full"):
        store.entity_key(ENTITY)
    assert env.rows() == []


# Rotation


def test_keys_unwrap_under_their_own_kek_and_new_keys_take_the_current_one(env: Env) -> None:
    with env.create() as store:
        old = store.entity_key(ENTITY)
        sealed = store.seal(old, b"old value", AAD)
    with env.load(NEW_KEK) as wrong, pytest.raises(GraphCliError, match="not configured") as caught:
        wrong.open(sealed, AAD)
    assert caught.value.code == "config.invalid"
    with env.load(NEW_KEK, KEK) as rotating:
        assert rotating.open(sealed, AAD) == b"old value"
        new = rotating.entity_key(OTHER_ENTITY)
    kek_ids = {row[0]: row[3] for row in env.rows()}
    assert kek_ids == {old.bytes: KEK.kek_id, new.bytes: NEW_KEK.kek_id}
    with sqlite3.connect(env.path) as conn:
        assert {row[0] for row in conn.execute("SELECT kek_id FROM kek")} == {
            KEK.kek_id,
            NEW_KEK.kek_id,
        }


# Shredding


def test_shred_destroys_the_key_and_keeps_the_evidence(env: Env) -> None:
    with env.create() as store:
        kid = store.entity_key(ENTITY)
        other = store.entity_key(OTHER_ENTITY)
        sealed = store.seal(kid, b"marker", AAD)
        store.shred([kid], PURGE)
        assert store.is_shredded(kid) and not store.is_shredded(other)
        for call in (
            lambda: store.open(sealed, AAD),
            lambda: store.mac_key(kid),
            lambda: store.seal(kid, b"x", AAD),
        ):
            with pytest.raises(KeyUnavailable) as caught:
                call()
            assert caught.value.reason == "shredded"
        # The subject keeps its shredded key: a purged entity cannot be written again.
        assert store.entity_key(ENTITY) == kid
        store.shred([kid], OTHER_PURGE)  # a re-run keeps the first purge event
        store.mac_key(other)
    row = next(r for r in env.rows() if r[0] == kid.bytes)
    assert row == (kid.bytes, "entity", ENTITY.bytes, KEK.kek_id, 1, PURGE.bytes)


def test_shred_of_an_absent_key_changes_nothing(env: Env) -> None:
    with env.create() as store:
        kid = store.entity_key(ENTITY)
        with pytest.raises(KeyUnavailable) as caught:
            store.shred([kid, ABSENT], PURGE)
        assert (caught.value.kid, caught.value.reason) == (ABSENT, "missing")
        assert not store.is_shredded(kid)


def test_a_fault_before_the_shred_commit_leaves_the_key(env: Env) -> None:
    def fail(point: CrashPoint) -> None:
        if point is CrashPoint.SHRED_BEFORE_COMMIT:
            raise OSError("disk full")

    with env.create() as store:
        kid = store.entity_key(ENTITY)
    faulty = KeyStore.load(env.path, GRAPH, (KEK,), clock=env.clock, ids=env.ids, faults=fail)
    with faulty, pytest.raises(OSError):
        faulty.shred([kid], PURGE)
    with env.load() as again:
        assert not again.is_shredded(kid)
        again.mac_key(kid)


def test_no_byte_of_a_shredded_key_stays_in_the_file(env: Env) -> None:
    with env.create() as store:
        kids = [store.entity_key(ENTITY), store.tx_key(TX), store.file_key()]
        wrapped = [env.wrapped(kid) for kid in kids]
        deks = [crypto.unwrap_key(KEK.key, w) for w in wrapped]
        survivor = store.entity_key(OTHER_ENTITY)
        raw = env.path.read_bytes()
        assert all(w in raw for w in wrapped)
        store.shred(kids, PURGE)
    raw = env.path.read_bytes()
    for secret in (*wrapped, *deks):
        assert secret not in raw
    assert env.wrapped(survivor) in raw
    assert sorted(p.name for p in env.path.parent.iterdir()) == [env.path.name]


def test_a_shred_in_another_process_reaches_a_cached_key(env: Env) -> None:
    with env.create() as reader:
        kid = reader.entity_key(ENTITY)
        sealed = reader.seal(kid, b"marker", AAD)
        assert reader.open(sealed, AAD) == b"marker"  # cached now
        with env.load() as purger:
            purger.shred([kid], PURGE)
        reader.refresh()
        with pytest.raises(KeyUnavailable):
            reader.open(sealed, AAD)


def test_cached_keys_are_checked_again_after_a_while(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(keystore.time, "monotonic", lambda: now[0])
    with env.create() as reader:
        kid = reader.entity_key(ENTITY)
        sealed = reader.seal(kid, b"marker", AAD)
        with env.load() as purger:
            purger.shred([kid], PURGE)
        assert reader.open(sealed, AAD) == b"marker"  # within CACHE_CHECK_S
        now[0] += keystore.CACHE_CHECK_S
        with pytest.raises(KeyUnavailable):
            reader.open(sealed, AAD)


def test_a_key_created_elsewhere_is_found_not_duplicated(env: Env) -> None:
    with env.create() as first, env.load() as second:
        assert second.entity_key(ENTITY) == first.entity_key(ENTITY)
        first.tx_key(TX)
        assert second.tx_key(TX) == first.tx_key(TX)
    assert len(env.rows()) == 2


# Check and collect


def test_check_reports_missing_unrecorded_and_orphan_keys(env: Env) -> None:
    with env.create() as store:
        used = store.entity_key(ENTITY)
        orphan = store.file_key()
        recorded = store.tx_key(TX)
        unrecorded = store.entity_key(OTHER_ENTITY)
        store.shred([recorded], PURGE)
        store.shred([unrecorded], OTHER_PURGE)
        found = store.check([used, recorded, ABSENT], {PURGE: [recorded], OTHER_PURGE: []})
    assert found.missing == (ABSENT,)
    assert found.shredded_unrecorded == (unrecorded,)
    assert found.orphans == (orphan,)
    with env.load() as again:
        clean = again.check(
            [used, orphan, recorded], {PURGE: [recorded], OTHER_PURGE: [unrecorded]}
        )
    assert (clean.missing, clean.shredded_unrecorded, clean.orphans) == ((), (), ())


def test_collect_deletes_old_live_orphans_only(env: Env) -> None:
    with env.create() as store:
        old = store.file_key()
        shredded = store.file_key()
        store.shred([shredded], PURGE)
        env.clock.advance(timedelta(hours=1))
        cutoff = env.clock.now()
        recent = store.file_key()
        assert store.collect([old, shredded, recent], created_before=cutoff) == 1
        with pytest.raises(KeyUnavailable) as caught:
            store.mac_key(old)
        assert caught.value.reason == "missing"
        store.mac_key(recent)
    assert {row[0] for row in env.rows()} == {shredded.bytes, recent.bytes}


# Backup


def test_backup_is_a_private_loadable_copy(env: Env, tmp_path: Path) -> None:
    dest = tmp_path / "backups" / "keys.sqlite"
    dest.parent.mkdir()
    with env.create() as store:
        kid = store.entity_key(ENTITY)
        sealed = store.seal(kid, b"marker", AAD)
        store.backup(dest)
        with pytest.raises(GraphCliError, match="already exists"):
            store.backup(dest)
    assert dest.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]
    copy = KeyStore.load(dest, GRAPH, (KEK,), clock=env.clock, ids=env.ids)
    with copy:
        assert copy.open(sealed, AAD) == b"marker"


def test_a_backup_after_a_shred_holds_no_byte_of_the_key(env: Env, tmp_path: Path) -> None:
    dest = tmp_path / "keys-after.sqlite"
    with env.create() as store:
        kid = store.entity_key(ENTITY)
        wrapped = env.wrapped(kid)
        store.shred([kid], PURGE)
        store.backup(dest)
    assert wrapped not in dest.read_bytes()
    assert not os.access(dest, os.X_OK)
