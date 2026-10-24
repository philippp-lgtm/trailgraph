"""Uploads of graph-server: names, per-token staging and the pending quota (B14.6)."""

import io
from pathlib import Path

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli_server.uploads import PENDING_UPLOADS, Uploads, link_name


def test_names_lose_their_directories() -> None:
    assert link_name("scans/report 2024.pdf") == "report 2024.pdf"
    assert link_name("C:\\\\x\\\\y.txt") == "y.txt"
    for unsafe in ("", ".", "..", "-", "a/..", "x" + chr(0) + "y"):
        assert link_name(unsafe) == "input"


def test_uploads_are_kept_per_token_and_limited(tmp_path: Path) -> None:
    uploads = Uploads(tmp_path)
    digest, size = uploads.store("ci", io.BytesIO(b"abc"), 3, 10)
    assert size == 3 and uploads.find("ci", digest) is not None
    assert uploads.find("other", digest) is None
    with pytest.raises(GraphCliError) as info:
        uploads.store("ci", io.BytesIO(b"x" * 11), 11, 10)
    assert info.value.code == "file.too_large"
    for n in range(PENDING_UPLOADS - 1):
        uploads.store("ci", io.BytesIO(bytes([n]) * 10), 10, 10)
    with pytest.raises(GraphCliError) as info:
        uploads.store("ci", io.BytesIO(b"y" * 10), 10, 10)
    assert "pending uploads" in info.value.message
    uploads.store("other", io.BytesIO(b"y" * 10), 10, 10)  # another token is not affected
