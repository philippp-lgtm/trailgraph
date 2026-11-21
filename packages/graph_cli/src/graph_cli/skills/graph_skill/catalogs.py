"""The ``functions`` and ``actions`` blocks.

- ``functions``: up to ``FEATURED_LIMIT`` of the graph's named functions, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), and the commands to run, list and search them. Without published functions it is one
  line saying so.
- ``actions``: up to ``ACTIONS_FEATURED_LIMIT`` of the actions the reader may run, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), the commands to list a module's actions and to search them, and the rule to dry
  run first. Without published actions it is one line saying so.

No count, list per module or file leaves the catalogs into the skill: a function, action or
view that is published, changed or removed does not change the render unless it is (or stops
being) featured; lists and counts come from the tool at the point of use (``graph start``,
``graph describe``). What the render takes from the catalogs is the featured lines and whether
the graph has functions, actions and views at all, which is what the ``featured`` digest of the
claim covers (``kernel.skill_featured``). ``references/functions.md`` and
``references/actions.md`` are no longer generated; their names stay reserved for templates.

The compiled function catalog (``graph_cli.functions/1``, the key ``functions`` of the model
documents) and the compiled action catalog (``graph_cli.action_catalog/1``, the key ``actions``,
present only when the graph has actions the reader may see) are input documents; the renderer
reads only their public shape and never imports ``graph_cli.kinetic``. A graph without active
functions or actions has no such lines.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import as_object, opt_str, req_list, req_str
from graph_cli.skills.graph_skill.model import Model
from graph_cli.skills.graph_skill.pieces import code, plain

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
    if doc.functions_withheld:
        return [
            "This token may not run named functions in this graph (`read.function`): do not"
            " look for one or write a query instead; tell the user what you cannot do."
        ]
    if not doc.functions:
        return ["This graph has no published functions."]
    name = doc.name
    featured = [f for f in doc.functions if f.get("featured") is True][:FEATURED_LIMIT]
    lines = [
        "Named functions: for a question use one before you write a query:"
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
        "Named actions: for a change use one before you write with the"
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
