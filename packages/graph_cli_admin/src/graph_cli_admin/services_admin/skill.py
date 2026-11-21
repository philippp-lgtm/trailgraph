"""``graph-admin graph skill publish|render``: the skill ``graph skills install`` installs.

The maintainer's skill directory is validated (``graph_cli.skills.tree``:
name ``graph-<graph>``, regular UTF-8 files, size budget, no maintenance
vocabulary or local paths) and published into the graph's runtime slot
(D15 Q2), together with the graph's ontology pin at publish time. Its kind
follows the content: a ``SKILL.md`` with block markers is a template
(``generated``), rendered by the app with the live model at every install;
without markers the files are the skill (``static``). A template is
rendered once against the pin, exactly as the app does, and its curated
text is checked against the model and the app inventory
(``graph_cli.skills.names``), so a template that would install broken or
name unknown terms, commands or alias formats never reaches the slot.

Publishing is one maintenance operation in the W.e shape, authorized as
``maint.graph_configure``: intent in the graph stream with
``change.skill{from, to}``, the slot switch (the commit point), then an
installation event anchoring the intent. Refused before any event: an
archived graph (``graph.archived``) and an invalid directory
(``admin.skill_invalid``, every broken rule in ``details.problems``).
Publishing the same files against the same pin again changes nothing.
``--dry-run`` stops after the checks.

A template whose description cannot take the ``graph start`` sentence the renderer adds (too
long, or wrapped over lines as a plain or quoted scalar) is refused with the rule
``description`` (``graph_cli.skills.graph_skill.description``), so every published template
sends agents to ``graph start``. A template whose rendered ``SKILL.md`` exceeds the level 0
budget of its graph, or whose curated text exceeds its own budget
(``graph_cli.skills.graph_skill.measure``), gets the warning ``admin.skill_over_budget`` from
``render``, ``publish`` and ``graph push``, with the sizes and the largest blocks; it is not
refused.

``render`` writes what ``graph skills install`` would install (without the
marker) into a preview directory; read-only for the data root, like
``graph show``: no lock, no event, authorized ``maint.graph_configure``
(whoever may publish may preview).
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot, skill_hash
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.kernel.skill_marker import GENERATED, graph_skill_name
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.describe import describe_model
from graph_cli.skills.graph_skill import Size, graph_block, measure, render_checked
from graph_cli.skills.graph_skill.description import start_sentence_problem
from graph_cli.skills.names import check_names
from graph_cli.skills.template import kind_of, parse_template
from graph_cli.skills.tree import Problem, read_tree, validate, write_tree
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.release import check_state

DAMAGED = frozenset({"skills.source_invalid", "format.unsupported_version"})


@dataclass(frozen=True, slots=True)
class Published:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class Checked:
    """A source directory judged against a graph: its kind, the files the slot keeps, what
    ``graph skills install`` would write from them, and the problems of the curated text
    (``description``, ``term``, ``command``, ``alias``; a template only) and the size of its
    ``SKILL.md`` (a template only)."""

    kind: str
    files: dict[str, bytes]
    rendered: dict[str, bytes]
    problems: list[Problem] = field(default_factory=list)
    size: Size | None = None

    def warnings(self, entry: GraphEntry, name: str) -> tuple[WarningRecord, ...]:
        """``admin.skill_over_budget`` when the rendered skill is over a budget."""
        size = self.size
        if size is None or not size.over:
            return ()
        parts = []
        if size.skill > size.budget:
            parts.append(f"SKILL.md has {size.skill} bytes of {size.budget}")
        if size.curated > size.curated_budget:
            parts.append(f"its curated text {size.curated} bytes of {size.curated_budget}")
        return (
            WarningRecord(
                "admin.skill_over_budget",
                f"{name} is over budget: {'; '.join(parts)}; move workflow detail and recipes"
                " into references/, and split a growing model into modules",
                {"graph": entry.name, "skill": name, **size.to_json()},
            ),
        )


def check_source(
    ctx: AdminContext, entry: GraphEntry, source: Path, inventory: Mapping[str, JsonValue]
) -> Checked:
    """``admin.skill_invalid`` when the directory cannot become the graph's skill at all."""
    try:
        files, problems = read_tree(source)
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot read {source}: {exc.strerror or exc}",
            details={"argument": "dir"},
        ) from exc
    return check_files(
        ctx, entry, files, problems, label=source, directory=source.name, inventory=inventory
    )


def check_files(
    ctx: AdminContext,
    entry: GraphEntry,
    files: dict[str, bytes],
    problems: list[Problem],
    *,
    label: Path | str,
    directory: str | None,
    inventory: Mapping[str, JsonValue],
    model: CompiledModel | None = None,
    functions: JsonObject | None = None,
    actions: JsonObject | None = None,
    views: JsonObject | None = None,
) -> Checked:
    """``check_source`` for files read already; a template renders against ``model`` (the
    entry's pinned model when ``None``), the compiled function catalog ``functions`` and the
    compiled action catalog ``actions`` and the compiled view catalog ``views`` (the published
    ones when ``None``). Unlike the app,
    which lists only the actions a reader may run, the maintainer's view has every action, so
    the preview and ``check_names`` know every name."""
    name = graph_skill_name(entry.name)
    kind = kind_of(files)
    if kind != GENERATED:
        problems = [*problems, *validate(files, name=name, directory=directory)]
        refuse(label, entry, name, problems)
        return Checked(kind, files, files)
    if functions is None:
        slot = ctx.catalog.read_functions(entry.id)
        functions = slot.compiled if slot is not None and slot.count else None
    if actions is None:
        published = ctx.catalog.read_actions(entry.id)
        actions = published.compiled if published is not None and published.count else None
    if views is None:
        shown = ctx.catalog.read_views(entry.id)
        views = shown.compiled if shown is not None and shown.count else None
    described = describe_model(
        entry,
        model if model is not None else pinned_model(ctx.catalog, entry),
        functions,
        actions,
        views,
    )
    graph = graph_block(entry)
    rendered, render_problems = render_checked(
        files, described, graph, name=name, directory=directory
    )
    refuse(label, entry, name, [*problems, *render_problems])
    template, _ = parse_template(files)
    assert rendered is not None and template is not None  # render_checked found no problem
    # Refused at publish, never at install: the renderer leaves out the sentence it cannot add.
    start = start_sentence_problem(template.lines)
    return Checked(
        kind,
        files,
        rendered,
        [
            *([start] if start is not None else []),
            *check_names(template, described, graph, inventory),
        ],
        measure(template, described, graph),
    )


def current_slot(session: MaintenanceSession) -> SkillSlot | None:
    """The published skill; a damaged slot is replaced like a missing one."""
    try:
        return session.read_skill()
    except GraphCliError as exc:
        if exc.code not in DAMAGED:
            raise
        return None


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    current: SkillSlot | None,
    slot: SkillSlot,
    *,
    reason: str | None,
) -> None:
    """Switch the slot from ``current`` to ``slot`` in the W.e shape."""
    change = events.skill_change(current.hash if current is not None else None, slot.hash)
    receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
    session.replace_skill(slot, receipt)
    ctx.anchor(entry, decisions, meta, receipt, reason=reason)


def publish(
    ctx: AdminContext,
    *,
    graph_ref: str,
    source: Path,
    reason: str | None,
    dry_run: bool,
    inventory: Mapping[str, JsonValue],
) -> Published:
    entry = ctx.resolve(graph_ref, "graph skill publish")
    check_state(entry)
    name = graph_skill_name(entry.name)
    checked = check_source(ctx, entry, source, inventory)
    refuse(source, entry, name, checked.problems)
    checked_against = entry.pin
    files = checked.files
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
        check_state(entry)
        if checked.kind == GENERATED and entry.pin != checked_against:
            # A release came in between: the template must render against the new pin.
            refuse(source, entry, name, check_source(ctx, entry, source, inventory).problems)
        current = current_slot(session)
        slot = SkillSlot(checked.kind, digest, entry.pin, files)
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
        warnings = checked.warnings(entry, name)
        if unchanged or dry_run:
            return Published(entry, data, seq, warnings)
        write(ctx, session, entry, decisions, meta, current, slot, reason=reason)
        data["published"] = True
        return Published(entry, data, seq, warnings)


@dataclass(frozen=True, slots=True)
class Rendered:
    entry: GraphEntry
    data: dict[str, JsonValue]
    error: GraphCliError | None
    warnings: tuple[WarningRecord, ...] = ()


def render(
    ctx: AdminContext,
    *,
    graph_ref: str,
    source: Path,
    out: Path,
    force: bool,
    inventory: Mapping[str, JsonValue],
) -> Rendered:
    """Write ``<out>/graph-<graph>/`` as ``graph skills install`` would (Q5). Problems of the
    curated text do not stop the preview; they come back as ``admin.skill_invalid`` next to
    the data, because publish would refuse them."""
    entry = ctx.resolve(graph_ref, "graph skill render")
    ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=RequestMeta.of("graph skill render", {"graph": graph_ref}),
        graph=entry.id,
    )
    name = graph_skill_name(entry.name)
    if not out.is_dir():
        raise GraphCliError(
            "usage.invalid_argument",
            f"--out {out}: no such directory; create it first",
            details={"argument": "out"},
        )
    checked = check_source(ctx, entry, source, inventory)
    dest = out / name
    exists = dest.is_symlink() or dest.exists()
    if exists and (not dest.is_dir() or any(dest.iterdir())) and not force:
        raise GraphCliError(
            "admin.skill_render_exists",
            f"{dest} exists; pass --force to replace it",
            details={"graph": entry.name, "dir": str(source), "out": str(out), "path": str(dest)},
        )
    try:
        if exists:
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        write_tree(dest, checked.rendered)
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot write {dest}: {exc.strerror or exc}",
            details={"argument": "out"},
        ) from exc
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "skill": name,
        "kind": checked.kind,
        "hash": skill_hash(checked.files),
        "model": entry.pin.to_json(),
        "path": str(dest),
        "files": sorted(checked.rendered),
        "problems": _json(checked.problems),
        "size": checked.size.to_json() if checked.size is not None else None,
    }
    error = None
    if checked.problems:
        error = _invalid(source, entry, name, checked.problems)
    return Rendered(entry, data, error, checked.warnings(entry, name))


def refuse(source: Path | str, entry: GraphEntry, name: str, problems: list[Problem]) -> None:
    if problems:
        raise _invalid(source, entry, name, problems)


def _invalid(
    source: Path | str, entry: GraphEntry, name: str, problems: list[Problem]
) -> GraphCliError:
    return GraphCliError(
        "admin.skill_invalid",
        f"{source} is not a valid skill for graph {entry.name}: {_summary(problems)}",
        details={"graph": entry.name, "skill": name, "problems": _json(problems)},
    )


def _summary(problems: list[Problem]) -> str:
    shown = "; ".join(f"{p.path or '.'}: {p.message}" for p in problems[:5])
    more = len(problems) - 5
    return f"{shown}; and {more} more" if more > 0 else shown


def _json(problems: list[Problem]) -> list[JsonValue]:
    return [p.to_json() for p in problems]
