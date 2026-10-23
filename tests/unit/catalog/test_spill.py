"""catalog.spill: the private directory, exclusive files and pruning (contracts B9.2)."""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from graph_cli.catalog.spill import KEEP_SECONDS, SpillFiles

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


def test_files_are_private_and_named_by_graph_command_and_time(tmp_path: Path) -> None:
    root = tmp_path / "parent" / "spill"
    path = Path(SpillFiles(root, NOW).write("example_graph", "describe commands", b"{}\n"))
    assert path.parent == root / "example_graph"
    assert path.name.startswith("describe_commands_20261023T120000Z_") and path.suffix == ".json"
    assert path.read_bytes() == b"{}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    other = Path(SpillFiles(root, NOW).write(None, None, b"x"))
    assert other.parent == root and other.name.startswith("graph_")


def test_old_files_are_pruned_from_the_same_directory(tmp_path: Path) -> None:
    files = SpillFiles(tmp_path / "spill", NOW)
    old = Path(files.write("g", "find", b"old"))
    kept = Path(files.write("g", "find", b"kept"))
    elsewhere = Path(files.write("h", "find", b"other graph"))
    stamp = NOW.timestamp() - KEEP_SECONDS - 1
    for path in (old, elsewhere):
        os.utime(path, (stamp, stamp))
    files.write("g", "get", b"new")
    assert not old.exists() and kept.exists() and elsewhere.exists()


@pytest.mark.parametrize("problem", ["open", "symlink", "file"])
def test_a_directory_that_is_not_private_is_refused(tmp_path: Path, problem: str) -> None:
    root = tmp_path / "spill"
    if problem == "open":
        root.mkdir()
        root.chmod(0o755)
    elif problem == "symlink":
        (tmp_path / "target").mkdir(mode=0o700)
        root.symlink_to(tmp_path / "target")
    else:
        root.write_text("")
    with pytest.raises(OSError):
        SpillFiles(root, NOW).write("g", "find", b"{}")
    assert not (tmp_path / "target" / "g").exists()
