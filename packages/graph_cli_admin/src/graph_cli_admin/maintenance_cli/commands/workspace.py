"""``graph-admin workspace sync DIR``: a maintainer workspace with one folder per graph.

A workspace is a directory an agent maintains graphs from, on any machine:
the admin skills in ``DIR/.claude/skills/``, a short ``DIR/CLAUDE.md``
pointing at them, and one folder per graph as ``graph pull`` writes it,
named ``<graph>_<the last six hex digits of its id>`` so two installations'
graphs of one name never share a folder. A folder is found by its base file
(``.graph_cli_source.json``), whatever it is called, so renaming it is fine.

``sync`` installs or refreshes the skills, writes ``CLAUDE.md`` only when
there is none, and pulls every graph that has no folder yet. It never
replaces a folder: one whose graph moved past its base is ``behind`` (pull
it again with ``--force`` once its edits are pushed), and a folder name
taken by something else is a ``conflict``. It runs here in both modes and
reaches the graphs through the handlers of ``graph list`` and ``graph pull``
(locally, or the admin API's in the remote mode), so authorization, the
audit and the checks of a pull are theirs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, STRING, array, enum, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli_admin.maintenance_cli.commands.common import DATA_ROOT, ROOT_ERRORS
from graph_cli_admin.maintenance_cli.commands.skills import STEP, plan
from graph_cli_admin.maintenance_cli.wiring import Services
from graph_cli_admin.services_admin import sources

SUFFIX_DIGITS: Final = 6
INSTRUCTIONS: Final = "CLAUDE.md"
LIST: Final = ("graph", "list")
PULL: Final = ("graph", "pull")
INSTRUCTIONS_TEXT: Final = """\
# Graph maintenance workspace

This folder maintains the graphs of a graph_cli installation with `graph-admin`.
Load the `maintaining-graphs` skill before the first `graph-admin` command here.

- One folder per graph, `<graph>_<id suffix>/`, as `graph-admin graph pull` writes it:
  `ontology/<version>/`, `skill/graph-<graph>/`, `functions/<name>.yaml`,
  `actions/<name>.yaml` and `views/<name>.yaml` (the last four only when the graph has them) and
  `.graph_cli_source.json` (never edit that file). Notes about a graph go into a `README.md`
  in its folder.
- `graph-admin workspace sync .` refreshes the skills in `.claude/skills/` and adds a
  folder for every graph that has none yet.

Written once by `graph-admin workspace sync`; edit it as you like, it is never replaced.
"""


def folder_name(name: str, graph_id: str) -> str:
    """``<graph>_<the last six hex digits of the id>``: the end of a UUIDv7 is random, its
    start is the creation time, so graphs created together still differ."""
    return f"{name}_{graph_id.replace('-', '')[-SUFFIX_DIGITS:]}"


def sync(
    state: CommandState, target: str, force: bool, dry_run: bool, data_root: str | None
) -> Result:
    from graph_cli.skills.install import SkillTarget, apply_install, refusal

    directory = Path(target).resolve()
    if directory.exists() and not directory.is_dir():
        raise _usage(f"{target} is not a directory")
    items = _run(state, LIST, include_archived=False, data_root=data_root).get("items")
    graphs = (
        [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    )
    folders = _folders(directory)
    planned: list[dict[str, JsonValue]] = []
    for item in sorted(graphs, key=lambda g: (str(g.get("name")), str(g.get("id")))):
        graph_id, name = str(item.get("id")), str(item.get("name"))
        ontology = item.get("ontology")
        version = ontology.get("version") if isinstance(ontology, Mapping) else None
        entry: dict[str, JsonValue] = {"graph": name, "id": graph_id, "version": version}
        if graph_id in folders:
            folder, base = folders[graph_id]
            moved = base.pin.version != version
            entry |= {"folder": folder, "action": "behind" if moved else "present"}
        else:
            folder = folder_name(name, graph_id)
            taken = (directory / folder).exists() or (directory / folder).is_symlink()
            entry |= {"folder": folder, "action": "conflict" if taken else "pull"}
        planned.append(entry)

    skills = SkillTarget("project", directory / ".claude", directory)
    install = plan(skills, force=force)
    instructions = directory / INSTRUCTIONS
    write_instructions = not (instructions.exists() or instructions.is_symlink())
    error: GraphCliError | None = None
    if not dry_run:
        try:
            skills.config_dir.mkdir(parents=True, exist_ok=True)
            if write_instructions:
                instructions.write_text(INSTRUCTIONS_TEXT, encoding="utf-8")
        except OSError as exc:
            raise _usage(f"cannot set up {directory}: {exc.strerror or exc}") from exc
        apply_install(install)
        for entry in planned:
            if entry["action"] != "pull":
                continue
            try:
                _run(
                    state,
                    PULL,
                    ref=str(entry["id"]),
                    target=str(directory / str(entry["folder"])),
                    version=None,
                    force=False,
                    data_root=data_root,
                )
                entry["action"] = "pulled"
            except GraphCliError as exc:
                entry |= {"action": "failed", "error": exc.code}
                error = error or exc
    conflicts = [str(e["folder"]) for e in planned if e["action"] == "conflict"]
    if error is None and conflicts:
        error = GraphCliError(
            "admin.workspace_conflict",
            f"{', '.join(conflicts)} in {directory} hold no pull of their graph and were left"
            " as they are; move them away, or pull the graph into another folder with"
            " graph-admin graph pull",
            details={"dir": str(directory), "folders": list(conflicts)},
        )
    error = error or refusal(install, {"dir": str(directory)})
    data: dict[str, JsonValue] = {
        "path": str(directory),
        "dry_run": dry_run,
        "instructions": "create" if write_instructions else "kept",
        "skills": [step.to_json() for step in install.steps],
        "graphs": planned,
    }
    return Result(data=data, error=error)


def _folders(directory: Path) -> dict[str, tuple[str, sources.Base]]:
    """``{graph id: (folder, base)}`` of the folders below ``directory`` that hold a base
    file; a folder with a broken one is skipped (``graph push`` names what is wrong)."""
    found: dict[str, tuple[str, sources.Base]] = {}
    if not directory.is_dir():
        return found
    for child in sorted(directory.iterdir()):
        if child.is_symlink() or not (child / sources.SOURCE_FILE).is_file():
            continue
        try:
            base = sources.read_base(child)
        except GraphCliError:
            continue
        found.setdefault(base.graph_id, (child.name, base))
    return found


def _run(state: CommandState, path: tuple[str, ...], **values: Any) -> Mapping[str, JsonValue]:
    """The data of another command run with its own handler, the remote one in the remote
    mode; its error is raised, its warnings join this command's."""
    from graph_cli_admin.maintenance_cli.commands import all_specs

    services = state.services
    assert isinstance(services, Services)
    specs = all_specs()
    if services.remote_mode():
        from graph_cli_admin.maintenance_cli.remote import remote_specs

        specs = remote_specs(specs)
    spec = next(s for s in specs if s.path == path)
    result = spec.handler(state, **values)
    if result.forwarded is None:
        state.warnings += result.warnings
        return result.data if isinstance(result.data, Mapping) else {}
    document = result.forwarded.document
    warnings = document.get("warnings")
    for warning in warnings if isinstance(warnings, list) else ():
        if isinstance(warning, Mapping):
            details = warning.get("details")
            state.warnings.append(
                WarningRecord(
                    str(warning.get("code")),
                    str(warning.get("message")),
                    details if isinstance(details, Mapping) else {},
                )
            )
    failed = document.get("error")
    if isinstance(failed, Mapping):
        details = failed.get("details")
        raise GraphCliError(
            str(failed.get("code")),
            str(failed.get("message")),
            details=details if isinstance(details, Mapping) else None,
        )
    data = document.get("data")
    return data if isinstance(data, Mapping) else {}


def _usage(message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": "dir"})


GRAPH: Final = obj(
    {
        "graph": STRING,
        "id": STRING,
        "version": nullable(STRING),
        "folder": STRING,
        "action": enum("pull", "pulled", "present", "behind", "conflict", "failed"),
    },
    {"error": STRING},
)
OUTPUT: Final = obj(
    {
        "path": STRING,
        "dry_run": BOOLEAN,
        "instructions": enum("create", "kept"),
        "skills": array(STEP),
        "graphs": array(GRAPH),
    }
)

SPECS: Final = (
    CommandSpec(
        path=("workspace", "sync"),
        kind="read",
        surface="model",
        action=None,
        summary="Set up or refresh a maintainer workspace in DIR: admin skills, one folder"
        " per graph.",
        params=(
            ParamSpec(
                "dir",
                "directory",
                "Workspace directory; created if missing.",
                required=True,
                positional=True,
                keyword="target",
            ),
            ParamSpec(
                "--force",
                "flag",
                "Replace admin skill directories that are not ours or were edited by hand.",
            ),
            ParamSpec(
                "--dry-run", "flag", "Show what would be installed and pulled; write nothing."
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin workspace sync <dir>", "Set up a workspace, or refresh it."),
            Example("graph-admin workspace sync . --dry-run --json", "Show the plan."),
        ),
        errors=(
            *ROOT_ERRORS,
            "usage.invalid_argument",
            "access.denied",
            "admin.workspace_conflict",
            "admin.source_exists",
            "skills.not_owned",
            "skills.modified",
            "skills.target_invalid",
        ),
        handler=sync,
        output=OUTPUT,
        remote="client",
        description="Installs the admin skills into DIR/.claude/skills (as graph-admin skills"
        " install --scope project), writes DIR/CLAUDE.md when there is none, and pulls every"
        " graph without a folder into DIR/<graph>_<last six hex digits of its id>/ (as"
        " graph-admin graph pull). A folder is found by its .graph_cli_source.json, whatever"
        " its name. Folders are never replaced: one whose graph has another ontology release"
        " pinned than it was pulled from is reported as behind; a folder name taken by"
        " anything else is a conflict (exit 1, the rest is still done). Archived graphs are"
        " left out.",
    ),
)
