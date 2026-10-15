"""``graph-admin graph skill publish``: the skill ``graph skills install`` installs (D15 Q2).

The maintainer's hand-written skill directory is validated
(``graph_cli.skills.tree``: name ``graph-<graph>``, regular UTF-8 files,
size budget, no maintenance vocabulary or local paths) and published into
the graph's runtime slot as kind ``static``, together with the graph's
ontology pin at publish time. One maintenance operation in the W.e shape,
authorized as ``maint.graph_configure``: intent in the graph stream with
``change.skill{from, to}``, the slot switch (the commit point), then an
installation event anchoring the intent.

Refused before any event: an archived graph (``graph.archived``) and an
invalid directory (``admin.skill_invalid``, every broken rule in
``details.problems``). Publishing the same files against the same pin
again changes nothing. ``--dry-run`` stops after the checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot, skill_hash
from graph_cli.kernel.records import RequestMeta
from graph_cli.skills.tree import Problem, graph_skill_name, read_tree, validate
from graph_cli_admin.services_admin.context import AdminContext

STATIC = "static"
DAMAGED = frozenset({"skills.source_invalid", "format.unsupported_version"})


@dataclass(frozen=True, slots=True)
class Published:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


def publish(
    ctx: AdminContext, *, graph_ref: str, source: Path, reason: str | None, dry_run: bool
) -> Published:
    entry = ctx.catalog.resolve(graph_ref)
    _check_state(entry)
    name = graph_skill_name(entry.name)
    files = _checked_files(source, name, entry)
    digest = skill_hash(files)
    meta = RequestMeta.of(
        "graph skill publish", {"graph": str(entry.id), "hash": digest, "dry_run": dry_run}
    )
    decisions = ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=meta,
        graph=entry.id,
        reason=reason,
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        _check_state(entry)
        try:
            current = session.read_skill()
        except GraphCliError as exc:
            if exc.code not in DAMAGED:
                raise
            current = None  # a damaged slot is replaced like a missing one
        slot = SkillSlot(STATIC, digest, entry.pin, files)
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "skill": name,
            "from": current.summary() if current is not None else None,
            "to": slot.summary(),
            "files": sorted(files),
            "published": False,
            "dry_run": dry_run,
        }
        seq = session.head().seq
        unchanged = current is not None and current.summary() == slot.summary()
        if unchanged or dry_run:
            return Published(entry, data, seq)
        change = events.skill_change(current.hash if current is not None else None, digest)
        receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
        session.replace_skill(slot, receipt)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["published"] = True
        return Published(entry, data, seq)


def _checked_files(source: Path, name: str, entry: GraphEntry) -> dict[str, bytes]:
    try:
        files, problems = read_tree(source)
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot read {source}: {exc.strerror or exc}",
            details={"argument": "dir"},
        ) from exc
    problems += validate(files, name=name, directory=source.name)
    if problems:
        raise GraphCliError(
            "admin.skill_invalid",
            f"{source} is not a valid skill for graph {entry.name}: {_summary(problems)}",
            details={"graph": entry.name, "skill": name, "problems": _json(problems)},
        )
    return files


def _summary(problems: list[Problem]) -> str:
    shown = "; ".join(f"{p.path or '.'}: {p.message}" for p in problems[:5])
    more = len(problems) - 5
    return f"{shown}; and {more} more" if more > 0 else shown


def _json(problems: list[Problem]) -> list[JsonValue]:
    return [p.to_json() for p in problems]


def _check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
