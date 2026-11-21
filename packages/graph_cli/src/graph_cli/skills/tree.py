"""A skill as files: reading, writing and validating skill directories (D15 Q6).

A skill is ``{relative POSIX path: bytes}`` with ``SKILL.md`` at the top.
``validate`` judges a tree before it is published or installed: the name
(Agent Skills rule, equal to the directory and the frontmatter ``name``),
regular UTF-8 files only (no symlinks, no dotfiles, no marker names), a
size budget, and a deny-list of maintenance vocabulary and local paths,
which an agent-facing skill must never carry. Plain file I/O; no services.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.skill_marker import MARKER_FILE

SkillFiles = Mapping[str, bytes]

SKILL_FILE: Final = "SKILL.md"
LEGACY_MARKER_FILE: Final = ".graph_cli_install"  # written by the former install script
MARKER_FILES: Final = frozenset({MARKER_FILE, LEGACY_MARKER_FILE})
NAME_RE: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME: Final = 64  # Agent Skills: name at most 64 characters
MAX_DESCRIPTION: Final = 1024  # Agent Skills: description at most 1024 characters
MAX_SKILL_LINES: Final = 500
MAX_TOTAL_BYTES: Final = 256 * 1024

# What an agent-facing skill must not carry: the maintenance surface and local paths.
LEAKS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("maintenance program", re.compile(r"graph-admin")),
    ("maintenance action", re.compile(r"(?<![\w.-])maint\.[a-z_]")),
    ("maintenance code", re.compile(r"(?<![\w.-])admin\.[a-z_]")),
    ("data root option", re.compile(r"--data-root")),
    ("data root variable", re.compile(r"GRAPH_CLI_ROOT")),
    ("absolute path", re.compile(r"(?<![\w.])/(?:Users|home|root)/")),
    ("home path", re.compile(r"~/|\$HOME\b")),
    ("windows path", re.compile(r"\b[A-Za-z]:\\")),
)


@dataclass(frozen=True, slots=True)
class Problem:
    """One broken rule; ``rule`` is one of name, file, frontmatter, size, leak, and for
    graph skill templates template, term, command, alias, function, action."""

    rule: str
    path: str
    message: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"rule": self.rule, "path": self.path, "message": self.message}


def read_tree(directory: Path) -> tuple[dict[str, bytes], list[Problem]]:
    """Every regular file below ``directory``; anything else is a problem, never followed."""
    files: dict[str, bytes] = {}
    problems: list[Problem] = []
    for current, dirs, names in os.walk(directory, followlinks=False):
        base = Path(current)
        for name in sorted([*dirs, *names]):
            path = base / name
            relative = path.relative_to(directory).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                problems.append(Problem("file", relative, "not a regular file (symlink?)"))
                continue
            files[relative] = path.read_bytes()
        dirs.sort()
    return files, problems


def write_tree(directory: Path, files: SkillFiles) -> None:
    """Write ``files`` below a fresh ``directory`` (the caller publishes it with a rename)."""
    directory.mkdir()
    for name in sorted(files):
        if not _is_relative(name):
            raise ValueError(f"not a relative file name: {name!r}")
        path = directory.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(files[name])


def validate(files: SkillFiles, *, name: str, directory: str | None = None) -> list[Problem]:
    """Every broken rule of a skill tree named ``name``; ``[]`` when it may be installed."""
    problems: list[Problem] = []
    if len(name) > MAX_NAME or not NAME_RE.match(name):
        problems.append(
            Problem("name", "", f"skill name {name!r} must match {NAME_RE.pattern} (max 64)")
        )
    if directory is not None and directory != name:
        problems.append(Problem("name", "", f"the directory is {directory!r}, expected {name!r}"))
    texts: dict[str, str] = {}
    for path in sorted(files):
        parts = PurePosixPath(path).parts
        if not _is_relative(path):
            problems.append(Problem("file", path, "not a relative file name"))
        elif any(part.startswith(".") for part in parts):
            problems.append(Problem("file", path, "hidden files and marker names are not allowed"))
        try:
            texts[path] = files[path].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(Problem("file", path, "not UTF-8 text"))
    if SKILL_FILE not in files:
        problems.append(Problem("file", SKILL_FILE, "missing"))
    elif SKILL_FILE in texts:
        problems += _frontmatter(texts[SKILL_FILE], name)
        lines = texts[SKILL_FILE].count("\n")
        if lines > MAX_SKILL_LINES:
            problems.append(
                Problem("size", SKILL_FILE, f"{lines} lines, at most {MAX_SKILL_LINES}")
            )
    total = sum(len(data) for data in files.values())
    if total > MAX_TOTAL_BYTES:
        problems.append(Problem("size", "", f"{total} bytes in total, at most {MAX_TOTAL_BYTES}"))
    for path, text in sorted(texts.items()):
        for kind, pattern in LEAKS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(Problem("leak", path, f"line {line}: {kind} {match.group(0)!r}"))
    return problems


def _frontmatter(text: str, name: str) -> list[Problem]:
    if not text.startswith("---\n"):
        return [Problem("frontmatter", SKILL_FILE, "must start with a --- frontmatter block")]
    end = text.find("\n---\n", 3)
    if end < 0:
        return [Problem("frontmatter", SKILL_FILE, "the frontmatter block is not closed")]
    keys = frontmatter_keys(text[4 : end + 1])
    problems = []
    if keys.get("name") != name:
        problems.append(
            Problem("frontmatter", SKILL_FILE, f"name is {keys.get('name')!r}, expected {name!r}")
        )
    description = keys.get("description") or ""
    if not description.strip():
        problems.append(Problem("frontmatter", SKILL_FILE, "description is missing"))
    elif len(description) > MAX_DESCRIPTION:
        problems.append(
            Problem("frontmatter", SKILL_FILE, f"description is longer than {MAX_DESCRIPTION}")
        )
    return problems


def frontmatter_keys(block: str) -> dict[str, str]:
    """Top-level ``key: value`` pairs of a YAML frontmatter, enough to read name and
    description: plain, single- or double-quoted scalars and block scalars."""
    found: dict[str, str] = {}
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if not line or line[0] in " \t#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in ("|", ">"):
            body = []
            for following in lines[index + 1 :]:
                if following and following[0] not in " \t":
                    break
                body.append(following.strip())
            value = " ".join(part for part in body if part)
        elif value.startswith('"'):
            try:
                value = str(json.loads(value))
            except ValueError:
                value = value.strip('"')
        elif value.startswith("'"):
            value = value[1:-1].replace("''", "'")
        found[key.strip()] = value
    return found


def _is_relative(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (
        bool(parts)
        and not name.startswith("/")
        and "\\" not in name
        and all(part not in ("", ".", "..") for part in parts)
    )
