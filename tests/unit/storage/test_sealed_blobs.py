"""Stored files of a sealed FileStore (plan T16, step 5): reads check what they decrypt."""

import hashlib
import os
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.stores import StoreEnv

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId
from graph_cli.storage.common.crypto import BLOB_CHUNK, BLOB_HEADER_BYTES, parse_blob_header
from graph_cli.storage.files.store import FileStore

CONTENT = bytes(i % 251 for i in range(3 * BLOB_CHUNK + 17))


@pytest.fixture
def env(tmp_path: Path) -> StoreEnv:
    return StoreEnv("files-sealed", "notes_and_sources", tmp_path / "root")


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def stored(env: StoreEnv, content: bytes = CONTENT) -> str:
    tx, _ = env.write_blobs(lambda d: d.create(), [content])
    (name,) = tx.blobs
    return name


def replace_bytes(store: FileStore, name: str, data: bytes) -> None:
    path = store.layout.blob_path(name)
    os.chmod(path, 0o644)
    path.write_bytes(data)


def read_until_failure(store: FileStore, name: str) -> tuple[list[bytes], GraphCliError]:
    received: list[bytes] = []
    with pytest.raises(GraphCliError) as info:
        for chunk in store.read_blob(name):
            received.append(chunk)
    return received, info.value


def test_a_sealed_blob_reads_back_in_chunks(env: StoreEnv) -> None:
    store = store_of(env)
    name = stored(env)
    chunks = list(store.read_blob(name))
    assert b"".join(chunks) == CONTENT and len(chunks) == 4
    info = store.blob(name)
    assert info is not None and info.size == len(CONTENT)
    assert store.blob_local_path(name) is None


def test_another_blob_under_this_name_is_refused_before_its_last_chunk(env: StoreEnv) -> None:
    """A valid blob of the same graph renamed into place decrypts under its own key; the
    ciphertext hash check fails, and the last chunk is never handed out."""
    store = store_of(env)
    name = stored(env)
    other = stored(env, bytes(reversed(CONTENT)))
    replace_bytes(store, name, store.layout.blob_path(other).read_bytes())
    received, error = read_until_failure(store, name)
    assert (error.code, error.details["reason"]) == ("backend.corrupt", "invalid")
    assert len(received) == 3  # the last of four chunks was held back


@pytest.mark.parametrize("damage", ["append", "truncate", "flip"])
def test_damaged_bytes_are_refused(env: StoreEnv, damage: str) -> None:
    store = store_of(env)
    name = stored(env)
    data = bytearray(store.layout.blob_path(name).read_bytes())
    if damage == "append":
        data += b"\x00"
    elif damage == "truncate":
        del data[-1]
    else:
        data[len(data) // 2] ^= 1
    replace_bytes(store, name, bytes(data))
    _, error = read_until_failure(store, name)
    assert (error.code, error.details["reason"]) == ("backend.corrupt", "invalid")


def test_a_shredded_file_key_is_reported_before_the_first_chunk(env: StoreEnv) -> None:
    store = store_of(env)
    name = stored(env)
    header = store.layout.blob_path(name).read_bytes()[:BLOB_HEADER_BYTES]
    file_kid, _, _ = parse_blob_header(header)
    store.key_ring().shred([file_kid], EventId(env.ids.new_id()))
    with pytest.raises(GraphCliError) as info:
        store.read_blob(name)
    assert info.value.details == {"blob": name, "kid": str(file_kid), "reason": "shredded"}


def test_staging_and_reading_stream_in_bounded_memory(env: StoreEnv) -> None:
    size = 48 * 1024 * 1024
    piece = b"\x5a" * (1024 * 1024)

    def chunks() -> Iterator[bytes]:
        for _ in range(size // len(piece)):
            yield piece

    store = store_of(env)
    tracemalloc.start()
    try:
        staged = store.stage_blob(chunks(), max_bytes=size)
        with store.session(timeout_s=5) as session:
            name = session.publish_blob(staged).hash
        hasher = hashlib.sha256()
        for chunk in store.read_blob(name):
            hasher.update(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert staged.size == size
    assert "sha256:" + hasher.hexdigest() == staged.hash
    assert peak < 8 * 1024 * 1024, peak
