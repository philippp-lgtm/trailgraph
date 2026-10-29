"""The admin skills: hand-written agent skills that ship with graph-admin.

They live in ``bundled_skills/<name>/`` of this package, so every install of
graph-admin, the wheel the server hands out included, carries the version
that fits it. ``graph-admin skills install`` and ``workspace sync`` copy them
with the installer of ``graph skills install`` (``graph_cli.skills.install``),
marked with kind ``admin``. Their names never start with ``graph-``: ``graph``
judges and refreshes only ``graph-cli`` and ``graph-*``, so neither program
touches the other's skills. They are graph-admin's agent guidance, so they
go into its compatibility id (``admin_compat``, contracts B14.16).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import graph_cli_admin
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

if TYPE_CHECKING:
    from graph_cli.skills.install import Owner, RenderedSkill

PROGRAM: Final = "graph-admin"
BUNDLED: Final = Path(graph_cli_admin.__file__).parent / "bundled_skills"
NAMES: Final = (
    "authoring-graph-functions",
    "authoring-graph-skills",
    "maintaining-graphs",
    "ontology-best-practices",
)


def program_block() -> dict[str, JsonValue]:
    return {"name": PROGRAM, "version": graph_cli_admin.__version__}


def owner() -> Owner:
    from graph_cli.skills.install import Owner
    from graph_cli.skills.marker import SkillKind

    kinds: tuple[SkillKind, ...] = ("admin",)
    return Owner(kinds, lambda name: name in NAMES)


def admin_skills() -> list[RenderedSkill]:
    """Every admin skill as this program installs it."""
    from graph_cli.skills.install import rendered
    from graph_cli.skills.tree import read_tree

    found = []
    for name in NAMES:
        try:
            files, problems = read_tree(BUNDLED / name)
        except OSError as exc:
            raise GraphCliError(
                "internal.error", f"cannot read the bundled skill {name}: {exc}"
            ) from exc
        if problems or "SKILL.md" not in files:
            raise GraphCliError("internal.error", f"the bundled skill {name} is incomplete")
        found.append(
            rendered(
                name,
                "admin",
                files,
                program=program_block(),
                source={"bundled": "graph_cli_admin"},
            )
        )
    return found


def admin_compat() -> str:
    """graph-admin's compatibility id (B14.16): its inventory with answer schemas and the
    admin skills it installs."""
    from graph_cli.catalog.compat import compat_id
    from graph_cli_admin.maintenance_cli.commands import all_specs
    from graph_cli_admin.maintenance_cli.commands.meta import inventory

    guidance = {skill.name: skill.files for skill in admin_skills()}
    return compat_id(inventory(all_specs(), output_schemas=True), guidance)
