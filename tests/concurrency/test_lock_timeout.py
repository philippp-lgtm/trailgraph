"""P1: lock timeout across processes is retryable (contracts B13.8, W.c)."""

from pathlib import Path

import pytest
from support.procs import release, result, spawn, wait_for

pytestmark = pytest.mark.slow


def test_lock_timeout_across_processes(tmp_path: Path) -> None:
    lock, ready, done = tmp_path / ".lock", tmp_path / "ready", tmp_path / "release"
    holder = spawn(
        "lock", {"mode": "hold", "lock": str(lock), "ready": str(ready), "release": str(done)}
    )
    try:
        wait_for(ready)
        contender = result(spawn("lock", {"mode": "try", "lock": str(lock), "timeout_s": 0.3}))
    finally:
        release(done)
    assert result(holder) == {"held": True}
    assert contender["acquired"] is False
    assert (contender["code"], contender["retryable"]) == ("backend.lock_timeout", True)
    assert 0.3 <= contender["elapsed_s"] < 0.3 + 0.5
    # The OS released the holder's lock when it exited; the file stays.
    assert result(spawn("lock", {"mode": "try", "lock": str(lock), "timeout_s": 1}))["acquired"]
    assert lock.exists()


def test_lock_is_released_when_the_holder_dies(tmp_path: Path) -> None:
    lock, ready = tmp_path / ".lock", tmp_path / "ready"
    holder = spawn(
        "lock",
        {
            "mode": "hold",
            "lock": str(lock),
            "ready": str(ready),
            "release": str(tmp_path / "never"),
        },
    )
    wait_for(ready)
    holder.kill()
    holder.wait(timeout=10)
    assert result(spawn("lock", {"mode": "try", "lock": str(lock), "timeout_s": 2}))["acquired"]
