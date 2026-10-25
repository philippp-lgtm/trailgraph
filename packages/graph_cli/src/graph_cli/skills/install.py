"""Installing skills into a skill directory and checking them (D15 Q3-Q5, Q8).

A target is an agent configuration directory (``<dir>/.claude`` or the
user's); skills live in its ``skills/``. ``scan`` reads what is there for
one owner: ``graph`` owns skill 1 and the graph skills, ``graph-admin`` the
admin skills (marker kind ``admin``). An owner sees the directories whose
marker names one of its kinds, and those with its names that carry no
readable marker (foreign or broken); anything else is not its own and
ignored, so neither program reports or replaces the other's skills.

``plan_install`` decides per skill (create, replace, unchanged, refused)
before anything is written; ``apply_install`` then replaces each skill
directory that is not refused on its own, so one hand-edited or foreign
directory holds back only itself (F63): the new tree is written into a staging directory
next to ``skills/`` (never inside it, so an agent never loads a half
copy), the old directory is renamed into staging, the new one into place,
and the old one is deleted. A crash leaves every skill old or new, never
mixed; the next install removes leftovers. Installs into one target are not
locked against each other. ``check`` compares what is installed with what
this program renders now.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import EMPTY, GraphCliError
from graph_cli.kernel.skill_marker import APP_SKILL, GRAPH_SKILL_PREFIX, MARKER_FILE, SKILLS_DIR
from graph_cli.skills.marker import Marker, SkillKind, decode_marker, encode_marker, file_hashes
from graph_cli.skills.tree import (
    LEGACY_MARKER_FILE,
    MARKER_FILES,
    SkillFiles,
    read_tree,
    write_tree,
)

STAGING_DIR: Final = ".graph_cli_staging"

Scope = Literal["user", "project"]
Integrity = Literal["intact", "legacy", "modified", "unexpected", "foreign"]
Action = Literal["create", "replace", "unchanged", "refused"]
Status = Literal["fresh", "stale", "modified", "unexpected", "foreign", "missing", "unavailable"]
STATUSES: Final[tuple[Status, ...]] = (
    "fresh",
    "stale",
    "modified",
    "unexpected",
    "foreign",
    "missing",
    "unavailable",
)


@dataclass(frozen=True, slots=True)
class SkillTarget:
    """An agent configuration directory chosen on the command line."""

    scope: Scope
    config_dir: Path  # <dir>/.claude, or the user configuration directory
    dir: Path | None = None  # the project directory (scope project)

    @property
    def skills(self) -> Path:
        return self.config_dir / SKILLS_DIR

    @property
    def staging(self) -> Path:
        return self.config_dir / STAGING_DIR

    def details(self) -> dict[str, JsonValue]:
        """What a hint needs to name this target again."""
        found: dict[str, JsonValue] = {"scope": self.scope}
        if self.dir is not None:
            found["dir"] = str(self.dir)
        return found

    def to_json(self) -> dict[str, JsonValue]:
        return {**self.details(), "path": str(self.skills)}


@dataclass(frozen=True, slots=True)
class RenderedSkill:
    """A skill as this program would install it now."""

    name: str
    kind: SkillKind
    files: SkillFiles
    marker: Marker

    @property
    def marker_bytes(self) -> bytes:
        return encode_marker(self.marker)


def rendered(
    name: str,
    kind: SkillKind,
    files: SkillFiles,
    *,
    program: Mapping[str, JsonValue],
    source: Mapping[str, JsonValue],
) -> RenderedSkill:
    marker = Marker(name, kind, program, source, file_hashes(files))
    return RenderedSkill(name, kind, dict(files), marker)


@dataclass(frozen=True, slots=True)
class Installed:
    """A skill directory found in the target."""

    name: str
    is_dir: bool
    marker: Marker | None = None
    marker_data: bytes | None = None  # raw bytes of MARKER_FILE when present
    legacy: bool = False  # LEGACY_MARKER_FILE present
    files: Mapping[str, bytes] = field(default_factory=dict)  # without marker files
    irregular: tuple[str, ...] = ()  # symlinks and other non-regular entries

    @property
    def integrity(self) -> Integrity:
        if not self.is_dir or (self.marker_data is None and not self.legacy):
            return "foreign"
        if self.marker_data is None:
            return "legacy"
        if self.marker is None or self.marker.skill != self.name:
            return "modified"
        hashes = file_hashes(self.files)
        if any(hashes.get(name) != digest for name, digest in self.marker.files.items()):
            return "modified"
        if set(self.files) - set(self.marker.files) or self.irregular:
            return "unexpected"
        return "intact"

    @property
    def kind(self) -> SkillKind | None:
        if self.marker is not None:
            return self.marker.kind
        if self.name == APP_SKILL:
            return "app"
        return "graph" if self.name.startswith(GRAPH_SKILL_PREFIX) else None


@dataclass(frozen=True, slots=True)
class Owner:
    """The skills one program installs: the marker kinds it writes, and the names that
    are its own even without a marker (a foreign directory there would clash)."""

    kinds: Collection[SkillKind]
    clashes: Callable[[str], bool]


def _clashes(name: str) -> bool:
    return name == APP_SKILL or name.startswith(GRAPH_SKILL_PREFIX)


APP_OWNER: Final = Owner(("app", "graph"), _clashes)


def require_target(target: SkillTarget) -> None:
    """``skills.target_invalid`` unless the configuration directory exists (D15 Q3)."""
    details = {**target.details(), "path": str(target.config_dir)}
    if not target.config_dir.is_dir():
        raise GraphCliError(
            "skills.target_invalid",
            f"{target.config_dir} does not exist; create it first"
            + (" (or start the agent once)" if target.scope == "user" else ""),
            details={**details, "cause": "missing"},
        )
    if target.skills.exists() and not target.skills.is_dir():
        raise GraphCliError(
            "skills.target_invalid",
            f"{target.skills} is not a directory",
            details={**details, "path": str(target.skills), "cause": "not_a_directory"},
        )


def scan(target: SkillTarget, owner: Owner = APP_OWNER) -> dict[str, Installed]:
    """The owner's skill directories and the foreign ones that would clash with its
    names, by name."""
    found: dict[str, Installed] = {}
    try:
        names = sorted(os.listdir(target.skills)) if target.skills.is_dir() else []
        for name in names:
            path = target.skills / name
            if path.is_symlink() or not path.is_dir():
                if owner.clashes(name):
                    found[name] = Installed(name, is_dir=False)
                continue
            marker_data = _read(path / MARKER_FILE)
            marker = decode_marker(marker_data) if marker_data is not None else None
            legacy = (path / LEGACY_MARKER_FILE).is_file()
            if marker is not None and marker.kind not in owner.kinds:
                continue  # another program's skill
            if marker is None and not owner.clashes(name):
                continue  # no readable marker and none of the owner's names
            files, problems = read_tree(path)
            found[name] = Installed(
                name,
                is_dir=True,
                marker=marker,
                marker_data=marker_data,
                legacy=legacy,
                files={k: v for k, v in files.items() if k not in MARKER_FILES},
                irregular=tuple(p.path for p in problems),
            )
    except OSError as exc:
        raise _io(target, "cannot read the skill directory", exc) from exc
    return found


# ---- install ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    skill: RenderedSkill
    action: Action
    installed: Integrity | None  # None when the directory does not exist yet

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "skill": self.skill.name,
            "kind": self.skill.kind,
            "action": self.action,
            "installed": self.installed,
        }


@dataclass(frozen=True, slots=True)
class InstallPlan:
    target: SkillTarget
    steps: tuple[Step, ...]

    @property
    def refused(self) -> tuple[Step, ...]:
        return tuple(s for s in self.steps if s.action == "refused")


def plan_install(
    target: SkillTarget,
    installed: Mapping[str, Installed],
    desired: Sequence[RenderedSkill],
    *,
    force: bool,
) -> InstallPlan:
    """One step per desired skill; nothing is written."""
    steps = []
    for skill in desired:
        found = installed.get(skill.name)
        integrity = found.integrity if found is not None else None
        action: Action
        if found is None:
            action = "create"
        elif integrity in ("foreign", "modified", "unexpected") and not force:
            action = "refused"
        elif (
            integrity == "intact"
            and dict(found.files) == dict(skill.files)
            and found.marker_data == skill.marker_bytes
        ):
            action = "unchanged"
        else:
            action = "replace"
        steps.append(Step(skill, action, integrity))
    return InstallPlan(target, tuple(steps))


def refusal(plan: InstallPlan, details: Mapping[str, JsonValue] = EMPTY) -> GraphCliError | None:
    """``skills.not_owned`` (a foreign directory) or ``skills.modified`` (a hand-edited one),
    the error of a partial install: the refused directories stay as they are."""
    refused = plan.refused
    if not refused:
        return None
    foreign = [s.skill.name for s in refused if s.installed == "foreign"]
    edited = [s.skill.name for s in refused if s.installed != "foreign"]
    data: dict[str, JsonValue] = {
        **plan.target.details(),
        **details,
        "refused": [{"skill": s.skill.name, "installed": s.installed} for s in refused],
    }
    if foreign:
        return GraphCliError(
            "skills.not_owned",
            f"{', '.join(foreign)} in {plan.target.skills} was not installed by this program"
            " and was left as it is; pass --force to replace it",
            details=data,
        )
    return GraphCliError(
        "skills.modified",
        f"{', '.join(edited)} in {plan.target.skills} was edited by hand and was left as it"
        " is; pass --force to discard the edits",
        details=data,
    )


def apply_install(plan: InstallPlan) -> None:
    """Replace every skill directory of the plan that changes, one atomic swap each;
    refused steps are left as they are."""
    target = plan.target
    try:
        clean_staging(target)
        target.skills.mkdir(exist_ok=True)
        for step in plan.steps:
            if step.action in ("create", "replace"):
                _swap(target, step.skill)
        if target.staging.is_dir() and not any(target.staging.iterdir()):
            target.staging.rmdir()
    except OSError as exc:
        raise _io(target, "cannot install the skills", exc) from exc


def clean_staging(target: SkillTarget) -> None:
    """Remove what an interrupted install left in the staging directory."""
    if target.staging.is_dir() and not target.staging.is_symlink():
        shutil.rmtree(target.staging)


def _swap(target: SkillTarget, skill: RenderedSkill) -> None:
    token = secrets.token_hex(4)
    target.staging.mkdir(exist_ok=True)
    new = target.staging / f"{skill.name}-{token}"
    write_tree(new, skill.files)
    (new / MARKER_FILE).write_bytes(skill.marker_bytes)
    dest = target.skills / skill.name
    old: Path | None = None
    if dest.exists() or dest.is_symlink():
        old = target.staging / f"{skill.name}-{token}.old"
        os.rename(dest, old)
    os.rename(new, dest)
    if old is not None:
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        else:
            old.unlink()


# ---- emit --------------------------------------------------------------------


def emit_skills(
    out: Path,
    skills: Sequence[RenderedSkill],
    *,
    force: bool,
    details: Mapping[str, JsonValue] = EMPTY,
) -> list[Step]:
    """Write ``<out>/<skill>/`` without a marker (plugin layout); an existing non-empty
    directory is replaced only with ``force`` (``skills.not_owned``)."""
    steps = []
    for skill in skills:
        dest = out / skill.name
        exists = dest.is_symlink() or dest.exists()
        occupied = exists and (not dest.is_dir() or any(dest.iterdir()))
        action: Action = "refused" if occupied and not force else "replace" if exists else "create"
        steps.append(Step(skill, action, "foreign" if occupied else None))
    refused = [s.skill.name for s in steps if s.action == "refused"]
    if refused:
        raise GraphCliError(
            "skills.not_owned",
            f"{', '.join(refused)} exists in {out}; pass --force to replace it",
            details={**details, "refused": [{"skill": n, "installed": "foreign"} for n in refused]},
        )
    try:
        for step in steps:
            dest = out / step.skill.name
            if step.action == "replace":
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            write_tree(dest, step.skill.files)
    except OSError as exc:
        raise GraphCliError(
            "skills.target_invalid",
            f"cannot write the skills: {exc.strerror or exc}",
            details={**details, "path": str(out), "cause": "io"},
        ) from exc
    return steps


# ---- check -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillStatus:
    name: str
    kind: SkillKind | None
    status: Status
    details: Mapping[str, JsonValue] = field(default=EMPTY)

    def to_json(self) -> dict[str, JsonValue]:
        return {"skill": self.name, "kind": self.kind, "status": self.status, **self.details}


def check(
    installed: Mapping[str, Installed],
    desired: Sequence[RenderedSkill],
    *,
    unavailable: Mapping[str, Mapping[str, JsonValue]] | None = None,
) -> list[SkillStatus]:
    """Every desired or found skill with its status; skill 1 first, then by name."""
    wanted = {skill.name: skill for skill in desired}
    unavailable = unavailable or {}
    statuses = []
    for name in sorted({*wanted, *installed, *unavailable}, key=lambda n: (n != APP_SKILL, n)):
        found = installed.get(name)
        skill = wanted.get(name)
        kind = skill.kind if skill is not None else found.kind if found is not None else None
        if name in unavailable:
            statuses.append(SkillStatus(name, kind, "unavailable", unavailable[name]))
        elif found is None:
            statuses.append(SkillStatus(name, kind, "missing"))
        elif found.integrity in ("foreign", "modified", "unexpected"):
            statuses.append(SkillStatus(name, kind, _as_status(found.integrity)))
        elif skill is None:
            statuses.append(SkillStatus(name, kind, "unavailable", {"reason": "unknown"}))
        elif found.integrity == "legacy" or dict(found.files) != dict(skill.files):
            statuses.append(SkillStatus(name, kind, "stale"))
        else:
            statuses.append(SkillStatus(name, kind, "fresh"))
    return statuses


def _as_status(integrity: Integrity) -> Status:
    if integrity == "foreign":
        return "foreign"
    return "modified" if integrity == "modified" else "unexpected"


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except FileNotFoundError:
        return None


def _io(target: SkillTarget, message: str, exc: OSError) -> GraphCliError:
    return GraphCliError(
        "skills.target_invalid",
        f"{message}: {exc.strerror or exc}",
        details={**target.details(), "path": str(target.skills), "cause": "io"},
    )
