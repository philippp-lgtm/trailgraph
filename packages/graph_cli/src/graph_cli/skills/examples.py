"""The ``graph`` command lines a curated skill text teaches, and two judgements on them.

``command_examples`` reads the curated text of a template (``Template.curated``) and yields
every ``graph <command> ...`` of fenced code and inline code as ``CommandExample``. A logical
line joins ``\\`` continuations and, for a command line, the lines of a quoted word that is
still open (a Cypher text over several lines). Commands that read their input elsewhere
(``--file``, ``-`` for stdin, a heredoc) carry nothing to judge and are left out.

``query_recipes`` turns the ``graph query`` examples into ``QueryRecipe``: the Cypher text
and the ``--param`` texts, walked with the options of the inventory (an option that is not a
flag takes the next word, ``--name=value`` is one word).

``generic_examples`` finds the fenced examples that name nothing of the graph: no type,
relation, field, enum value, lifecycle state, module, function or action. Such an example
teaches the command, and the ``graph-cli`` skill and the generated ``start`` block already do.
The terms come from the model documents the renderer gets (``model_terms``): CURIEs and their
local names, field names but the system fields, enum values, lifecycle state names, module
ids, function and action names. Data values, aliases and numbers are not terms, and neither
are ``<placeholder>`` words.

Pure: no model, no storage; kernel imports only.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_obj, req_str
from graph_cli.skills.names import (
    COMMAND_WORD_RE,
    FENCE_RE,
    INLINE_CODE_RE,
    PROGRAM,
    command_words,
)
from graph_cli.skills.template import Template

# Mirror of ``ontology_layer.vocabulary.SYSTEM_FIELDS`` (the kernel-only layer cannot import it;
# ``tests/unit/skills/test_examples.py`` keeps the two equal).
SYSTEM_FIELDS: Final = frozenset(
    {
        "id",
        "type",
        "rev",
        "record_status",
        "alias",
        "created_at",
        "recorded_at",
        "tx_id",
        "tx_seq",
        "valid_from",
        "valid_to",
        "content_hash",
        "ontology_version",
    }
)
GRAPH_RE: Final = re.compile(rf"(?<![\w/.<$-]){PROGRAM}[ \t]+(?=\S)")
HEREDOC_RE: Final = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
PLACEHOLDER_RE: Final = re.compile(r"<[A-Za-z][^<>]*>")
WORD_RE: Final = re.compile(r"[a-z0-9]+")
TOKEN_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*)?")
MAX_JOINED_LINES: Final = 60  # a quote that stays open longer is prose, not a command
# String options whose value names fields, relations or sort keys of the graph.
NAMING_OPTIONS: Final = frozenset(
    {"--for", "--fields", "--field", "--attr", "--sort", "--rel", "--in-rel", "--unset"}
)
# Options whose value is data written for the graph: its words are no terms, but a placeholder
# of the maintainer's (``--title '<kind> <date>'``) still says something about the graph.
DATA_OPTIONS: Final = frozenset({"--title", "--label", "--name"})
NAMING_TYPES: Final = frozenset({"term", "filter", "entity_ref"})
NAME_PART_TYPES: Final = frozenset({"assignment", "json_assignment"})


@dataclass(frozen=True, slots=True)
class CommandExample:
    """One ``graph`` command line: the file, its line, the words after ``graph`` and whether it
    stands in a fenced block (else in inline code)."""

    path: str
    line: int
    words: tuple[str, ...]
    fenced: bool


@dataclass(frozen=True, slots=True)
class QueryRecipe:
    """A ``graph query`` example: the Cypher text and the ``--param`` texts as written."""

    path: str
    line: int
    text: str
    params: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    line: int
    fenced: bool


def command_examples(
    template: Template, inventory: Mapping[str, JsonValue]
) -> list[CommandExample]:
    """Every ``graph <command> ...`` of the curated text, in file and line order. Global
    options before the command (``graph --graph g find``) are skipped."""
    flags = _global_flags(inventory)
    found: list[CommandExample] = []
    for path, text in sorted(template.curated().items()):
        for piece in _pieces(text):
            found += _examples(path, piece, flags)
    return found


def _examples(path: str, piece: _Piece, flags: Mapping[str, bool]) -> Iterator[CommandExample]:
    for match in GRAPH_RE.finditer(piece.text):
        if _open_quote(piece.text[: match.start()]):
            continue  # the word "graph" inside a quoted value, not a command
        words = _without_global_options(command_words(piece.text[match.end() :]), flags)
        if not words or not COMMAND_WORD_RE.match(words[0]):
            continue
        if any(w == "--file" or w.startswith("--file=") or w == "-" for w in words):
            continue  # the input is elsewhere
        line = piece.line + piece.text.count("\n", 0, match.start())
        yield CommandExample(path, line, tuple(words), piece.fenced)


def _without_global_options(words: list[str], flags: Mapping[str, bool]) -> list[str]:
    index = 0
    while index < len(words) and words[index].startswith("--"):
        name, equals, _ = words[index].partition("=")
        if name not in flags:
            return words  # not a global option: leave the line as it is
        index += 1 if flags[name] or equals else 2
    return words[index:]


def _global_flags(inventory: Mapping[str, JsonValue]) -> dict[str, bool]:
    doc = as_object(dict(inventory), "inventory")
    return {
        req_str(as_object(o, "global_options[]"), "name"): as_object(o, "o").get("type") == "flag"
        for o in req_list(doc, "global_options")
    }


def _pieces(text: str) -> Iterator[_Piece]:
    """Logical lines of fenced code, then inline code of the prose."""
    prose: list[str] = []
    fence: str | None = None
    heredoc: str | None = None
    current = ""
    joined = 0
    start = 0
    sep = ""
    for number, line in enumerate(text.split("\n"), start=1):
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
            if current.strip():
                yield _Piece(_scan(current)[0], start, True)
            fence, heredoc, current, joined = None, None, "", 0
            continue
        if heredoc is not None:
            heredoc = None if line.strip() == heredoc else heredoc
            continue
        if not current:
            start, joined, sep = number, 0, ""
        joined += 1
        stem = line.rstrip()
        if stem.endswith("\\"):  # a continuation: the next line follows after a space
            current = f"{current}{sep}{stem[:-1]}"
            sep = " "
            continue
        current = f"{current}{sep}{line}"
        cleaned, is_open = _scan(current)
        if joined < MAX_JOINED_LINES and GRAPH_RE.search(cleaned) and is_open:
            sep = "\n"  # a quoted word goes on in the next line
            continue
        tag = HEREDOC_RE.search(cleaned)
        if cleaned.strip():
            yield _Piece(cleaned, start, True)
        current = ""
        heredoc = tag.group(2) if tag is not None else None
    if current.strip():
        yield _Piece(_scan(current)[0], start, True)
    rest = "\n".join(prose)
    for found in INLINE_CODE_RE.finditer(rest):
        yield _Piece(found.group(2), rest.count("\n", 0, found.start(2)) + 1, False)


def _open_quote(text: str) -> bool:
    """Whether ``text`` ends inside a quoted shell word."""
    try:
        shlex.split(text, posix=True)
    except ValueError:
        return True
    return False


def _scan(text: str) -> tuple[str, bool]:
    """``text`` without its shell comments (``#`` at the start of a word, outside quotes) and
    whether it ends inside a quoted word."""
    out: list[str] = []
    quote: str | None = None
    word_start = True
    index = 0
    while index < len(text):
        char = text[index]
        if quote is None:
            if char == "\\":
                out.append(text[index : index + 2])
                index += 2
                word_start = False
            elif char in "'\"":
                quote = char
                out.append(char)
                index += 1
                word_start = False
            elif char == "#" and word_start:
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
            else:
                out.append(char)
                word_start = char in " \t\n;|&("
                index += 1
        else:
            if char == "\\" and quote == '"':
                out.append(text[index : index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            out.append(char)
            index += 1
    return "".join(out), quote is not None


# ---- options ------------------------------------------------------------------------------


def _options(inventory: Mapping[str, JsonValue]) -> dict[tuple[str, ...], dict[str, str]]:
    """Per command path, its options (and the global ones) with their types."""
    doc = as_object(dict(inventory), "inventory")
    common = {
        req_str(as_object(o, "global_options[]"), "name"): str(as_object(o, "o").get("type"))
        for o in req_list(doc, "global_options")
    }
    found: dict[tuple[str, ...], dict[str, str]] = {}
    for value in req_list(doc, "commands"):
        command = as_object(value, "commands[]")
        path = tuple(str(p) for p in as_list(command["path"], "path"))
        options = dict(common)
        for option in as_list(command.get("options", []), "options"):
            body = as_object(option, "options[]")
            options[req_str(body, "name")] = str(body.get("type"))
        found[path] = options
    return found


def _command_path(
    words: tuple[str, ...], options: Mapping[tuple[str, ...], dict[str, str]]
) -> tuple[str, ...] | None:
    return max((p for p in options if words[: len(p)] == p), key=len, default=None)


def _arguments(
    words: tuple[str, ...], kinds: Mapping[str, str]
) -> Iterator[tuple[str | None, str]]:
    """``(option, value)`` for every argument word after the command path: ``(None, word)`` for
    a positional word, ``(--name, value)`` for an option that takes one; flags give nothing."""
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if not word.startswith("--"):
            yield None, word
            continue
        name, equals, value = word.partition("=")
        if kinds.get(name, "flag") == "flag" and not equals:
            continue  # a flag (an unknown option counts as one)
        if not equals:
            if index >= len(words):
                break
            value = words[index]
            index += 1
        yield name, value


def query_recipes(
    examples: list[CommandExample], inventory: Mapping[str, JsonValue]
) -> list[QueryRecipe]:
    """The ``graph query`` examples that carry a Cypher text."""
    options = _options(inventory)
    kinds = options.get(("query",), {})
    found: list[QueryRecipe] = []
    for example in examples:
        if example.words[0] != "query":
            continue
        text: str | None = None
        params: dict[str, str] = {}
        for option, value in _arguments(example.words[1:], kinds):
            if option is None:
                text = _unescaped(value) if text is None else text
            elif option == "--param":
                name, equals, given = value.partition("=")
                if equals:
                    params[name] = given
        if text is not None:
            found.append(QueryRecipe(example.path, example.line, text, params))
    return found


def _unescaped(text: str) -> str:
    """A Cypher text written in double quotes has ``\\$param`` for the shell; the query is
    ``$param`` (a backslash before ``$`` is no Cypher)."""
    return text.replace("\\$", "$")


# ---- generic examples ---------------------------------------------------------------------


def model_terms(documents: Mapping[str, JsonValue]) -> frozenset[str]:
    """The names of the graph an example can use (see the module docstring), from the model
    documents of ``describe_model``."""
    bundle = as_object(dict(documents), "model")
    found: set[str] = set()
    for key, listing in (("types", "types"), ("relations", "relations")):
        for item in req_list(req_obj(bundle, key), listing):
            _add_curie(found, req_str(as_object(item, f"{key}[]"), "curie"))
    for module in req_list(bundle, "modules"):
        found.add(req_str(as_object(module, "modules[]"), "id"))
    for document in req_obj(bundle, "type").values():
        body = req_obj(as_object(document, "type"), "type")
        for field in req_list(body, "fields"):
            attribute = as_object(field, "fields[]")
            name = req_str(attribute, "name")
            if name not in SYSTEM_FIELDS:
                found.add(name)
            values = attribute.get("values")
            if isinstance(values, list):
                found.update(v for v in values if isinstance(v, str))
        for lifecycle in req_list(body, "lifecycles"):
            for state in req_list(as_object(lifecycle, "lifecycles[]"), "states"):
                found.add(req_str(as_object(state, "states[]"), "name"))
    for key in ("functions", "actions"):
        catalog = bundle.get(key)
        if isinstance(catalog, dict):
            for entry in req_list(catalog, key):
                found.add(req_str(as_object(entry, f"{key}[]"), "name"))
    return frozenset(found)


def _add_curie(found: set[str], curie: str) -> None:
    found.add(curie)
    found.add(curie.partition(":")[2])


def generic_examples(
    examples: list[CommandExample],
    terms: frozenset[str],
    graph: str,
    inventory: Mapping[str, JsonValue],
) -> list[CommandExample]:
    """The fenced examples of a real command that name no term of the graph (``terms``, see
    ``model_terms``). ``graph``: the graph's own name, which a ``--graph`` example uses and which
    is no term either. Only what names things counts: positional words, options that take types,
    relations, fields, filters or assignments (the field of ``--set field=value``), not free text
    such as ``--reason``. A placeholder the tool's own examples do not use (``<title> <kind>``)
    names something of the graph in the maintainer's words, so such an example is kept, also in
    a ``--title``."""
    options = _options(inventory)
    vocabulary = _placeholder_words(inventory)
    found = []
    for example in examples:
        path = _command_path(example.words, options)
        if not example.fenced or path is None:
            continue
        kinds = options[path]
        named: list[str] = []  # text whose words may be terms
        written: list[str] = []  # text whose placeholders may say something about the graph
        for option, value in _arguments(example.words[len(path) :], kinds):
            if option == "--graph":
                continue
            if option is None or kinds.get(option) in NAMING_TYPES or option in NAMING_OPTIONS:
                named.append(value)
                written.append(value)
            elif kinds.get(option) in NAME_PART_TYPES:  # ``field=value``: the field names
                named.append(value.partition("=")[0])
                written.append(value)
            elif option in DATA_OPTIONS:
                written.append(value)
        specific = any(
            not set(WORD_RE.findall(placeholder.lower())) <= vocabulary
            for placeholder in PLACEHOLDER_RE.findall(" ".join(written))
        )
        for token in TOKEN_RE.findall(PLACEHOLDER_RE.sub(" ", " ".join(named))):
            if token != graph and {token, *token.split(":")} & terms:
                specific = True
        if not specific:
            found.append(example)
    return found


def _placeholder_words(inventory: Mapping[str, JsonValue]) -> frozenset[str]:
    """The words inside the ``<placeholders>`` of the examples of the commands."""
    words: set[str] = set()
    for command in req_list(as_object(dict(inventory), "inventory"), "commands"):
        for example in as_list(as_object(command, "commands[]").get("examples", []), "examples"):
            line = req_str(as_object(example, "examples[]"), "command")
            for placeholder in PLACEHOLDER_RE.findall(line):
                words.update(WORD_RE.findall(placeholder.lower()))
    return frozenset(words)
