"""``graph start``: the first call of a session (graph start plan S1-S3).

One short answer with what an agent needs to begin: whether the program and the installed skills
are current (stale copies are refreshed, as ``graph self update`` does, but the program itself is
never replaced), who acts, and for each graph the agent reaches how many modules, types, functions,
actions and views its token may use and how many it sees but may not use, with the commands that
search them. Counts are what the token may use; a hidden function, action or view is in no number.

A meta command: the skill check never gates it (it is how stale copies are found), it writes
nothing but skill files, and it never replaces the program. In the remote mode the graphs come
from the server (``POST /v1/commands/start``) and the program and the skills are judged here
(``app_cli.remote_hooks.start_hook``); on the server only the graphs are answered.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, PROGRAM, context, services
from graph_cli.app_cli.commands.self_update import DIR, skill_scopes
from graph_cli.app_cli.output_schemas import NULLABLE_STRING
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import INTEGER, STRING, array, enum, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_list, as_object, req_str

if TYPE_CHECKING:
    from graph_cli.catalog.remote import RemoteSession

START_SCHEMA: Final = "graph_cli.start/1"
CHECK: Final = ParamSpec("--check", "flag", "Only report what is stale; refresh nothing.")
TEXT_GRAPHS: Final = 5  # graphs the text names; the JSON has them all
TITLE_WIDTH: Final = 20
SKILL_STATES: Final = (
    "fresh",
    "refreshed",
    "stale",
    "missing",
    "modified",
    "foreign",
    "unavailable",
)
COUNT_KINDS: Final = ("functions", "actions", "views")

NOT_ALLOWED: Final = obj(optional={"functions": INTEGER, "actions": INTEGER, "views": INTEGER})
COUNTS: Final = obj(
    {
        "modules": INTEGER,
        "types": INTEGER,
        "functions": INTEGER,
        "actions": INTEGER,
        "views": INTEGER,
    }
)
OUTPUT: Final = obj(
    {
        "schema": enum(START_SCHEMA),
        "identity": obj({"acting": STRING, "project": NULLABLE_STRING, "roles": array(STRING)}),
        "graphs": array(
            obj(
                {
                    "name": STRING,
                    "title": NULLABLE_STRING,
                    "level": enum("none", "read", "write"),
                    "skill": NULLABLE_STRING,
                    "counts": {"anyOf": [COUNTS, {"type": "null"}]},
                },
                {
                    "not_allowed": NOT_ALLOWED,
                    "modules": array(
                        obj(
                            {"id": STRING, "summary": STRING, "types": INTEGER},
                            {
                                "functions": INTEGER,
                                "actions": INTEGER,
                                "views": INTEGER,
                                "not_allowed": NOT_ALLOWED,
                            },
                        )
                    ),
                },
            )
        ),
        "hidden_count": INTEGER,
    },
    {
        # the program and the skills are judged where the command runs: not in a server's answer
        "program": obj(
            {
                "name": STRING,
                "version": STRING,
                "state": enum("local", "current", "outdated", "unchecked"),
            },
            {"build": NULLABLE_STRING, "server_build": NULLABLE_STRING},
        ),
        "skills": array(
            obj(
                {"scope": STRING, "state": enum(*SKILL_STATES, "error")},
                {"skill": NULLABLE_STRING, "reason": STRING},
            )
        ),
    },
)


# ---- what the server (or a local program) knows: the graphs ------------------------------


def orientation(state: CommandState, graph: str | None) -> JsonObject:
    """The identity and the graphs the caller reaches, with their counts; with ``graph`` that
    graph alone and its modules."""
    ctx = context(state)
    hidden = 0
    if graph is not None:
        entries = [ctx.select(graph)]
    else:
        allowed = ctx.pointer.graphs if ctx.pointer is not None else None
        entries, hidden = ctx.reader.reachable(ctx.actor, allowed=allowed)
    items = ctx.reader.start_graphs(ctx.actor, entries)
    if graph is not None:
        items[0]["modules"] = list(ctx.reader.start_modules(ctx.actor, entries[0]))
    project = ctx.actor.project
    return {
        "schema": START_SCHEMA,
        "identity": {
            "acting": ctx.actor.label(),
            "project": project.name or project.id,
            "roles": list(ctx.actor.roles),
        },
        "graphs": list(items),
        "hidden_count": hidden,
    }


# ---- what only the client knows: the program and the skills ------------------------------


def program_state(session: RemoteSession | None) -> dict[str, JsonValue]:
    """This program, and in the remote mode how its build compares with the server's. Never
    replaces it: ``graph self update`` does."""
    block: dict[str, JsonValue] = {"name": PROGRAM, "version": __version__, "state": "local"}
    if session is None:
        return block
    import graph_cli
    from graph_cli.catalog.dist import Installer, program_step

    try:
        step = program_step(
            session.client,
            PROGRAM,
            package_init=graph_cli.__file__,
            state_root=Path(),
            installer=Installer({}),
            check=True,
        )
    except GraphCliError:
        return {**block, "state": "unchecked"}  # the server hands out no build to compare with
    action = step["action"]
    block["state"] = {"current": "current", "outdated": "outdated"}.get(str(action), "unchecked")
    block["build"] = step.get("build")
    block["server_build"] = step.get("server")
    return block


def refreshed(state: CommandState, *, check: bool, directory: str | None) -> list[JsonValue]:
    """The installed skill copies, refreshed unless ``check`` (the code of ``graph self update``
    without the program swap: ``skills install --project-graphs`` for each scope that has a
    skill of this program, in this process): ``{scope, skill, state}`` each."""
    from graph_cli.app_cli.commands.skills import install

    app = services(state)
    base = app.path(directory) if directory is not None else app.cwd
    found: list[JsonValue] = []
    for scope, project in skill_scopes(app, base):
        try:
            result = install(
                state, scope, str(project) if project else None, (), False, check, True
            )
        except GraphCliError as exc:  # one scope that cannot be read does not end the answer
            found.append({"scope": scope, "skill": None, "state": "error", "reason": exc.code})
            continue
        data = as_object(result.data, "install")
        for value in as_list(data["skills"], "skills"):
            step = as_object(value, "step")
            found.append(
                {"scope": scope, "skill": req_str(step, "skill"), "state": _state(step, check)}
            )
        for value in as_list(data["unavailable"], "unavailable"):
            gone = as_object(value, "unavailable")
            found.append(
                {
                    "scope": scope,
                    "skill": req_str(gone, "skill"),
                    "state": "unavailable",
                    "reason": req_str(gone, "reason"),
                }
            )
    return found


def _state(step: Mapping[str, JsonValue], check: bool) -> str:
    """What became of one skill copy: the plan's action and what was installed there."""
    action = step["action"]
    if action == "unchanged":
        return "fresh"
    if action == "refused":
        return "foreign" if step["installed"] == "foreign" else "modified"
    if action == "replace":
        return "stale" if check else "refreshed"
    return "missing" if check else "refreshed"  # create


def client_part(
    state: CommandState, *, check: bool, directory: str | None, session: RemoteSession | None
) -> JsonObject:
    return {
        "program": program_state(session),
        "skills": refreshed(state, check=check, directory=directory),
    }


# ---- the text ----------------------------------------------------------------------------


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


NAMES_SHOWN: Final = 2  # skill copies named per state; the rest counted


def _named(names: list[str]) -> str:
    shown = ", ".join(names[:NAMES_SHOWN])
    return shown + (f" and {len(names) - NAMES_SHOWN} more" if len(names) > NAMES_SHOWN else "")


def _skill_lines(data: JsonObject) -> list[str]:
    """At most three lines, whatever the states: what was refreshed (or, with ``--check``, what
    would be), what is left as it is, and how many are fresh."""
    copies = [as_object(v, "skill") for v in as_list(data["skills"], "skills")]
    if not copies:
        return ["skills: none installed here"]
    by_state: dict[str, list[str]] = {}
    for copy in copies:
        name = str(copy["skill"]) if copy.get("skill") else str(copy["scope"])
        names = by_state.setdefault(str(copy["state"]), [])
        if name not in names:
            names.append(name)
    lines: list[str] = []
    if "refreshed" in by_state:  # the plan's sentence: a copy loaded before is out of date now
        lines.append(
            f"{_named(by_state['refreshed'])} refreshed; if loaded already in this session,"
            " load again."
        )
    due = [(n, s) for s in ("stale", "missing") for n in by_state.get(s, ())]
    if due:  # only with --check
        lines.append(f"skills to refresh with {PROGRAM} start: {_listed(due)}")
    left = [
        (n, s) for s in ("modified", "foreign", "unavailable", "error") for n in by_state.get(s, ())
    ]
    if left:
        lines.append(f"skills left as they are: {_listed(left)}")
    fresh = len(by_state.get("fresh", ()))
    if not lines:
        return [f"skills: fresh ({fresh})"]
    return [*lines, *([f"skills fresh: {fresh}"] if fresh else [])]


def _listed(entries: list[tuple[str, str]]) -> str:
    shown = ", ".join(f"{name} ({state})" for name, state in entries[:NAMES_SHOWN])
    more = len(entries) - NAMES_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def _counts(item: Mapping[str, JsonValue]) -> str:
    counts = item.get("counts")
    if not isinstance(counts, Mapping):
        return "its model and catalogs are not open to you"
    refused = item.get("not_allowed")
    refused = refused if isinstance(refused, Mapping) else {}
    parts = [f"modules {counts['modules']}", f"types {counts['types']}"]
    for kind in COUNT_KINDS:
        extra = f" (+{refused[kind]} not allowed)" if kind in refused else ""
        parts.append(f"{kind} {counts[kind]}{extra}")
    return ", ".join(parts)


def _graph_line(item: Mapping[str, JsonValue]) -> str:
    title = f' "{_short(str(item["title"]), TITLE_WIDTH)}"' if item.get("title") else ""
    skill = "" if item.get("skill") else " (no skill)"
    return f"  {item['name']}{title} ({item['level']}): {_counts(item)}{skill}"


def render(data: JsonObject) -> str:
    """The answer for an agent: at most 1,500 bytes for up to five graphs with names of up to 24
    characters (more graphs are named in one line, the JSON has them all)."""
    lines: list[str] = []
    program = data.get("program")
    if isinstance(program, Mapping):
        head = f"{program['name']} {program['version']}"
        note = {
            "current": " (the server's build)",
            "outdated": f"; server build differs: {PROGRAM} self update",
        }.get(str(program["state"]), "")
        lines.append(head + note)
        lines += _skill_lines(data)
    who = as_object(data["identity"], "identity")
    project = f", project {who['project']}" if who.get("project") else ""
    lines.append(f"You: {who['acting']}{project}")
    graphs = [as_object(v, "graph") for v in as_list(data["graphs"], "graphs")]
    if not graphs:
        lines.append(f"Graphs: none reachable; {PROGRAM} whoami shows what your token may do")
    else:
        lines.append("Graphs (skill: graph-NAME, - for _):")
        lines += [_graph_line(g) for g in graphs[:TEXT_GRAPHS]]
        if len(graphs) > TEXT_GRAPHS:
            lines.append(f"  and {len(graphs) - TEXT_GRAPHS} more: {PROGRAM} graphs")
    hidden = data.get("hidden_count")
    if isinstance(hidden, int) and hidden:
        lines.append(f"  +{hidden} outside the pointer file: {PROGRAM} graphs")
    if len(graphs) == 1 and graphs[0].get("modules"):
        lines += _module_lines(graphs[0])
    lines += [
        f"Search: {PROGRAM} describe search TEXT [--kind function|action|view]"
        " [--module M] [--limit N]" + (" [--graph G]" if len(graphs) > 1 else ""),
        f"Module: {PROGRAM} describe module M",
        "Counts and hits are what your token may use; a hit marked not allowed exists, but your"
        " token cannot use it: tell the user instead of working around it.",
    ]
    return "\n".join(lines) + "\n"


def _module_lines(item: Mapping[str, JsonValue]) -> list[str]:
    lines = ["Modules:"]
    for value in as_list(item["modules"], "modules"):
        module = as_object(value, "module")
        refused = module.get("not_allowed")
        refused = refused if isinstance(refused, Mapping) else {}
        parts = [f"types {module['types']}"]
        for kind in COUNT_KINDS:
            if kind in module:
                extra = f" (+{refused[kind]} not allowed)" if kind in refused else ""
                parts.append(f"{kind} {module[kind]}{extra}")
        lines.append(f"  {module['id']}: {', '.join(parts)}  {_short(str(module['summary']), 60)}")
    return lines


# ---- the command -------------------------------------------------------------------------


def start(state: CommandState, graph: str | None, check: bool, dir: str | None) -> Result:
    app = services(state)
    data = orientation(state, graph)
    if app.request is None:  # a program, not a server: the skills and the program are here
        data = {**client_part(state, check=check, directory=dir, session=None), **data}
    return Result(data=data, text=render(data))


SPECS: Final = (
    CommandSpec(
        path=("start",),
        kind="meta",
        surface="meta",
        action=None,
        summary="First call of a session: what is current, the graphs you reach, what you may use.",
        params=(GRAPH, CHECK, DIR),
        examples=(
            Example("graph start", "Program and skills (stale copies refreshed), graphs, counts."),
            Example("graph start --json", "The same as graph_cli.start/1."),
            Example("graph start --graph <graph>", "One graph, with its modules."),
            Example("graph start --check", "Only report what is stale; change nothing."),
        ),
        errors=(*GRAPH_ERRORS, "remote.unreachable", "remote.unauthorized", "remote.protocol"),
        handler=start,
        output=OUTPUT,
        remote="hooked",
        description="Answers in a few lines: whether this program and the installed skills are"
        " current (a stale copy of a skill is refreshed in every scope that has one, as"
        " `graph self update` does; the program itself is never replaced, a different server"
        " build is only named), who acts, and per graph the number of modules, types,"
        " functions, actions and views your token may use, followed by the number of"
        " functions, actions and views you see but may not use. Counts and hits of"
        " `describe search` are what your token may use; a hit marked not allowed exists but"
        " is not yours to use. Skills edited by hand are reported, not replaced. With"
        " --graph, one graph and its modules. Never refused for a stale skill.",
    ),
)
