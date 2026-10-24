"""The build id of an installed distribution (``Graph-Cli-Build``).

``scripts/build_id.py`` writes ``_build_id`` into a package before it is
built: ``sha256`` over the sources of the distribution and of the workspace
distributions it depends on. A client sends ``<program> <build id>`` with
every request; graph-server refuses another build of that program
(``remote.client_outdated``) or, with ``[server] client_builds = "warn"``,
answers its own and the client warns. A package without the file (a
checkout's editable install) has no build id and claims none.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

BUILD_HEADER: Final = "Graph-Cli-Build"
BUILD_FILE: Final = "_build_id"
BUILD_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
PROGRAM_RE: Final = re.compile(r"^[a-z][a-z-]{0,31}$")


def read_build(package_init: str) -> str | None:
    """The build id next to a package's ``__init__.py``; ``None`` without a valid one."""
    try:
        text = (Path(package_init).parent / BUILD_FILE).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if BUILD_RE.match(text) else None


def build_header(program: str, build: str) -> str:
    return f"{program} {build}"


def parse_build_header(value: str | None) -> tuple[str, str] | None:
    """``(program, build id)`` of a ``Graph-Cli-Build`` value; ``None`` if malformed."""
    program, _, build = (value or "").strip().partition(" ")
    if PROGRAM_RE.match(program) and BUILD_RE.match(build.strip()):
        return program, build.strip()
    return None


def short(build: str) -> str:
    """The first 12 hex digits, for messages."""
    return build.removeprefix("sha256:")[:12]
