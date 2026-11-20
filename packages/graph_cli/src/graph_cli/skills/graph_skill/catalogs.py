"""The ``functions`` and ``actions`` blocks and the pages that list the catalogs.

- ``functions``: up to ``FEATURED_LIMIT`` of the graph's named functions, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), and the commands to list a module's functions and to search them. Without
  published functions it is one line saying so.
- ``actions``: up to ``ACTIONS_FEATURED_LIMIT`` of the actions the reader may run, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), the commands to list a module's actions and to search them, and the rule to dry
  run first. Without published actions it is one line saying so.

The compiled function catalog (``graph_cli.functions/1``, the key ``functions`` of the model
documents) is one more input document; the renderer reads only its public shape and never
imports ``graph_cli.kinetic``. A graph without active functions renders exactly as before.
With some, the ``modules`` block and the module pages (module ``modules``) get a ``Functions``
column and section, and a single-module graph the page ``references/functions.md``
(deprecated functions are left out everywhere).

The compiled action catalog (``graph_cli.action_catalog/1``, the key ``actions``) is one more
input document, present only when the graph has actions the reader may see; it is rendered
the same way: an ``Actions`` column and section in the ``modules`` block and the module
pages, and ``references/actions.md`` for a single-module graph. A graph without active
actions renders exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import as_object, opt_str, req_list, req_str
from graph_cli.skills.graph_skill.model import Model
from graph_cli.skills.graph_skill.pieces import code, md_table, plain

FEATURED_LIMIT: Final = 12  # functions listed in ``SKILL.md``, however many a graph has
ACTIONS_FEATURED_LIMIT: Final = 8  # actions listed in ``SKILL.md``, however many a graph has


def _param_text(param: JsonObject) -> str:
    """``name: type``, ``name?: type`` for an optional parameter (the form of ``graph describe
    functions`` and ``graph describe actions``)."""
    kind = req_str(param, "type")
    if kind == "ref":
        kind += f" {param.get('class')}"
    elif kind == "enum":
        kind += f" {param.get('enum')}"
    if param.get("many") is True:
        kind = f"list of {kind}"
    optional = "?" if param.get("default") is not None or param.get("optional") is True else ""
    return f"{req_str(param, 'name')}{optional}: {kind}"


def _signature(function: JsonObject) -> str:
    return ", ".join(_param_text(as_object(p, "params[]")) for p in req_list(function, "params"))


def _function_line(function: JsonObject) -> str:
    call = f"{req_str(function, 'name')}({_signature(function)})"
    return f"- {code(call)}: {plain(opt_str(function, 'summary'))}"


def render_functions(doc: Model) -> list[str]:
    """Level 0: a few featured functions and how to find the rest (K12)."""
    if not doc.functions:
        return ["This graph has no published functions."]
    name = doc.name
    featured = [f for f in doc.functions if f.get("featured") is True][:FEATURED_LIMIT]
    lines = [
        f"{len(doc.functions)} named functions; for a question use one before you write a query:"
        f" `graph run <function> --param <name>=<value> --graph {name} --json`.",
    ]
    if featured:
        lines += ["", *[_function_line(f) for f in featured]]
    lines += [
        "",
        f"More: `graph describe functions --module <module> --graph {name}`; find one:"
        f" `graph describe search <text> --kind function --graph {name} --json`; columns and use:"
        f" `graph describe function <function> --graph {name}`.",
    ]
    return lines


def _function_table(functions: Sequence[JsonObject]) -> list[str]:
    return md_table(
        ["Function", "Parameters", "Summary"],
        [
            [code(req_str(f, "name")), _signature(f) or "none", plain(opt_str(f, "summary"))]
            for f in functions
        ],
    )


def _function_hint(doc: Model) -> list[str]:
    return [
        "",
        f"Run one with `graph run <function> --param <name>=<value> --graph {doc.name} --json`;"
        f" `graph describe function <function> --graph {doc.name}` gives its columns and when to"
        " use it.",
    ]


def functions_file(doc: Model, stamp: str) -> str:
    """``references/functions.md`` of a single-module graph."""
    lines = [
        stamp,
        "",
        f"# Functions of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is `graph describe functions"
        f" --graph {doc.name} --json`; it wins when they differ.",
        "",
        *_function_table(doc.functions),
        *_function_hint(doc),
    ]
    return "\n".join(lines) + "\n"


def module_functions(doc: Model, module_id: str) -> list[str]:
    """The ``Functions`` section of a module page (none for a graph without functions)."""
    if not doc.functions:
        return []
    own = [f for f in doc.functions if opt_str(f, "module") == module_id]
    if not own:
        return ["", "## Functions", "", "None."]
    return ["", "## Functions", "", *_function_table(own), *_function_hint(doc)]


def _action_summary(action: JsonObject) -> str:
    """The summary, and that the action needs ``--reason`` when it does (saves a refusal)."""
    summary = plain(opt_str(action, "summary"))
    return f"{summary} Needs `--reason`." if action.get("reason") == "required" else summary


def _action_line(action: JsonObject) -> str:
    call = f"{req_str(action, 'name')}({_signature(action)})"
    return f"- {code(call)}: {_action_summary(action)}"


def render_actions(doc: Model) -> list[str]:
    """Level 0: a few featured actions, how to find the rest and the dry-run rule (A11)."""
    if not doc.actions:
        return ["This graph has no published actions."]
    name = doc.name
    featured = [a for a in doc.actions if a.get("featured") is True][:ACTIONS_FEATURED_LIMIT]
    lines = [
        f"{len(doc.actions)} named actions; for a change use one before you write with the"
        f" primitives. Dry run first: `graph do <action> --param <name>=<value> --graph {name}"
        " --dry-run`, read what it will change, then run it without `--dry-run`.",
    ]
    if featured:
        lines += ["", *[_action_line(a) for a in featured]]
    lines += [
        "",
        f"More: `graph describe actions --module <module> --graph {name}`; find one:"
        f" `graph describe search <text> --kind action --graph {name} --json`; preconditions and"
        f" use: `graph describe action <action> --graph {name}`. For an entity you already have,"
        f" `graph get <id> --affordances --graph {name}` lists the actions that fit it, whether"
        " each applies and what blocks it.",
    ]
    return lines


def _action_table(actions: Sequence[JsonObject]) -> list[str]:
    return md_table(
        ["Action", "Parameters", "Summary"],
        [[code(req_str(a, "name")), _signature(a) or "none", _action_summary(a)] for a in actions],
    )


def _action_hint(doc: Model) -> list[str]:
    return [
        "",
        f"Dry run first: `graph do <action> --param <name>=<value> --graph {doc.name}"
        " --dry-run`, read what it will change, then run it without `--dry-run`;"
        f" `graph describe action <action> --graph {doc.name}` gives its preconditions and when"
        " to use it.",
    ]


def actions_file(doc: Model, stamp: str) -> str:
    """``references/actions.md`` of a single-module graph."""
    lines = [
        stamp,
        "",
        f"# Actions of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is `graph describe actions"
        f" --graph {doc.name} --json`; it wins when they differ.",
        "",
        *_action_table(doc.actions),
        *_action_hint(doc),
    ]
    return "\n".join(lines) + "\n"


def module_actions(doc: Model, module_id: str) -> list[str]:
    """The ``Actions`` section of a module page (none for a graph without actions)."""
    if not doc.actions:
        return []
    own = [a for a in doc.actions if opt_str(a, "module") == module_id]
    if not own:
        return ["", "## Actions", "", "None."]
    return ["", "## Actions", "", *_action_table(own), *_action_hint(doc)]
