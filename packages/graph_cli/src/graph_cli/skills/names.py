"""Checks of the curated text of a graph skill template against the model and the program (Q3).

The curated text (the template outside its block markers, and the extra
files) is written by hand, so it can name what the model or this program
does not have. Checked, each with rule:

- ``term``: every CURIE with a prefix of the model (``inv:Item``) names a
  class, relation, enum, lifecycle or datatype of the model.
- ``command``: every ``graph <command>`` in code (inline code or a code
  block) names a command of the inventory, and every ``--flag`` after it is
  an option of that command or a global option.
- ``alias``: an alias-shaped token with a prefix of the graph has the live
  format (``ITEM-0001``, not ``ITEM-1``, F12); in code, an alias with four
  or more digits and a prefix the graph does not have is reported too
  (text like ``UTF-8`` stays plain). Whether the entity exists is not checked:
  data changes, the model part does not name data (Q4).

Generated blocks are not checked; they come from the model.
"""

from __future__ import annotations

import difflib
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.ids import ALIAS_DIGITS, format_alias
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_obj, req_str
from graph_cli.skills.template import Template
from graph_cli.skills.tree import Problem

PROGRAM: Final = "graph"
CURIE_RE: Final = re.compile(r"(?<![\w:/.@-])([a-z][a-z0-9_]{0,15}):([A-Za-z][A-Za-z0-9_]*)")
ALIAS_TOKEN_RE: Final = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{0,7})-([0-9]+)(?![\w-])")
COMMAND_RE: Final = re.compile(rf"(?<![\w/.<$-]){PROGRAM}[ \t]+([^\n`]*)")
FENCE_RE: Final = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE: Final = re.compile(r"(`+)((?:(?!\n[ \t]*\n).)+?)\1", re.DOTALL)
COMMAND_WORD_RE: Final = re.compile(r"^[a-z][a-z-]*$")
FLAG_RE: Final = re.compile(r"^--[a-z][a-z0-9-]*")
SHELL_STOPS: Final = frozenset({"|", "||", "&&", ";", ">", ">>", "2>&1", "&"})
ALWAYS_FLAGS: Final = frozenset({"--help"})


@dataclass(frozen=True, slots=True)
class _Code:
    """A piece of code in a curated file: its text and the line it starts on."""

    text: str
    line: int


def check_names(
    template: Template,
    model: Mapping[str, JsonValue],
    graph: Mapping[str, JsonValue],
    inventory: Mapping[str, JsonValue],
) -> list[Problem]:
    """Every ``term``, ``command`` and ``alias`` problem of the curated text."""
    terms = _terms(model)
    prefixes = {curie.partition(":")[0] for curie in terms}
    aliases = sorted(
        {str(p) for p in req_obj(as_object(dict(graph), "graph"), "alias_prefixes").values()}
    )
    commands = _commands(inventory)
    problems: list[Problem] = []
    for path, text in sorted(template.curated().items()):
        problems += _term_problems(path, text, terms, prefixes)
        code = list(_code(text))
        problems += _alias_problems(path, text, code, aliases)
        for piece in code:
            problems += _command_problems(path, piece, commands)
    return problems


# ---- terms -------------------------------------------------------------------------------


def _terms(model: Mapping[str, JsonValue]) -> frozenset[str]:
    """Classes, relations and the enums, datatypes and lifecycles the types use."""
    bundle = as_object(dict(model), "model")
    found = {
        req_str(as_object(t, "types[]"), "curie")
        for t in req_list(req_obj(bundle, "types"), "types")
    }
    found |= {
        req_str(as_object(r, "relations[]"), "curie")
        for r in req_list(req_obj(bundle, "relations"), "relations")
    }
    for document in req_obj(bundle, "type").values():
        body = req_obj(as_object(document, "type"), "type")
        for field in _objects(body, "fields"):
            if field.get("kind") in ("enum", "datatype", "lifecycle"):
                found.add(req_str(field, "type"))
        found |= {req_str(lc, "lifecycle") for lc in _objects(body, "lifecycles")}
    return frozenset(found)


def _term_problems(
    path: str, text: str, terms: frozenset[str], prefixes: set[str]
) -> list[Problem]:
    problems = []
    for match in CURIE_RE.finditer(text):
        curie = match.group(0)
        if match.group(1) not in prefixes or curie in terms:
            continue
        close = difflib.get_close_matches(curie, sorted(terms), n=1)
        hint = f"; did you mean {close[0]}?" if close else ""
        problems.append(
            Problem(
                "term",
                path,
                f"line {_line(text, match.start())}: {curie} is not in the model{hint}",
            )
        )
    return problems


# ---- aliases ------------------------------------------------------------------------------


def _alias_problems(
    path: str, text: str, code: Sequence[_Code], prefixes: Sequence[str]
) -> list[Problem]:
    problems = []
    for match in ALIAS_TOKEN_RE.finditer(text):
        prefix, digits = match.groups()
        if prefix not in prefixes:
            continue
        expected = format_alias(prefix, max(int(digits), 1))
        if match.group(0) != expected:
            problems.append(
                Problem(
                    "alias",
                    path,
                    f"line {_line(text, match.start())}: {match.group(0)} is not an alias;"
                    f" aliases look like {expected}",
                )
            )
    known = ", ".join(prefixes) or "none"
    for piece in code:
        for match in ALIAS_TOKEN_RE.finditer(piece.text):
            prefix, digits = match.groups()
            if prefix in prefixes or len(digits) < ALIAS_DIGITS:
                continue
            problems.append(
                Problem(
                    "alias",
                    path,
                    f"line {piece.line + piece.text.count(chr(10), 0, match.start())}:"
                    f" {match.group(0)} has a prefix this graph does not use (prefixes: {known})",
                )
            )
    return problems


# ---- commands -------------------------------------------------------------------------------


def _commands(inventory: Mapping[str, JsonValue]) -> dict[tuple[str, ...], frozenset[str]]:
    """Every command path with the flags it takes (its options and the global ones)."""
    doc = as_object(dict(inventory), "inventory")
    common = {
        req_str(as_object(o, "global_options[]"), "name") for o in req_list(doc, "global_options")
    }
    found = {}
    for value in req_list(doc, "commands"):
        command = as_object(value, "commands[]")
        path = tuple(str(p) for p in as_list(command["path"], "path"))
        options = {req_str(o, "name") for o in _objects(command, "options")}
        found[path] = frozenset({*options, *common, *ALWAYS_FLAGS})
    return found


def _command_problems(
    path: str, piece: _Code, commands: Mapping[tuple[str, ...], frozenset[str]]
) -> list[Problem]:
    problems = []
    for match in COMMAND_RE.finditer(piece.text):
        line = piece.line + piece.text.count("\n", 0, match.start())
        words = _words(match.group(1))
        if not words or not COMMAND_WORD_RE.match(words[0]):
            continue  # "graph" as a word, or global options only
        command = max((p for p in commands if tuple(words[: len(p)]) == p), key=len, default=None)
        if command is None:
            known = max(
                (
                    n
                    for n in range(len(words) + 1)
                    if any(p[:n] == tuple(words[:n]) for p in commands)
                ),
            )
            shown = " ".join([PROGRAM, *words[: known + 1]])
            problems.append(
                Problem("command", path, f"line {line}: {shown!r} is not a command of {PROGRAM}")
            )
            continue
        for word in words[len(command) :]:
            found = FLAG_RE.match(word)  # quotes or commas may follow inside JSON or prose
            flag = found.group(0) if found is not None else None
            if flag is not None and flag not in commands[command]:
                problems.append(
                    Problem(
                        "command",
                        path,
                        f"line {line}: {' '.join([PROGRAM, *command])} has no option {flag}",
                    )
                )
    return problems


def _words(text: str) -> list[str]:
    """The words of one command line, up to the first shell operator."""
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        words = text.split()
    found = []
    for word in words:
        if word in SHELL_STOPS or word.startswith("<<"):
            break
        found.append(word)
    return found


# ---- reading Markdown -------------------------------------------------------------------------


def _code(text: str) -> Iterator[_Code]:
    """Code blocks (one logical line each, ``\\`` continuations joined) and inline code."""
    lines = text.split("\n")
    prose: list[str] = []
    fence: str | None = None
    pending: list[str] = []
    start = 0
    for number, line in enumerate(lines, start=1):
        opener = FENCE_RE.match(line)
        if fence is None:
            if opener is not None:
                fence = opener.group(1)
                prose.append("")
            else:
                prose.append(line)
            continue
        prose.append("")
        if opener is not None and opener.group(1) == fence:
            fence = None
            continue
        if not pending:
            start = number
        if line.rstrip().endswith("\\"):
            pending.append(line.rstrip()[:-1])
            continue
        yield _Code(" ".join([*pending, line]), start)
        pending = []
    if pending:
        yield _Code(" ".join(pending), start)
    rest = "\n".join(prose)
    for match in INLINE_CODE_RE.finditer(rest):
        yield _Code(match.group(2), _line(rest, match.start(2)))


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _objects(doc: JsonObject, key: str) -> list[JsonObject]:
    return [as_object(item, f"{key}[]") for item in req_list(doc, key)]
