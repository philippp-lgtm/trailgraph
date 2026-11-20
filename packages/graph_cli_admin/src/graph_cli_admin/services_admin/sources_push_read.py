"""Reading a maintainer workspace for ``graph push``, before the lock.

``read_workspace`` reads what a push takes from the workspace directory: the
ontology (the highest version directory, compiled) with its migration file,
the skill tree and the ``functions``, ``actions`` and ``views`` folders. Each
is hashed and compared with the base file, and the ontology is judged a new
release or not (``admin.release_exists`` for edited modules under the base
version). The result is one ``Workspace``.

Nothing here takes the lock or reads a published slot: what the graph holds
now is read and compared by ``sources_push`` under the lock. The layout of the
workspace and the base file are in ``sources``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry, SlotKind, skill_hash, sources_hash
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.kinetic.params import Problem as KineticProblem
from graph_cli.ontology_layer.semver import Version, is_version
from graph_cli.skills.tree import Problem, read_tree
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli_admin.services_admin import kinetic_slots
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import Compiled, compile_source, read_migration
from graph_cli_admin.services_admin.sources import (
    ACTIONS,
    FUNCTIONS,
    ONTOLOGY,
    SKILL,
    VIEWS,
    Base,
    release_sources,
    usage_error,
)

EMPTY: Final = sources_hash({})  # a missing slot and an empty one are the same


@dataclass(frozen=True, slots=True)
class Folder:
    """The ``functions/``, ``actions/`` or ``views/`` folder of a workspace."""

    directory: Path
    files: dict[str, bytes]
    problems: list[KineticProblem]
    hash: str  # of the files; the hash of no files for a missing folder
    changed: bool  # the hash differs from the base's


@dataclass(frozen=True, slots=True)
class SkillTree:
    """The skill tree of a workspace; ``files`` is ``None`` without a skill directory."""

    name: str  # the skill's name for this graph (and the name of its directory)
    directory: Path | None
    files: dict[str, bytes] | None
    problems: list[Problem]
    hash: str | None
    changed: bool


@dataclass(frozen=True, slots=True)
class Workspace:
    """Everything a push reads of the workspace before it takes the lock."""

    source: Path
    base: Base
    compiled: Compiled
    migration: bytes | None  # the raw ``migration.yaml`` of the ontology directory
    skill: SkillTree
    functions: Folder
    actions: Folder
    views: Folder
    ontology_changed: bool


def read_workspace(ctx: AdminContext, entry: GraphEntry, source: Path, base: Base) -> Workspace:
    """Read everything a push takes from the workspace, before the lock. The order decides
    which usage error wins when several parts are broken: the ontology (and its migration
    file), the skill, the function, action and view folders, then whether the ontology is a
    new release (``admin.release_exists``)."""
    compiled, ontology_dir = _ontology(source, base)
    migration = read_migration(ontology_dir)
    skill_tree = _read_skill_tree(source, entry, base)
    function_folder = _read_folder(source, FUNCTIONS, "functions", base.functions)
    action_folder = _read_folder(source, ACTIONS, "actions", base.actions)
    view_folder = _read_folder(source, VIEWS, "views", base.views)
    ontology_changed = _ontology_changed(ctx, entry, base, compiled, source / ONTOLOGY)
    return Workspace(
        source=source,
        base=base,
        compiled=compiled,
        migration=migration,
        skill=skill_tree,
        functions=function_folder,
        actions=action_folder,
        views=view_folder,
        ontology_changed=ontology_changed,
    )


def _read_skill_tree(source: Path, entry: GraphEntry, base: Base) -> SkillTree:
    """The skill tree of the workspace, hashed and compared with the base's skill."""
    name = graph_skill_name(entry.name)
    directory = _skill_dir(source, name)
    files: dict[str, bytes] | None = None
    problems: list[Problem] = []
    if directory is not None:
        try:
            files, problems = read_tree(directory)
        except OSError as exc:
            raise usage_error(f"cannot read {directory}: {exc.strerror or exc}") from exc
    pushed = skill_hash(files) if files is not None else None
    return SkillTree(
        name, directory, files, problems, pushed, pushed is not None and pushed != base.skill
    )


def _read_folder(source: Path, part: str, kind: SlotKind, base_hash: str | None) -> Folder:
    """``functions/``, ``actions/`` or ``views/`` of the workspace, hashed and compared with the
    base's hash (a missing folder and a graph without that catalog are both the empty one)."""
    directory = source / part
    files, problems = _kinetic_files(directory, kind)
    pushed = sources_hash(files)
    return Folder(directory, files, problems, pushed, pushed != (base_hash or EMPTY))


def _ontology(source: Path, base: Base) -> tuple[Compiled, Path]:
    """The highest version directory of ``ontology/``, compiled with the base's main
    module, and the directory; its name must be the version its main module declares."""
    directory = source / ONTOLOGY
    try:
        versions = sorted(
            (Version.parse(p.name) for p in directory.iterdir() if is_version(p.name)),
            reverse=True,
        )
    except OSError as exc:
        raise usage_error(f"cannot read {directory}: {exc.strerror or exc}") from exc
    if not versions:
        raise usage_error(f"{directory} holds no version directory (<major>.<minor>.<patch>)")
    latest = directory / str(versions[0])
    main = base.pin.bundle
    compiled = compile_source(latest, main if (latest / f"{main}.yaml").is_file() else None)
    declared = compiled.model.bundle_version
    if declared != latest.name:
        raise usage_error(
            f"{latest} declares version {declared}; name the directory after the version"
            " (bump the module version in a new directory)"
        )
    return compiled, latest


def _kinetic_files(
    directory: Path, kind: SlotKind
) -> tuple[dict[str, bytes], list[KineticProblem]]:
    """The files of ``functions/`` or ``actions/``; a missing folder is an empty one."""
    if not directory.is_dir():
        return {}, []
    return kinetic_slots.read_dir(directory, kind)


def _skill_dir(source: Path, name: str) -> Path | None:
    """``skill/<name>/`` when it exists; any other entry of ``skill/`` is a usage error
    (a renamed graph, a typo), except hidden files such as ``.DS_Store``."""
    directory = source / SKILL
    try:
        found = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise usage_error(f"cannot read {directory}: {exc.strerror or exc}") from exc
    other = [n for n in found if n != name]
    if other:
        raise usage_error(
            f"{directory} holds {', '.join(other)}; the skill of this graph is {name}"
        )
    return directory / name if found else None


def _ontology_changed(
    ctx: AdminContext, entry: GraphEntry, base: Base, compiled: Compiled, directory: Path
) -> bool:
    """Whether the workspace's ontology is a new release. The base version is compared by
    its authored modules (the compiled hash may change with the compiler while they do
    not); edited modules under the base version are ``admin.release_exists``: a release
    never changes, so even an edited comment needs a new version."""
    if compiled.pin.version != base.pin.version:
        return True
    installed = release_hashes(GraphLayout(ctx.catalog.graphs_dir / str(entry.id)))
    if base.pin.version not in installed:
        return True  # cannot happen after a pull; the release decides
    _, modules = release_sources(ctx, entry, base.pin.version, installed[base.pin.version])
    if dict(compiled.sources) == modules:
        return False
    raise GraphCliError(
        "admin.release_exists",
        f"{directory} differs from release {base.pin.version}; copy it to a new version"
        " directory and bump the module version there",
        details={
            "version": base.pin.version,
            "expected": installed[base.pin.version],
            "actual": compiled.pin.artifact_hash,
        },
    )
