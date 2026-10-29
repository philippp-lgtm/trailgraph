"""The template of a generated graph skill: block markers in a curated ``SKILL.md``.

A published graph skill directory is a template when its ``SKILL.md`` has at
least one block marker, a line consisting only of
``<!-- graph_cli:block <name> -->``; without one it is a static skill,
installed as is. Each marker is replaced by the generated block ``<name>``
(``graph_cli.skills.graph_skill``); everything else (frontmatter, prose,
extra files) is curated and kept. A marker inside a line is plain text.

Refused (rule ``template``): an unknown block name (a newer publisher, or a
typo), a block used twice, a line that starts like a marker but is not one,
markers outside ``SKILL.md`` and a curated ``references/model.md``,
``references/functions.md`` or file below ``references/modules/``, which a generated skill
gets from the model and the function catalog.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.skill_marker import GENERATED, STATIC
from graph_cli.skills.tree import SKILL_FILE, Problem, SkillFiles

BLOCKS: Final = (
    "start",
    "modules",
    "types",
    "relations",
    "constraints",
    "names",
    "model",
    "functions",
)
MODEL_FILE: Final = "references/model.md"
FUNCTIONS_FILE: Final = "references/functions.md"  # generated for a single-module graph
MODULES_DIR: Final = "references/modules/"  # one generated page per module of a modular graph
MARKER_RE: Final = re.compile(r"^<!-- graph_cli:block ([a-z_]+) -->$")
MARKER_START: Final = "<!-- graph_cli:"


@dataclass(frozen=True, slots=True)
class Template:
    """A parsed template: the decoded files and ``(line index, block)`` of each marker."""

    files: Mapping[str, str]
    blocks: tuple[tuple[int, str], ...]

    @property
    def lines(self) -> list[str]:
        """The lines of ``SKILL.md`` without their line ends."""
        return self.files[SKILL_FILE].split("\n")

    def curated(self) -> dict[str, str]:
        """The maintainer's text: every file, with the marker lines of ``SKILL.md`` blanked
        (line numbers stay as in the template)."""
        lines = self.lines
        for index, _ in self.blocks:
            lines[index] = ""
        return {**self.files, SKILL_FILE: "\n".join(lines)}


def kind_of(files: SkillFiles) -> str:
    """``generated`` when ``SKILL.md`` has a block marker line, else ``static``."""
    text = files.get(SKILL_FILE, b"").decode("utf-8", errors="replace")
    return GENERATED if any(_is_marker(line) for line in text.split("\n")) else STATIC


def parse_template(files: SkillFiles) -> tuple[Template | None, list[Problem]]:
    """The template, or ``None`` with every broken template rule."""
    texts: dict[str, str] = {}
    problems: list[Problem] = []
    for path in sorted(files):
        try:
            texts[path] = files[path].decode("utf-8")
        except UnicodeDecodeError:
            continue  # a tree problem (``file``), reported by ``tree.validate``
    if SKILL_FILE not in texts:
        return None, [Problem("template", SKILL_FILE, "missing or not UTF-8")]
    for path in sorted(files):
        if path in (MODEL_FILE, FUNCTIONS_FILE) or path.startswith(MODULES_DIR):
            problems.append(Problem("template", path, "generated from the model; remove this file"))
    for path, text in sorted(texts.items()):
        if path == SKILL_FILE:
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            if _is_marker(line):
                problems.append(
                    Problem("template", path, f"line {number}: block markers work in SKILL.md only")
                )
    blocks: list[tuple[int, str]] = []
    for index, line in enumerate(texts[SKILL_FILE].split("\n")):
        if not _is_marker(line):
            continue
        where = f"line {index + 1}"
        match = MARKER_RE.match(line.strip())
        if match is None:
            problems.append(
                Problem(
                    "template",
                    SKILL_FILE,
                    f"{where}: not a block marker; write <!-- graph_cli:block <name> -->",
                )
            )
        elif match.group(1) not in BLOCKS:
            problems.append(
                Problem(
                    "template",
                    SKILL_FILE,
                    f"{where}: unknown block {match.group(1)!r} (known: {', '.join(BLOCKS)})",
                )
            )
        elif match.group(1) in {name for _, name in blocks}:
            problems.append(
                Problem("template", SKILL_FILE, f"{where}: block {match.group(1)!r} used twice")
            )
        else:
            blocks.append((index, match.group(1)))
    if problems:
        return None, problems
    return Template(texts, tuple(blocks)), []


def _is_marker(line: str) -> bool:
    """A line that is meant as a marker: it starts with ``<!-- graph_cli:``."""
    return line.strip().startswith(MARKER_START)
