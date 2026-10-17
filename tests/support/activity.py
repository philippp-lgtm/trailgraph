"""Short directories for activity sockets.

A Unix socket path is limited to 104 bytes on macOS; pytest's ``tmp_path``
is often longer, so socket tests use a short temp directory instead.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="gv"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
