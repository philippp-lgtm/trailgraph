"""The reader's probe of the graph lock (``locking.is_held``): lock-free, creates nothing."""

from pathlib import Path

from graph_cli.storage.files.locking import acquire, is_held


def test_a_lock_file_that_does_not_exist_is_not_held_and_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    assert is_held(path) is False
    assert not path.exists()


def test_a_held_lock_is_seen_and_released_ones_are_not(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    with acquire(path, timeout_s=1):
        assert is_held(path) is True
        assert is_held(path) is True  # the probe does not take the lock away
    assert is_held(path) is False
    with acquire(path, timeout_s=1):  # and a writer is not blocked by an earlier probe
        pass
