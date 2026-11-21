"""Skill freshness: installed skills claim what they were rendered from (contracts B14.17).

An agent works from the skills installed for it. Each copy's marker
(``.graph_cli_skill.json``) says what it was rendered from: skill 1 the
inventory hash of the program that rendered it, a graph skill the hash of
the graph's published slot and, for a template, the ontology release of its
model and, for a reader with type read limits, a digest of the types it sees (the template is
rendered per reader, so a policy change that moves the set makes the copy stale) and, for a
template, a digest of what its render shows of the published catalogs (``featured``: the featured
function and action lines and whether the graph has functions, actions and views; a publish that
changes neither leaves the copy fresh, one that does makes it stale; a copy installed before the
digest was recorded has none and is not judged on it).
``read_claims`` reads them from the project scope (``<pointer dir>/.claude/skills``) and the
user scope; both count, because which copy an agent loads depends on the agent. Without an
installed skill nothing is claimed and nothing is checked.

``SkillGate`` judges the graph claims of one command when it selects a graph
(``AppContext.select``): a copy rendered from another slot or model than the
graph has now refuses a write (``skills.outdated``) and warns on a read
(``skills.stale``). In the remote mode the client sends its graph claims in
``Graph-Cli-Skills`` and the server judges them; skill 1 is judged by the
program that runs the command (``app_cli.skill_check``), against the
inventory ``graph skills install`` renders it from. Not access control: a
client may send no claims.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from graph_cli.kernel.canonical import HASH_RE, JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry, SkillSlot
from graph_cli.kernel.skill_marker import (
    APP_SKILL,
    GENERATED,
    GRAPH_SKILL_PREFIX,
    MARKER_FILE,
    MARKER_SCHEMA,
    graph_skill_name,
)

SKILLS_HEADER: Final = "Graph-Cli-Skills"
MAX_CLAIMS: Final = 32  # graph claims of one request
MAX_MARKER_BYTES: Final = 1024 * 1024
TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")  # graph ids, bundles, versions
Scope = Literal["project", "user"]
SCOPES: Final[tuple[Scope, ...]] = ("project", "user")


@dataclass(frozen=True, slots=True)
class SkillClaim:
    """What one installed copy was rendered from."""

    scope: Scope
    hash: str  # skill 1: the inventory hash; a graph skill: the slot hash
    graph: str | None = None  # the graph id of a graph skill; None for skill 1
    model: tuple[str, str] | None = None  # (bundle, version) a template was rendered with
    visible: str | None = None  # ``ReadView.digest`` of a limited reader; else None
    featured: str | None = None  # ``kernel.skill_featured`` digest; None: not recorded


def read_claims(dirs: Sequence[tuple[Scope, Path]]) -> tuple[SkillClaim, ...]:
    """The claims of the skills in each ``(scope, skills directory)``; a directory seen
    twice (the project is the home directory) counts once, an unreadable marker not at
    all (``graph skills check`` reports it)."""
    found: list[SkillClaim] = []
    seen: set[Path] = set()
    for scope, directory in dirs:
        try:
            resolved = directory.resolve()
            names = sorted(os.listdir(directory)) if directory.is_dir() else []
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        for name in names:
            if name == APP_SKILL or name.startswith(GRAPH_SKILL_PREFIX):
                claim = _claim(scope, directory / name / MARKER_FILE)
                if claim is not None and claim not in found:
                    found.append(claim)
    return tuple(found)


def _claim(scope: Scope, path: Path) -> SkillClaim | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MARKER_BYTES:
            return None
        doc = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != MARKER_SCHEMA:
        return None
    source = doc.get("source")
    if not isinstance(source, dict):
        return None
    if doc.get("kind") == "app":
        digest = source.get("inventory_hash")
        return SkillClaim(scope, str(digest)) if _is_hash(digest) else None
    graph, digest, model = source.get("graph"), source.get("hash"), source.get("model")
    graph_id = graph.get("id") if isinstance(graph, dict) else None
    if doc.get("kind") != "graph" or not _is_token(graph_id) or not _is_hash(digest):
        return None
    seen = source.get("visible")
    if seen is not None and not _is_hash(seen):
        return None
    visible = None if seen is None else str(seen)
    shown = source.get("featured")
    if shown is not None and not _is_hash(shown):
        return None
    featured = None if shown is None else str(shown)
    if model is None:
        return SkillClaim(scope, str(digest), str(graph_id), None, visible, featured)
    pair = _model(model)
    return SkillClaim(scope, str(digest), str(graph_id), pair, visible, featured) if pair else None


def _model(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    bundle, version = value.get("bundle"), value.get("version")
    if _is_token(bundle) and _is_token(version):
        return str(bundle), str(version)
    return None


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.match(value) is not None


def _is_token(value: object) -> bool:
    return isinstance(value, str) and TOKEN_RE.match(value) is not None


# ---- the request header ----------------------------------------------------------


def claims_header(claims: Sequence[SkillClaim]) -> str | None:
    """``Graph-Cli-Skills``: one item per graph claim, ``graph=<id>;hash=<hash>``, for a
    template ``;bundle=<bundle>;version=<version>``, for a limited reader
    ``;visible=<digest>``, for a copy that recorded it ``;featured=<digest>``, then
    ``;scope=<scope>``; items separated by ``, ``. ``None``
    without a graph claim."""
    items = []
    for claim in claims:
        if claim.graph is None:
            continue
        parts = [f"graph={claim.graph}", f"hash={claim.hash}"]
        if claim.model is not None:
            parts += [f"bundle={claim.model[0]}", f"version={claim.model[1]}"]
        if claim.visible is not None:
            parts.append(f"visible={claim.visible}")
        if claim.featured is not None:
            parts.append(f"featured={claim.featured}")
        items.append(";".join([*parts, f"scope={claim.scope}"]))
    return ", ".join(items[:MAX_CLAIMS]) or None


def parse_claims_header(value: str | None) -> tuple[SkillClaim, ...]:
    """The graph claims of a ``Graph-Cli-Skills`` value; a malformed item is left out."""
    found: list[SkillClaim] = []
    for item in (value or "").split(",")[:MAX_CLAIMS]:
        params: dict[str, str] = {}
        for part in item.strip().split(";"):
            key, _, text = part.strip().partition("=")
            params.setdefault(key, text)
        graph, digest, scope = params.get("graph"), params.get("hash"), params.get("scope")
        if not _is_token(graph) or not _is_hash(digest) or scope not in SCOPES:
            continue
        model = None
        if "bundle" in params or "version" in params:
            model = _model({"bundle": params.get("bundle"), "version": params.get("version")})
            if model is None:
                continue
        seen = params.get("visible")
        if seen is not None and not _is_hash(seen):
            continue
        shown = params.get("featured")
        if shown is not None and not _is_hash(shown):
            continue
        claim = SkillClaim(
            "project" if scope == "project" else "user", str(digest), graph, model, seen, shown
        )
        if claim not in found:
            found.append(claim)
    return tuple(found)


# ---- judging ---------------------------------------------------------------------


@dataclass
class SkillGate:
    """The freshness check of one command that runs on a graph."""

    claims: Callable[[], Sequence[SkillClaim]]  # read on the first check
    write: bool  # the command is a write: a stale copy refuses it
    warnings: list[WarningRecord]  # the command's warnings
    project_dir: Path | None  # where the project scope is, for the refresh command
    # The digest of what the caller sees in a graph (``ReadView.digest``), ``None`` without a
    # read limit
    visible: Callable[[GraphEntry], str | None] = lambda entry: None
    # The digest of what a render shows of the graph's published catalogs
    # (``Catalog.skill_featured``); ``None`` when the catalogs cannot be read
    featured: Callable[[GraphEntry], str | None] = lambda entry: None

    def check(self, catalog: Catalog, entry: GraphEntry) -> None:
        claims = [c for c in self.claims() if c.graph == str(entry.id)]
        if not claims:
            return
        try:
            slot = catalog.read_skill(entry.id)
        except GraphCliError:
            return  # a damaged slot is for the maintainer; installing reports it
        if slot is None:
            return  # unpublished: an installed copy is unavailable, not stale
        # Only a template is rendered per reader; a static skill is the same for everyone
        seen = self.visible(entry) if slot.kind == GENERATED else None
        shown = _featured_of(self, entry) if slot.kind == GENERATED else None
        stale = [s for c in claims if (s := graph_stale(c, entry, slot, seen, shown)) is not None]
        report(stale, write=self.write, warnings=self.warnings, project_dir=self.project_dir)


def _featured_of(gate: SkillGate, entry: GraphEntry) -> str | None:
    try:
        return gate.featured(entry)
    except GraphCliError:
        return None  # a damaged catalog slot is for the maintainer; the freshness check goes on


def graph_stale(
    claim: SkillClaim,
    entry: GraphEntry,
    slot: SkillSlot,
    visible: str | None = None,
    featured: str | None = None,
) -> dict[str, JsonValue] | None:
    """The stale entry of a graph claim; ``None`` when the copy is what the graph has now.
    ``visible`` is the digest of what the caller sees (``ReadView.digest``; ``None``: no read
    limit); a copy rendered for another view is stale. ``featured`` is the digest of what a render
    shows of the catalogs now (``None``: unknown); a copy that recorded another one is stale, a
    copy that recorded none is not judged on it."""
    model = (entry.pin.bundle, entry.pin.version) if slot.kind == GENERATED else None
    moved = claim.featured is not None and featured is not None and claim.featured != featured
    if claim.hash == slot.hash and claim.model == model and claim.visible == visible and not moved:
        return None
    installed: dict[str, JsonValue] = {"hash": claim.hash, "model": _model_json(claim.model)}
    current: dict[str, JsonValue] = {"hash": slot.hash, "model": _model_json(model)}
    if claim.visible is not None or visible is not None:  # only for limited readers
        installed["visible"] = claim.visible
        current["visible"] = visible
    if moved:  # only when the catalogs are (one of) the reasons
        installed["featured"] = claim.featured
        current["featured"] = featured
    return {
        "skill": graph_skill_name(entry.name),
        "kind": "graph",
        "scope": claim.scope,
        "graph": entry.name,
        "installed": installed,
        "current": current,
    }


def app_stale(claims: Sequence[SkillClaim], current: str) -> list[dict[str, JsonValue]]:
    """The stale entries of skill 1: copies rendered from another inventory than
    ``current``, the inventory hash of the program that runs the command."""
    return [
        {
            "skill": APP_SKILL,
            "kind": "app",
            "scope": claim.scope,
            "installed": {"inventory_hash": claim.hash},
            "current": {"inventory_hash": current},
        }
        for claim in claims
        if claim.graph is None and claim.hash != current
    ]


def report(
    stale: Sequence[Mapping[str, JsonValue]],
    *,
    write: bool,
    warnings: list[WarningRecord],
    project_dir: Path | None,
) -> None:
    """``skills.outdated`` for a write, else the warning ``skills.stale`` (once)."""
    if not stale:
        return
    scope = str(stale[0]["scope"])
    details: dict[str, JsonValue] = {"skills": [dict(s) for s in stale], "scope": scope}
    words = ["graph", "self", "update"]
    if scope == "project" and project_dir is not None:
        details["dir"] = str(project_dir)
        words += ["--dir", shlex.quote(str(project_dir))]
    names = ", ".join(f"{s['skill']} ({s['scope']} scope)" for s in stale)
    if write:
        raise GraphCliError(
            "skills.outdated",
            f"the installed skill {names} describes an older state than the one this write"
            " would run against; the write is refused so it is not made from outdated"
            " guidance. Refresh the skills, read them again, then repeat the write",
            details=details,
        )
    warning = WarningRecord(
        "skills.stale",
        f"the installed skill {names} describes an older state; the answer is right, but"
        f" the skill may mislead. Refresh the skills ({' '.join(words)}) and read them again",
        details,
    )
    if warning not in warnings:
        warnings.append(warning)


def _model_json(model: tuple[str, str] | None) -> JsonValue:
    return None if model is None else {"bundle": model[0], "version": model[1]}
