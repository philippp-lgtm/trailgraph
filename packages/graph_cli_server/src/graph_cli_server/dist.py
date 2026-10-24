"""The programs graph-server hands out under ``/v1/dist/`` (contracts B14.18).

The image keeps the wheels it was built from in one directory
(``GRAPH_CLI_DIST_DIR``) with ``constraints.txt``, the pinned third-party
requirements. The manifest is read from those files once, at start: for
each program the one wheel of its distribution, its version (from the file
name), its build id (``<module>/_build_id`` inside the wheel), hash and
size. A wheel of another build than the server runs is left out and named
in ``problems``: a client would install a program the server then refuses.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.catalog.build import BUILD_FILE, BUILD_RE, short
from graph_cli.catalog.dist import CONSTRAINTS, PROGRAMS, DistFile, Manifest, Wheel, file_hash

DIST_ENV: Final = "GRAPH_CLI_DIST_DIR"
WHEEL_RE: Final = re.compile(r"^(?P<dist>[A-Za-z0-9_]+)-(?P<version>[^-]+)-[^/]+\.whl$")


@dataclass(frozen=True)
class Served:
    """The manifest and where each of its files lies."""

    manifest: Manifest
    files: Mapping[str, tuple[Path, bool]]  # file name -> (path, needs an admin token)


def load(directory: Path, builds: Mapping[str, str | None]) -> tuple[Served | None, list[str]]:
    """What ``directory`` hands out, compared with the builds the server runs (``None``:
    unknown, not compared); ``None`` without a wheel. Also the problems found."""
    problems: list[str] = []
    try:
        names = sorted(p.name for p in directory.iterdir() if p.is_file())
    except OSError as exc:
        return None, [f"{directory}: {exc.strerror or exc}"]
    wheels: dict[str, Wheel] = {}
    files: dict[str, tuple[Path, bool]] = {}
    for program in PROGRAMS.values():
        found = [
            (n, m) for n in names if (m := WHEEL_RE.match(n)) and m["dist"] == program.distribution
        ]
        if len(found) != 1:
            if found:
                problems.append(f"{program.name}: {len(found)} wheels of {program.distribution}")
            continue
        name, match = found[0]
        path = directory / name
        build = _build_of(path, program.distribution)
        ours = builds.get(program.name)
        if build is None or (ours is not None and ours != build):
            shown = "no build id" if build is None else f"build {short(build)}"
            problems.append(f"{program.name}: {name} has {shown}, the server runs another")
            continue
        wheels[program.name] = Wheel(
            program.name,
            program.distribution,
            match["version"],
            build,
            program.requires,
            "admin" if program.admin else "public",
            DistFile(name, file_hash(path), path.stat().st_size),
        )
        files[name] = (path, program.admin)
    for name, wheel in list(wheels.items()):
        missing = [r for r in wheel.requires if r not in wheels]
        if missing:
            problems.append(f"{name}: needs {', '.join(missing)}, which is not handed out")
            del wheels[name]
            del files[wheel.dist.file]
    if not wheels:
        return None, problems
    constraints = None
    path = directory / CONSTRAINTS
    if path.is_file():
        constraints = DistFile(CONSTRAINTS, file_hash(path), path.stat().st_size)
        files[CONSTRAINTS] = (path, False)
    return Served(Manifest(wheels, constraints), files), problems


def _build_of(wheel: Path, distribution: str) -> str | None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            text = archive.read(f"{distribution}/{BUILD_FILE}").decode().strip()
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    return text if BUILD_RE.match(text) else None
