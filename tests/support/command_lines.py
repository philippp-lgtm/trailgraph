"""The command lines of a Markdown text against the inventories of `graph` and `graph-admin`.

A command line is each line of a shell (or unnamed) code block and each code span of the
prose (a span may wrap). One that starts with a program (after an optional `uv run `) must
name a real command and only options that command, or every command, takes. The inventory
goldens are kept equal to `describe commands` by `tests/cli/test_app_meta.py` and
`tests/cli/test_admin_init.py`.
"""

import json
import re
from typing import Any

from support.fixtures import GOLDENS

CODE_SPAN = re.compile(r"`([^`\n]+)`")
WORD = re.compile(r"[a-z][a-z_-]*")
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z-]*")


def _inventory(golden: str) -> dict[tuple[str, ...], set[str]]:
    found: dict[str, Any] = json.loads((GOLDENS / golden / "inventory.json").read_text())
    common = {"--json", "--help", *(o["name"] for o in found["global_options"])}
    return {tuple(c["path"]): {o["name"] for o in c["options"]} | common for c in found["commands"]}


PROGRAMS = {"graph-admin": _inventory("admin"), "graph": _inventory("app")}


def command_lines(text: str) -> list[str]:
    """Each line of a shell (or unnamed) code block, and each code span of the prose."""
    found: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = line.strip()[3:] if fence is None else None
        elif fence is None:
            prose.append(line)
        elif fence in ("", "sh"):
            found.append(line)
    return [*found, *CODE_SPAN.findall(" ".join(prose))]


def command_line_problems(text: str) -> list[str]:
    """Each command line of a program that names no command, or an option its command
    does not take; ``<program> <placeholder>`` names none, a group alone (``graph
    describe``) is fine."""
    problems = []
    for line in command_lines(text):
        words = line.split()
        if words[:2] == ["uv", "run"]:
            words = words[2:]
        if len(words) < 2 or words[0] not in PROGRAMS or not WORD.fullmatch(words[1]):
            continue
        commands = PROGRAMS[words[0]]
        path: tuple[str, ...] = ()
        for word in words[1:]:
            longer = (*path, word)
            if not WORD.fullmatch(word) or not any(c[: len(longer)] == longer for c in commands):
                break
            path = longer
        if not path or (path not in commands and words[len(path) + 1 :]):
            problems.append(f"{' '.join(words)}: no such command")
            continue
        if path not in commands:
            continue  # a group alone
        unknown = sorted(set(OPTION.findall(" ".join(words))) - commands[path])
        if unknown:
            problems.append(f"{' '.join(words)}: {', '.join(unknown)}")
    return problems
