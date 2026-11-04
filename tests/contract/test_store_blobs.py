"""Blob port contract (files slice 1: stage, publish, read; tx ``blobs``; B13.2 capability)."""

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import replace

import pytest
from support.stores import StoreEnv, snapshot_state

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import CAPABILITY_BLOB_LOCAL_PATH
from graph_cli.kernel.records import BlobInfo, tx_to_json
from graph_cli.storage.files.layout import STAGING_PREFIX
from graph_cli.storage.files.store import FileStore

CONTENT = b"%PDF-1.4\nsynthetic test bytes\n"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()


def _staging(env: StoreEnv) -> list[str]:
    if not isinstance(env.store, FileStore) or not env.store.layout.blobs.is_dir():
        return []
    return sorted(n for n in os.listdir(env.store.layout.blobs) if n.startswith(STAGING_PREFIX))


def test_stage_publish_read(env: StoreEnv) -> None:
    staged = env.store.stage_blob([CONTENT[:5], CONTENT[5:]], max_bytes=len(CONTENT))
    assert (staged.hash, staged.size) == (DIGEST, len(CONTENT))
    assert env.store.blob(DIGEST) is None  # staged is not stored
    with env.store.session(timeout_s=5) as session:
        assert session.publish_blob(staged) == BlobInfo(DIGEST, len(CONTENT))
    assert env.store.blob(DIGEST) == BlobInfo(DIGEST, len(CONTENT))
    assert b"".join(env.store.read_blob(DIGEST)) == CONTENT
    assert _staging(env) == []


def test_publishing_known_content_keeps_the_stored_blob(env: StoreEnv) -> None:
    first = env.store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    second = env.store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    with env.store.session(timeout_s=5) as session:
        session.publish_blob(first)
        assert session.publish_blob(second) == BlobInfo(DIGEST, len(CONTENT))
    assert b"".join(env.store.read_blob(DIGEST)) == CONTENT
    assert _staging(env) == []


def test_discarded_blobs_are_not_published(env: StoreEnv) -> None:
    staged = env.store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    staged.discard()
    staged.discard()  # idempotent
    assert _staging(env) == []
    with env.store.session(timeout_s=5) as session, pytest.raises(GraphCliError) as info:
        session.publish_blob(staged)
    assert info.value.code == "internal.error"
    assert env.store.blob(DIGEST) is None


def test_too_large_is_refused_while_reading(env: StoreEnv) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 8
        yield b"x" * 8
        raise AssertionError("read past the limit")

    with pytest.raises(GraphCliError) as info:
        env.store.stage_blob(chunks(), max_bytes=10)
    assert info.value.code == "file.too_large"
    assert info.value.details == {"max_bytes": 10}
    assert _staging(env) == []
    env.store.stage_blob([b"x" * 10], max_bytes=10).discard()  # the limit is inclusive


def test_absent_blobs(env: StoreEnv) -> None:
    assert env.store.blob(DIGEST) is None
    assert env.store.blob("not a hash") is None
    with pytest.raises(GraphCliError) as info:
        env.store.read_blob(DIGEST)
    assert info.value.code == "backend.corrupt"
    assert env.store.blob_local_path(DIGEST) is None


def test_tx_blobs_round_trip(env: StoreEnv) -> None:
    other = b"plain text\n"
    tx, _ = env.write_blobs(lambda d: d.create(), [CONTENT, other])
    expected = tuple(sorted({DIGEST, "sha256:" + hashlib.sha256(other).hexdigest()}))
    assert tx.blobs == expected
    (record,) = env.store.changes_since(0)
    assert record.blobs == expected
    assert tx_to_json(record)["blobs"] == list(expected)


def test_txs_without_blobs_keep_their_bytes(env: StoreEnv) -> None:
    env.write(lambda d: d.create())
    (record,) = env.store.changes_since(0)
    assert record.blobs == ()
    assert "blobs" not in tx_to_json(record)


def test_a_tx_naming_an_unstored_blob_is_refused(env: StoreEnv) -> None:
    with env.store.session(timeout_s=5) as session:
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        tx = replace(draft.build(), blobs=(DIGEST,))
        receipt = session.append_audit(env.intent(tx, session.snapshot()))
        with pytest.raises(GraphCliError) as info:
            session.commit(tx, receipt)
    assert (info.value.code, info.value.details["rule"]) == ("internal.error", "blobs")
    assert env.store.head().seq == 0


def test_local_path_is_a_capability_of_the_file_store(env: StoreEnv) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    path = env.store.blob_local_path(DIGEST)
    if env.backend.startswith("memory"):
        assert CAPABILITY_BLOB_LOCAL_PATH not in env.store.capabilities()
        assert path is None
        return
    assert CAPABILITY_BLOB_LOCAL_PATH in env.store.capabilities()
    assert path is not None and path.read_bytes() == CONTENT
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert path.relative_to(env.root).parts[2:4] == ("blobs", "sha256")
    assert path.parent.name == DIGEST.removeprefix("sha256:")[:2]
