"""Write (or remove) the build id of every distribution before a build.

    python3 scripts/build_id.py           write packages/*/src/<module>/_build_id
    python3 scripts/build_id.py --clean   remove them again

A build id is ``sha256:<hex>`` over the ``pyproject.toml`` and the ``src/``
files of a distribution and of every workspace distribution it depends on,
so two builds of the same tree get the same id whatever the version says.
Caches, compiled files, dot files and the build id itself are not part of
it. The Dockerfile and the install of a client run it before building and
remove the files after (the checkout's editable install must not claim a
build). Stdlib only: it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from pathlib import Path

BUILD_FILE = "_build_id"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+")  # the name at the start of a requirement
PACKAGES = Path(__file__).resolve().parent.parent / "packages"


def distributions(packages: Path) -> dict[str, Path]:
    """Distribution name to its package directory."""
    found = {}
    for project in sorted(packages.glob("*/pyproject.toml")):
        name = tomllib.loads(project.read_text())["project"]["name"]
        found[name] = project.parent
    return found


def closure(name: str, found: dict[str, Path]) -> list[str]:
    """``name`` and the workspace distributions it depends on, sorted."""
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        project = tomllib.loads((found[current] / "pyproject.toml").read_text())["project"]
        for requirement in project.get("dependencies", []):
            match = NAME_RE.match(requirement.strip())
            if match is not None and match.group(0) in found:
                todo.append(match.group(0))
    return sorted(seen)


def _hashed(directory: Path) -> list[Path]:
    files = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if not path.is_file() or path.name == BUILD_FILE or path.suffix == ".pyc":
            continue
        if any(part == "__pycache__" or part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return files


def build_id(name: str, found: dict[str, Path]) -> str:
    digest = hashlib.sha256()
    for member in closure(name, found):
        root = found[member]
        for path in [root / "pyproject.toml", *_hashed(root / "src")]:
            relative = path.relative_to(root).as_posix()
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{member}/{relative}\0{content}\n".encode())
    return f"sha256:{digest.hexdigest()}"


def targets(found: dict[str, Path]) -> dict[str, Path]:
    """Distribution name to the build file in its module directory."""
    return {name: root / "src" / name / BUILD_FILE for name, root in found.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clean", action="store_true", help="remove the build files")
    parser.add_argument("--packages", type=Path, default=PACKAGES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    found = distributions(args.packages)
    for name, target in targets(found).items():
        if args.clean:
            target.unlink(missing_ok=True)
        else:
            target.write_text(build_id(name, found) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
