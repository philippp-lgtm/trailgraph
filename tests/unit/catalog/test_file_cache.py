"""This machine's file cache (object storage O8): read-only copies, unique partial files,
the use time refreshed on every use, a day's expiry, two fetches of one file at once."""

import hashlib
import os
import stat
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
from support.clock import FixedClock

from graph_cli.catalog.file_cache import COPY_MODE, UNUSED_FOR, StateFileCache
from graph_cli.kernel.errors import GraphCliError

GRAPH = "0192e000-0000-7000-8000-0000000000a1"
DATA = b"stored bytes\n" * 100
DIGEST = "sha256:" + hashlib.sha256(DATA).hexdigest()


def writes(data: bytes, calls: list[int] | None = None) -> Callable[[BinaryIO], None]:
    def fill(sink: BinaryIO) -> None:
        if calls is not None:
            calls.append(1)
        sink.write(data)

    return fill


def entries(cache: StateFileCache) -> list[str]:
    return sorted(p.name for p in (cache.root / GRAPH).iterdir())


def test_a_copy_is_read_only_and_hash_checked(tmp_path: Path) -> None:
    clock = FixedClock()
    cache = StateFileCache(tmp_path / "state", clock)
    path = cache.copy(GRAPH, DIGEST, writes(DATA))
    assert path == tmp_path / "state" / "files" / GRAPH / DIGEST.removeprefix("sha256:")
    assert path.read_bytes() == DATA
    assert stat.S_IMODE(path.stat().st_mode) == COPY_MODE == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.stat().st_mtime == clock.now().timestamp()
    assert entries(cache) == [path.name]  # no partial file left


def test_bytes_without_their_hash_never_become_a_copy(tmp_path: Path) -> None:
    cache = StateFileCache(tmp_path / "state", FixedClock())
    with pytest.raises(GraphCliError) as caught:
        cache.copy(GRAPH, DIGEST, writes(b"other bytes"))
    assert caught.value.code == "backend.corrupt"
    own = GraphCliError("remote.protocol", "not its hash")
    with pytest.raises(GraphCliError) as named:
        cache.copy(GRAPH, DIGEST, writes(b"other bytes"), mismatch=lambda: own)
    assert named.value is own
    assert entries(cache) == []

    def broken(sink: BinaryIO) -> None:
        sink.write(DATA[:10])
        raise GraphCliError("backend.unavailable", "the store went away")

    with pytest.raises(GraphCliError):
        cache.copy(GRAPH, DIGEST, broken)
    assert entries(cache) == []


def test_reuse_refreshes_the_use_time_without_fetching(tmp_path: Path) -> None:
    clock = FixedClock()
    cache = StateFileCache(tmp_path / "state", clock)
    calls: list[int] = []
    path = cache.copy(GRAPH, DIGEST, writes(DATA, calls))
    clock.advance(timedelta(hours=5))
    assert cache.copy(GRAPH, DIGEST, writes(DATA, calls)) == path
    assert calls == [1]
    assert path.stat().st_mtime == clock.now().timestamp()


def test_a_changed_or_older_copy_is_replaced(tmp_path: Path) -> None:
    cache = StateFileCache(tmp_path / "state", FixedClock())
    calls: list[int] = []
    path = cache.copy(GRAPH, DIGEST, writes(DATA, calls))
    os.chmod(path, 0o600)
    path.write_bytes(b"edited by hand")
    assert cache.copy(GRAPH, DIGEST, writes(DATA, calls)).read_bytes() == DATA
    assert calls == [1, 1]
    os.chmod(path, 0o444)  # a copy written by an older version
    assert cache.copy(GRAPH, DIGEST, writes(DATA, calls)) == path
    assert calls == [1, 1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


def test_copies_unused_for_a_day_are_pruned(tmp_path: Path) -> None:
    clock = FixedClock()
    cache = StateFileCache(tmp_path / "state", clock)
    old = cache.copy(GRAPH, DIGEST, writes(DATA))
    other = b"other"
    other_digest = "sha256:" + hashlib.sha256(other).hexdigest()
    clock.advance(timedelta(hours=20))
    kept = cache.copy(GRAPH, other_digest, writes(other))
    leftover = old.parent / f".{DIGEST.removeprefix('sha256:')}.partial-dead"
    leftover.write_bytes(b"half")
    os.utime(leftover, (old.stat().st_mtime, old.stat().st_mtime))
    foreign = old.parent / "notes.txt"  # not the cache's: never touched
    foreign.write_text("x")
    os.utime(foreign, (0, 0))
    clock.advance(UNUSED_FOR - timedelta(hours=20) - timedelta(seconds=1))
    cache.prune()
    assert old.exists() and leftover.exists()
    clock.advance(timedelta(seconds=1))
    cache.prune()
    assert not old.exists() and not leftover.exists()
    assert kept.exists() and foreign.exists()
    foreign.unlink()
    clock.advance(UNUSED_FOR)
    cache.prune()
    assert not (cache.root / GRAPH).exists()  # an empty graph directory goes too
    StateFileCache(tmp_path / "nothing", clock).prune()  # no cache yet: nothing to do


def test_two_fetches_of_one_file_at_once(tmp_path: Path) -> None:
    """Each fetch writes its own partial file; both end with the same copy."""
    cache = StateFileCache(tmp_path / "state", FixedClock())
    both = threading.Barrier(2, timeout=10)
    partials: list[list[str]] = []

    def fill(sink: BinaryIO) -> None:
        sink.write(DATA[:100])
        sink.flush()
        both.wait()  # both partial files exist now
        partials.append(entries(cache))
        sink.write(DATA[100:])

    results: list[Path] = []
    errors: list[BaseException] = []

    def fetch() -> None:
        try:
            results.append(cache.copy(GRAPH, DIGEST, fill))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert results[0].read_bytes() == DATA
    assert all(len([n for n in seen if ".partial-" in n]) == 2 for seen in partials)
    assert entries(cache) == [results[0].name]


def test_only_ids_and_hashes_become_paths(tmp_path: Path) -> None:
    cache = StateFileCache(tmp_path / "state", FixedClock())
    for graph, digest in (("../x", DIGEST), (GRAPH, "sha256:../../etc"), ("", DIGEST)):
        with pytest.raises(GraphCliError) as caught:
            cache.copy(graph, digest, writes(DATA))
        assert caught.value.code == "internal.error"
