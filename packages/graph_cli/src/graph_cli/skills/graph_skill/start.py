"""The ``start`` block: how to begin working with the graph.

- ``start``: run ``graph start`` if not done in this session, load the ``graph-cli`` skill
  before the first command, how to tell the graph is reachable, the graph language, and the
  rules of working with ``graph`` that apply from the first call (plain calls, the envelope,
  asking for less, never guessing, functions and actions first, dry runs, ``--reason`` and
  ``--expect``, findings), so that a graph skill loaded without skill 1 still leads to safe
  calls (F42, F74); for a graph whose types have names (label roles) the rule to look a name
  up before a create (the ``names`` block keeps only what is specific to the types); the
  consent rule: ask the user before ``retract``, and before a ``transition`` into a terminal
  state when some lifecycle has terminal states; for a model with ``files:File``, one line on
  the file workflow; for a graph with published views a clause on the ``graph view`` rule that
  names ``graph describe views``.

The compiled view catalog (``graph_cli.view_catalog/1``, the key ``views``) is one more input
document, present only when the graph has views the reader may see. Only its presence shows: one
clause in the ``start`` block says the graph has views and where to list them (no list in
level 0: discovery is ``graph describe views`` and ``describe search``). A graph without active
views renders exactly as before.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.json_access import req_str
from graph_cli.skills.graph_skill.model import Model, objects, strings
from graph_cli.skills.graph_skill.naming import labelled_fields
from graph_cli.skills.graph_skill.pieces import code, plain

LANG_BASES: Final = frozenset({"lang_string", "lang_text"})
FILE_TYPE: Final = "files:File"  # the standard module files (B7.8), not graph vocabulary


def _patterns_source(doc: Model) -> str:
    """Where the query patterns are read: the whole schema, or one module at a time."""
    if doc.modular:
        return (
            f"read the module first with `graph describe module <module> --graph {doc.name}`;"
            f" `graph describe schema --module <module> --graph {doc.name} --compact --json`"
            " lists its patterns"
        )
    return "`graph describe schema --compact --json` lists the patterns"


def render_start(doc: Model) -> list[str]:
    name = doc.name
    title = f" (title: {plain(doc.title)})" if doc.title else ""
    return [
        "If you have not yet in this session: `graph start --json` (it refreshes stale skills,"
        " lists the graphs and what your token may use of each, and how to search them).",
        "",
        "Load the `graph-cli` skill as well before your first `graph` command: it has every"
        " command with its options, the change sets of `graph apply` and the error codes. The"
        " rules of working with `graph` below hold from the first call, with or without it.",
        "",
        f"Use this skill only if an item named {code(name)} is listed{title} by `graph start` or"
        f" `graph graphs --json`. Pass `--graph {name}` to every command, as the examples here do,"
        f" so they work in every directory; a project whose pointer makes {code(name)}"
        " the default may leave it out.",
        *_language(doc),
        "",
        "Working with `graph`:",
        "",
        "- Run each call as a plain command that starts with `graph` and passes `--json`"
        + (
            " (the module views of `graph describe module` read better as text)"
            if doc.modular
            else ""
        )
        + ": no `cd`, variables, loops, pipes or heredocs, which a restricted harness refuses."
        " If a call is refused, run it again in that form before you give up.",
        "- Read the answer: `ok`, then `data`; on failure `error.hint` is the command to run"
        " next. Never cut an answer short; ask for less: `--fields <attr>,<attr>` on `find`,"
        " `get` and `neighbors`, or `graph query` with `RETURN` of just the columns you need"
        f" ({_patterns_source(doc)}).",
        "- For what is known about one entity, `graph view <id> --json` answers in one call (key"
        " fields, relations with counts, files, what applies next): use it before walking edges"
        " with `get` and `neighbors`."
        + (
            f" The graph also has published views: `graph describe views --graph {name}`."
            if doc.views
            else ""
        ),
        "- Never guess an option, type, field, relation or state: `graph <command> --help`"
        f" lists the options, `graph describe type <type> --graph {name} --json` the fields,"
        " states and relations.",
        *(
            [
                "- For a question, run a named function when one fits (`graph run <function>"
                " --param <name>=<value>`); write a `graph query` only when none does. An empty"
                " value in a function's answer means nothing is recorded: say so instead of"
                " searching further."
            ]
            if doc.functions
            else []
        ),
        (
            "- For a change, run a named action when one fits (`graph do <action> --param"
            " <name>=<value> --dry-run`, read what it will change, then run it without"
            " `--dry-run`). Otherwise preview"
            if doc.actions
            else "- Preview"
        )
        + " `create`, `apply`, `transition`, `supersede` and `retract` with `--dry-run`, then"
        " run them without; one `update`, `link` or `unlink` may run directly. Say why in"
        " `--reason`, with how you learned it (in English, whatever language the values are in),"
        " never in a field value, and pass `--expect <rev>` when the write depends on what you"
        " read. A change set for `graph apply` goes into a file in the working directory.",
        *(
            [
                "- Before you create an entity with a name, look the name up (`graph lookup"
                " '<name>' --json`) and use the one that exists, never a second one under a"
                " similar name: the write finding `data.semantic.possible_duplicate` names near"
                " spellings, also on a dry run."
            ]
            if labelled_fields(doc)
            else []
        ),
        "- Ask the user before `retract`"
        + (
            " and before a `transition` into a terminal state (`graph describe type` lists them"
            " under `terminal`)."
            if _terminal_states(doc)
            else "."
        ),
        "- Read `data.findings` of every write and tell the user about each warning.",
        *(
            [
                "",
                f"The graph stores files: `graph file add <file> --graph {name} --json`"
                " stores one, and entities point to it with `files:from_file` and the page;"
                " the `graph-cli` skill section Files says how.",
            ]
            if FILE_TYPE in doc.types
            else []
        ),
    ]


def _terminal_states(doc: Model) -> bool:
    """Whether some lifecycle of some type has terminal states."""
    return any(
        strings(lifecycle.get("terminal"))
        for body in doc.types.values()
        for lifecycle in objects(body, "lifecycles")
    )


def _language(doc: Model) -> list[str]:
    """The graph language, and the tagged form when the model has language-tagged fields."""
    if doc.language is None:
        return []
    text = f"Text values are written in the graph language {code(doc.language)}."
    tagged = any(
        req_str(f, "base") in LANG_BASES
        for body in doc.types.values()
        for f in objects(body, "fields")
    )
    if tagged:
        text += (
            " A field of base `lang_string` or `lang_text` takes another language as"
            f" `<text>@<tag>`; without a tag it gets {code(doc.language)}."
        )
    return ["", text]
