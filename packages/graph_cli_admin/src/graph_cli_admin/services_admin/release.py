"""``graph-admin ontology release``: publish a release into a graph and pin it (B7, W.e).

Two maintenance operations under one graph lock, each in the W.e shape:
intent in the graph stream, the effect's rename (the commit point), then an
installation event anchoring the intent:

1. ``maint.ontology_release`` installs ``ontology/<version>/`` (skipped if
   that version is installed with the same artifact hash);
2. ``maint.ontology_pin`` switches the manifest pin from the current to the
   new release and seeds the alias prefixes of classes the release adds
   (existing prefixes never change; a renamed class keeps the prefix of its
   old name; a seed another class already uses is left out with the warning
   ``admin.alias_prefix_taken``).

Refused before any event: archived graph (``graph.archived``), the version
installed with other content (``admin.release_exists``), a bump smaller
than the changes (``admin.version_bump_insufficient``, also downgrades),
a higher major on a graph whose data has error findings under the new
version (``check_graph`` under the lock, B7.14: ``admin.migration_required``
with the counts and the first 20 findings). A larger bump than needed is a
warning.
Re-running a release that is already pinned changes nothing.

Every bump also re-checks the published function catalog against the new
model (``graph_cli.kinetic.check_catalog``, K5): a function that no longer
resolves refuses the release with ``admin.functions_broken`` (first 20
problems in the details). The published action catalog gets the same check
(``check_actions``, ``admin.actions_broken``), against the function names the graph
will have. The fix is ``graph push`` with the ontology and the fixed or deleted
files together; ``plan`` then leaves the replaced catalog to the push, which checks
the new files against the same model.

``plan`` makes every check under the lock and ``write`` writes, so
``graph push`` (``sources``) runs a release, a skill publish and a function and
action publish under one lock, checking all before the first write.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from graph_cli.access_control.terms import named_terms
from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, ManifestData, SlotKind
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_compile import check_actions
from graph_cli.kinetic.compile import check_catalog
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli.services.term_model import extended_types, term_model
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.graphs import merged_alias_prefixes
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    Compiled,
    PinnedModel,
    compile_source,
    diff,
    module_bump_warnings,
    release_files,
)

MAX_FINDINGS = 20
FUNCTIONS_DAMAGED = frozenset({"functions.slot_invalid", "format.unsupported_version"})
ACTIONS_DAMAGED = frozenset({"actions.slot_invalid", "format.unsupported_version"})
DAMAGED: Final[dict[SlotKind, frozenset[str]]] = {
    "functions": FUNCTIONS_DAMAGED,
    "actions": ACTIONS_DAMAGED,
}


@dataclass(frozen=True, slots=True)
class Released:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


@dataclass(frozen=True, slots=True)
class Planned:
    """A release checked under the graph lock: everything ``write`` writes. ``old`` is
    ``None`` when the graph is pinned to the release already (nothing to write)."""

    manifest: ManifestData
    compiled: Compiled
    old: PinnedModel | None
    install: bool
    prefixes: dict[TermRef, str]
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]

    @property
    def unchanged(self) -> bool:
        return self.old is None


@dataclass(frozen=True, slots=True)
class Decisions:
    release: tuple[Decision, ...]
    pin: tuple[Decision, ...]


def authorize(
    ctx: AdminContext, entry: GraphEntry, meta: RequestMeta, *, reason: str | None = None
) -> Decisions:
    resource = f"graph:{entry.id}"
    pairs = [("maint.ontology_release", resource)]
    release = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    pairs = [("maint.ontology_pin", resource)]
    pin = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    ctx.read_data(entry, meta)  # a release checks the data; its findings quote values
    return Decisions(release, pin)


def release(ctx: AdminContext, *, graph_ref: str, source: Path, module: str | None) -> Released:
    compiled = compile_source(source, module)
    entry = ctx.resolve(graph_ref, "ontology release")
    check_state(entry)
    meta = RequestMeta.of(
        "ontology release", {"graph": str(entry.id), "ontology": compiled.pin.to_json()}
    )
    decisions = authorize(ctx, entry, meta)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        planned = plan(ctx, session, compiled)
        if planned.unchanged:
            return Released(planned.manifest.entry, planned.data, planned.seq, ())
        pinned = write(ctx, session, planned, decisions, meta)
        return Released(pinned, planned.data, planned.seq, planned.warnings)


def plan(
    ctx: AdminContext,
    session: MaintenanceSession,
    compiled: Compiled,
    *,
    functions_replaced: bool = False,
    actions_replaced: bool = False,
    function_names: frozenset[str] | None = None,
) -> Planned:
    """Every check of a release, under the lock; refuses before anything is written.

    ``functions_replaced`` / ``actions_replaced``: the caller publishes new function or
    action files after the release and checks them against the new model itself, so the
    published catalog of that kind is not checked here. ``function_names`` are the
    functions the graph will have (the pushed ones when ``functions_replaced``); omitted,
    the published ones."""
    target = compiled.pin
    manifest = session.manifest()  # W.6: re-read under the lock
    entry = manifest.entry
    check_state(entry)
    current = entry.pin
    installed = session.release_hashes()
    if installed.get(target.version, target.artifact_hash) != target.artifact_hash:
        raise GraphCliError(
            "admin.release_exists",
            f"release {target.version} is installed with other content",
            details={
                "version": target.version,
                "expected": installed[target.version],
                "actual": target.artifact_hash,
            },
        )
    head = session.head()
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "from": current.to_json(),
        "to": target.to_json(),
        "released": False,
        "pinned": False,
    }
    if current == target:
        return Planned(manifest, compiled, None, False, {}, data, head.seq, ())
    old_model = load_compiled(
        session.read_release_file(current.version, COMPILED_FILE),
        expected_hash=current.artifact_hash,
    )
    report, bump = diff(old_model, compiled.model)
    data["diff"] = report.to_json()
    bump_details: dict[str, JsonValue] = {
        "from": current.version,
        "to": target.version,
        "level": report.level,
    }
    if bump == "insufficient":
        raise GraphCliError(
            "admin.version_bump_insufficient",
            f"{current.version} -> {target.version} does not cover {report.level} changes",
            details=bump_details,
        )
    if Version.parse(target.version).major > Version.parse(current.version).major and head.seq:
        _check_data(compiled.model, session, bump_details, head.seq)
    functions = current_catalog(session, "functions")
    if not functions_replaced and functions is not None:
        broken = catalog_problems(functions, compiled.model)
        if broken:
            raise functions_broken(broken, bump_details)
    if not actions_replaced:
        actions = current_catalog(session, "actions")
        if actions is not None:
            if function_names is None:
                function_names = names_of(functions)
            broken = catalog_problems(actions, compiled.model, function_names=function_names)
            if broken:
                raise actions_broken(broken, bump_details)
    warnings = module_bump_warnings(old_model, compiled.model, report)
    warnings += policies_affected(ctx.policy.current(entry.id), entry, compiled.model)
    if bump == "excessive":
        warnings += (
            WarningRecord(
                "admin.version_bump_excessive",
                f"{current.version} -> {target.version} is more than {report.level} needs",
                bump_details,
            ),
        )
    prefixes, skipped = merged_alias_prefixes(entry.alias_prefixes, compiled)
    data["alias_prefixes_added"] = {
        str(curie): prefix
        for curie, prefix in prefixes.items()
        if curie not in entry.alias_prefixes
    }
    owners = {prefix: curie for curie, prefix in entry.alias_prefixes.items()}
    warnings += tuple(
        WarningRecord(
            "admin.alias_prefix_taken",
            f"{curie} gets no aliases: prefix {prefix} is used by {owners[prefix]}",
            {"type": str(curie), "prefix": prefix, "used_by": str(owners[prefix])},
        )
        for curie, prefix in skipped.items()
    )
    old = PinnedModel(old_model, current.artifact_hash)
    install = target.version not in installed
    return Planned(manifest, compiled, old, install, prefixes, data, head.seq, warnings)


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    planned: Planned,
    decisions: Decisions,
    meta: RequestMeta,
    *,
    reason: str | None = None,
) -> GraphEntry:
    """Install (unless installed) and pin a planned release; answers the pinned entry."""
    assert planned.old is not None, "nothing to commit"
    entry = planned.manifest.entry
    target = planned.compiled.pin
    if planned.install:
        release_id = ctx.ids.new_id()
        files = release_files(
            planned.compiled,
            previous=planned.old,
            release_id=release_id,
            released_at=ctx.clock.now(),
        )
        change = events.release_change(target.version, target.artifact_hash, release_id)
        receipt = ctx.intent(session, entry, decisions.release, meta, change, reason=reason)
        session.install_release(target.version, files, receipt)
        ctx.anchor(entry, decisions.release, meta, receipt, reason=reason)
        planned.data["released"] = True
    change = events.pin_change(entry.pin, target)
    receipt = ctx.intent(session, entry, decisions.pin, meta, change, reason=reason)
    pinned = replace(
        planned.manifest, entry=replace(entry, pin=target, alias_prefixes=planned.prefixes)
    )
    session.replace_manifest(pinned, receipt)
    ctx.anchor(entry, decisions.pin, meta, receipt, reason=reason)
    planned.data["pinned"] = True
    return pinned.entry


def _check_data(
    model: CompiledModel, session: MaintenanceSession, bump: dict[str, JsonValue], seq: int
) -> None:
    """B7.14: data under a new major must check clean, else a migration is needed."""
    errors = [f for f in check_graph(model, session.snapshot()) if f.severity is Severity.ERROR]
    if errors:
        counts = Counter(f.code for f in errors)
        raise GraphCliError(
            "admin.migration_required",
            f"{len(errors)} error findings under {bump['to']}; the data needs a migration",
            details={
                **bump,
                "seq": seq,
                "counts": dict(sorted(counts.items())),
                "findings": [f.to_json() for f in errors[:MAX_FINDINGS]],
            },
        )


def current_catalog(session: MaintenanceSession, kind: SlotKind) -> KineticSlot | None:
    """The published catalog of ``kind``; a damaged slot is replaced like a missing one."""
    try:
        return session.read_functions() if kind == "functions" else session.read_actions()
    except GraphCliError as exc:
        if exc.code not in DAMAGED[kind]:
            raise
        return None


def names_of(slot: KineticSlot | None) -> frozenset[str]:
    """The names in a published catalog (deprecated ones too: they still hold the name)."""
    if slot is None:
        return frozenset()
    key = slot.spec.list_key
    return frozenset(req_str(as_object(e, key), "name") for e in req_list(slot.compiled, key))


def catalog_problems(
    slot: KineticSlot | None,
    model: CompiledModel,
    *,
    function_names: frozenset[str] | None = None,
) -> list[Problem]:
    """The problems a published catalog has against ``model``; none for a missing one.
    ``function_names`` are the functions the graph has: an action may not share a name."""
    if slot is None:
        return []
    if slot.kind == "functions":
        return check_catalog(slot.compiled, model)
    return check_actions(slot.compiled, model, function_names=function_names or frozenset())


def catalog_broken(
    kind: SlotKind,
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str,
) -> GraphCliError:
    """``admin.<kind>_broken``: the first problems of the published catalog against the
    model of ``bump['to']``."""
    names = sorted({p.name for p in problems})
    return GraphCliError(
        f"admin.{kind}_broken",
        _would_not_resolve(kind, names, str(bump["to"])) + advice,
        details={
            **bump,
            "count": len(problems),
            kind: list[JsonValue](names[:MAX_FINDINGS]),
            "problems": [p.to_json() for p in problems[:MAX_FINDINGS]],
        },
    )


def functions_broken(
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str = "; fix or delete them in the same push",
) -> GraphCliError:
    """``admin.functions_broken`` (see ``catalog_broken``)."""
    return catalog_broken("functions", problems, bump, advice=advice)


def actions_broken(
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str = "; fix or delete them in the same push",
) -> GraphCliError:
    """``admin.actions_broken`` (see ``catalog_broken``)."""
    return catalog_broken("actions", problems, bump, advice=advice)


def _would_not_resolve(kind: SlotKind, names: list[str], version: str) -> str:
    shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
    noun = "function" if kind == "functions" else "action"
    plural = "s" if len(names) != 1 else ""
    return f"{len(names)} published {noun}{plural} would not resolve under {version}: {shown}"


def catalog_affected(
    kind: SlotKind,
    slot: KineticSlot | None,
    model: CompiledModel,
    *,
    function_names: frozenset[str] | None = None,
) -> tuple[WarningRecord, ...]:
    """``admin.<kind>_affected`` when the published catalog would not resolve under
    ``model`` (what ``ontology diff --graph`` shows before a release refuses)."""
    problems = catalog_problems(slot, model, function_names=function_names)
    if not problems:
        return ()
    names = sorted({p.name for p in problems})
    return (
        WarningRecord(
            f"admin.{kind}_affected",
            _would_not_resolve(kind, names, model.bundle_version),
            {
                "to": model.bundle_version,
                "count": len(problems),
                kind: list[JsonValue](names[:MAX_FINDINGS]),
                "problems": [p.to_json() for p in problems[:MAX_FINDINGS]],
            },
        ),
    )


def functions_affected(slot: KineticSlot | None, model: CompiledModel) -> tuple[WarningRecord, ...]:
    return catalog_affected("functions", slot, model)


def actions_affected(
    actions: KineticSlot | None, functions: KineticSlot | None, model: CompiledModel
) -> tuple[WarningRecord, ...]:
    """The action twin of ``functions_affected``; the action names are checked against the
    published functions."""
    return catalog_affected("actions", actions, model, function_names=names_of(functions))


def policies_affected(
    policy: PolicySet, entry: GraphEntry, model: CompiledModel
) -> tuple[WarningRecord, ...]:
    """What the installed policies name for ``entry`` that ``model`` no longer has (write
    allow lists plan W8): the types and terms of the installation's write grants and of the
    graph layer's term deny rules, with the warnings of ``policy check``:
    ``admin.policy_unknown_type`` for a type the model lacks, and for a type named in a write
    grant that is now abstract or has subclasses (an exact rule misses the entities of its
    subclasses);
    ``admin.policy_unknown_term`` for an attribute, relation or lifecycle attribute the type
    lacks (fail closed: the entry has no effect until a release adds it). A policy without
    write grants and term rules gives none."""
    named = named_terms(policy.documents, entry.id)
    if not named:
        return ()
    terms = term_model(model).types
    extended = extended_types(model.classes)
    version = model.bundle_version
    warnings: list[WarningRecord] = []
    unknown: set[str] = set()
    for item in named:
        found = terms.get(item.type)
        if item.kind == "type":
            if found is None:
                if item.type in unknown:
                    continue  # one warning per type, whichever rule names it
                unknown.add(item.type)
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_type",
                        f"release {version} has no type {item.type} in graph {entry.name}, which"
                        " an installed policy names; its write grants and term rules apply to"
                        " nothing until a release has it",
                        {"graph": entry.name, "type": item.type},
                    )
                )
            elif item.grant and item.type in extended:
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_type",
                        f"type {item.type} of graph {entry.name} is abstract or has subclasses in"
                        f" release {version}: its write grant is an exact rule and now misses the"
                        " entities of the subclasses; grant their types too",
                        {"graph": entry.name, "type": item.type},
                    )
                )
        elif found is not None:
            attribute = found.attributes.get(item.term)
            missing = (
                item.term not in found.relations
                if item.kind == "relation"
                else attribute is None or (item.kind == "lifecycle" and not attribute.lifecycle)
            )
            if missing:
                what = {
                    "relation": "relation",
                    "attribute": "attribute",
                    "lifecycle": "lifecycle attribute",
                }[item.kind]
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_term",
                        f"type {item.type} of graph {entry.name} has no {what} {item.term} in"
                        f" release {version}, which an installed policy names; nobody can write"
                        " a term that does not exist (fail closed), so the policy entry has no"
                        " effect until a release adds it",
                        {
                            "graph": entry.name,
                            "type": item.type,
                            "term": item.term,
                            "kind": item.kind,
                        },
                    )
                )
    return tuple(warnings)


def check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
