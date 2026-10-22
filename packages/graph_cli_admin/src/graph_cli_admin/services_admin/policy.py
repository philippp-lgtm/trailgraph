"""``graph-admin policy check|show|diff|apply|explain`` (contracts B5.15, B6.13-B6.16).

An authored policy file is compiled against this installation: graph names
resolve to ids, and a graph policy is checked against the graph's pinned
model (an unknown type warns with ``admin.policy_unknown_type``). Its ``id``
says which layer it is; a graph policy needs ``--graph``, an installation
policy refuses it.

``apply`` is ``maint.policy_change``. The installation policy is an
installation event (the intent), the file switch under the registry lock
and a confirmation event without ``change``; a graph policy follows W.e:
the intent in the graph stream, the file switch under the graph lock, the
installation event anchoring the intent. Both intents record
``change.policy{from, to}``. A policy under which the applying principal
could no longer change policies is refused before anything is recorded
(``admin.policy_lockout``); applying the installed document again changes
nothing.

``check``, ``show``, ``diff`` and ``explain`` write nothing and take no
lock. ``explain`` asks ``authorize()`` what an app caller from a project, or
from no project, would get: with the installed policy or with a file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import Compiled, compile_policy, layer_of_source
from graph_cli.access_control.documents import GRAPH_LAYER, INSTALLATION_LAYER, RESOURCE_RE, rules
from graph_cli.access_control.effective import graph_level, project_warning, scope_of
from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.audit import events
from graph_cli.audit.verify import InstalledPolicy
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.kernel.actions import WRITE_ACTIONS, is_known_action, level_actions
from graph_cli.kernel.actor import (
    ABSENT,
    ActorContext,
    Assurance,
    ClientInfo,
    Entrypoint,
    Facet,
    is_valid_asserted_value,
)
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.policy_files import installation_policy_path, read_policy_file
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model

ACTION: Final = "maint.policy_change"
APP_PROGRAM: Final = "graph"
# Decisions are shown as granted when a reason is given: obligations are listed, not failed.
_PROBE: Final = AuthzContext(op_count=1, has_reason=True, dry_run=False)


@dataclass(frozen=True, slots=True)
class Outcome:
    data: dict[str, JsonValue]
    warnings: tuple[WarningRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class _Source:
    compiled: Compiled
    entry: GraphEntry | None  # the graph of a graph policy
    warnings: tuple[WarningRecord, ...]


def check(ctx: AdminContext, *, file: Path, graph_ref: str | None) -> Outcome:
    source = _compile(ctx, file, graph_ref)
    compiled = source.compiled
    data: dict[str, JsonValue] = {
        "file": str(file),
        "layer": compiled.layer,
        "graph": _graph_name(source.entry),
        "hash": compiled.hash,
        "rules": len(rules(compiled.document)),
        "document": compiled.document,
    }
    return Outcome(data, source.warnings)


def show(ctx: AdminContext, *, graph_ref: str | None) -> Outcome:
    entry = ctx.catalog.resolve(graph_ref) if graph_ref is not None else None
    installed = _installed(ctx, entry)
    document = installed if installed is not None or entry is not None else BUILTIN_POLICY
    data: dict[str, JsonValue] = {
        "layer": GRAPH_LAYER if entry is not None else INSTALLATION_LAYER,
        "graph": _graph_name(entry),
        "installed": installed is not None,
        "hash": hash_value(document) if document is not None else None,
        "document": dict(document) if document is not None else None,
    }
    return Outcome(data)


def diff(ctx: AdminContext, *, file: Path, graph_ref: str | None) -> Outcome:
    source = _compile(ctx, file, graph_ref)
    new = source.compiled.document
    installed = _installed(ctx, source.entry)
    old = installed if installed is not None or source.entry is not None else BUILTIN_POLICY
    old_rules = {str(r.get("id")): r for r in rules(old or {})}
    new_rules = {str(r.get("id")): r for r in rules(new)}
    changed_keys = sorted(
        key
        for key in {*(old or {}), *new}
        if key != "rules" and (old or {}).get(key) != new.get(key)
    )
    data: dict[str, JsonValue] = {
        "layer": source.compiled.layer,
        "graph": _graph_name(source.entry),
        "from": hash_value(old) if old is not None else None,
        "installed": installed is not None,
        "to": source.compiled.hash,
        "changed": old is None or hash_value(old) != source.compiled.hash,
        "rules": {
            "added": sorted(set(new_rules) - set(old_rules)),
            "removed": sorted(set(old_rules) - set(new_rules)),
            "changed": sorted(
                i for i in set(old_rules) & set(new_rules) if old_rules[i] != new_rules[i]
            ),
        },
        "keys_changed": list(changed_keys),
    }
    return Outcome(data, source.warnings)


def apply(ctx: AdminContext, *, file: Path, graph_ref: str | None, reason: str) -> Outcome:
    source = _compile(ctx, file, graph_ref)
    compiled, entry = source.compiled, source.entry
    resource = "installation" if entry is None else f"graph:{entry.id}"
    if entry is None:
        future = policy_set(compiled.document)
    else:
        future = policy_set(ctx.policy.current(None).documents[0], compiled.document)
    lockout = authorize(ctx.actor, ACTION, resource, _PROBE, future)
    if lockout.effect == "deny":
        raise GraphCliError(
            "admin.policy_lockout",
            f"{file} would deny {ACTION} to the applying principal (rule {lockout.rule_id})",
            details={"rule": lockout.rule_id},
        )
    meta = RequestMeta.of(
        "policy apply",
        {"graph": str(entry.id) if entry else None, "hash": compiled.hash, "reason": reason},
    )
    decisions = ctx.authorize(
        [(ACTION, resource)], request=meta, graph=entry.id if entry else None, reason=reason
    )
    data: dict[str, JsonValue] = {
        "layer": compiled.layer,
        "graph": _graph_name(entry),
        "from": None,
        "to": compiled.hash,
        "applied": False,
    }
    if entry is None:
        with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
            old = _hash_of(registry.read_installation_policy())
            data["from"] = old
            if old != compiled.hash:
                change = events.policy_change(old, compiled.hash)
                receipt = ctx.audit.append_installation(
                    _event(ctx, decisions, meta, change, reason)
                )
                registry.replace_installation_policy(compiled.document, receipt)
                # The confirmation proves the switch to audit verify (B5.15).
                ctx.audit.append_installation(_event(ctx, decisions, meta, None, reason))
                data["applied"] = True
        return Outcome(data, source.warnings)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        old = _hash_of(session.read_policy())
        data["from"] = old
        if old != compiled.hash:
            change = events.policy_change(old, compiled.hash)
            receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
            session.replace_policy(compiled.document, receipt)
            ctx.anchor(entry, decisions, meta, receipt, reason=reason)
            data["applied"] = True
    return Outcome(data, source.warnings)


def explain(
    ctx: AdminContext,
    *,
    graph_ref: str,
    project: str | None,
    action: str | None,
    type_curie: str | None,
    file: Path | None,
) -> Outcome:
    entry = ctx.catalog.resolve(graph_ref)
    installation = ctx.policy.current(None).documents[0]
    layer = _installed(ctx, entry)
    warnings: tuple[WarningRecord, ...] = ()
    if file is not None:
        source = _compile(
            ctx, file, graph_ref if layer_of_source(_read(file)) == GRAPH_LAYER else None
        )
        warnings = source.warnings
        if source.entry is None:
            installation = source.compiled.document
        else:
            layer = source.compiled.document
    policy = policy_set(installation, layer)
    caller = _app_caller(ctx.actor, project)
    resource = f"graph:{entry.id}" + (f"/type:{type_curie}" if type_curie is not None else "")
    if not RESOURCE_RE.match(resource):
        raise GraphCliError(
            "usage.invalid_argument",
            f"--type {type_curie}: not a type CURIE",
            details={"argument": "type"},
        )
    if action is not None and not is_known_action(action):
        raise GraphCliError(
            "usage.invalid_argument",
            f"--action {action}: unknown action",
            details={"argument": "action"},
        )
    actions = [action] if action is not None else _app_actions()
    decisions = [authorize(caller, a, resource, _PROBE, policy) for a in actions]
    warning = project_warning(caller, policy)
    data: dict[str, JsonValue] = {
        "graph": entry.name,
        "project": project,
        "scope": scope_of(caller, policy),
        "policy_hash": policy.hash,
        "level": graph_level(caller, entry.id, policy),
        "resource": resource,
        "decisions": [_decision_json(d) for d in decisions],
    }
    return Outcome(data, (*warnings, *((warning,) if warning is not None else ())))


# ---- helpers -----------------------------------------------------------------------


def _compile(ctx: AdminContext, file: Path, graph_ref: str | None) -> _Source:
    data = _read(file)
    layer = layer_of_source(data)
    if layer == GRAPH_LAYER and graph_ref is None:
        raise GraphCliError(
            "usage.missing_argument",
            f"{file} is a graph policy; name the graph with --graph",
            details={"argument": "graph"},
        )
    if layer == INSTALLATION_LAYER and graph_ref is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            f"{file} is the installation policy; it applies to every graph, drop --graph",
            details={"argument": "graph"},
        )
    graphs = {e.name: e.id for e in ctx.catalog.list_graphs(include_archived=True)}
    entry = ctx.catalog.resolve(graph_ref) if graph_ref is not None else None
    classes = pinned_model(ctx.catalog, entry).classes if entry is not None else None
    compiled = compile_policy(
        data,
        file=str(file),
        graphs=graphs,
        graph=entry.id if entry is not None else None,
        known_types=frozenset(classes) if classes is not None else None,
        extended_types=frozenset(
            name
            for name, cls in (classes or {}).items()
            if cls.abstract or any(name in other.ancestors for other in classes.values())
        )
        if classes is not None
        else frozenset(),
    )
    warnings = tuple(
        WarningRecord(
            "admin.policy_unknown_type",
            f"the graph's model has no type {name}; the limit applies once it has",
            {"graph": entry.name if entry else None, "type": name},
        )
        for name in compiled.unknown_types
    )
    return _Source(compiled, entry, warnings)


def _read(file: Path) -> bytes:
    try:
        return file.read_bytes()
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot read {file}: {exc.strerror or exc}",
            details={"argument": "file"},
        ) from exc


def _installed(ctx: AdminContext, entry: GraphEntry | None) -> Mapping[str, JsonValue] | None:
    """The installed document of a layer, checked (``policy.invalid`` when broken)."""
    source = FilePolicySource(ctx.roots.data_root)
    if entry is not None:
        return source.graph(entry.id)
    if read_policy_file(installation_policy_path(ctx.roots.data_root)) is None:
        return None
    return source.installation()


def _hash_of(data: bytes | None) -> str | None:
    """``from`` of a change: the stored document's hash; ``None`` when there is none (or it
    is not a JSON object, which ``audit verify`` reports)."""
    observed = InstalledPolicy.of(data).observed
    return observed if observed is not None and observed.startswith("sha256:") else None


def _event(
    ctx: AdminContext,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    change: Mapping[str, JsonValue] | None,
    reason: str,
) -> AuditEventDraft:
    return events.maintenance(
        actor=ctx.actor,
        decisions=decisions,
        request=meta,
        ts=ctx.clock.now(),
        event_id=ctx.event_id(),
        env=ctx.env,
        change=change,
        reason=reason,
    )


def _app_caller(maintainer: ActorContext, project: str | None) -> ActorContext:
    """An app invocation by the same principal: from ``project``, or from no project."""
    if project is not None and not is_valid_asserted_value(project):
        raise GraphCliError(
            "usage.invalid_argument",
            f"--project {project}: not a valid project name",
            details={"argument": "project"},
        )
    facet = Facet(Assurance.ASSERTED, id=project, source="pointer") if project else ABSENT
    return dataclasses.replace(
        maintainer,
        agent=ABSENT,
        session=ABSENT,
        project=facet,
        client=ClientInfo(APP_PROGRAM, maintainer.client.version, Entrypoint.APP),
    )


def _app_actions() -> list[str]:
    return [*(a for a in level_actions("read") if not a.endswith("*")), *sorted(WRITE_ACTIONS)]


def _decision_json(decision: Decision) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "action": decision.action,
        "effect": decision.effect,
        "rule": decision.rule_id,
        "code": decision.code,
    }
    obligations = decision.obligations.to_json() if decision.effect == "allow" else None
    data["obligations"] = obligations
    return data


def _graph_name(entry: GraphEntry | None) -> str | None:
    return entry.name if entry is not None else None
