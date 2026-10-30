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
from no project, or a request with a given server token would get: with the
installed policy or with a file.

``for_graph`` is what the live view shows of one graph: the rules of both
installed layers that can decide on it, the obligations, the graph's type
limits, and what ``explain`` answers for every project, for no project and
for every token. It reads the installed files again, as ``show`` does.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import Compiled, compile_policy, layer_of_source
from graph_cli.access_control.documents import (
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    RESOURCE_RE,
    glob,
    rules,
    strings,
)
from graph_cli.access_control.effective import (
    graph_level,
    maintains,
    project_warning,
    scope_of,
    withheld_actions,
)
from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.audit import events
from graph_cli.audit.verify import (
    InstalledPolicy,
    graph_creations,
    installation_changes,
    verify_stream,
)
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.tokens import ADMIN_SCOPE, TokenRecord, parse_tokens
from graph_cli.kernel.actions import (
    PROJECT_ADMIN_ACTIONS,
    WRITE_ACTIONS,
    is_known_action,
    level_actions,
)
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
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditEventDraft, AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import RegistrySession
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.policy_files import installation_policy_path, read_policy_file
from graph_cli.storage.files.token_files import read_tokens_file, tokens_path
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.tokens import check_name, server_actor

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
    source = _compile(ctx, file, graph_ref, "policy check")
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
    entry = ctx.resolve(graph_ref, "policy show") if graph_ref is not None else None
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
    source = _compile(ctx, file, graph_ref, "policy diff")
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
    source = _compile(ctx, file, graph_ref, "policy apply")
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
            if old != compiled.hash or _unmirrored(ctx, old):
                _apply_installation(ctx, registry, compiled, old, decisions, meta, reason)
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
    token: str | None = None,
) -> Outcome:
    if token is not None and project is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            "--token and --project exclude each other: a token names its own project",
            details={"argument": "project"},
        )
    entry = ctx.resolve(graph_ref, "policy explain")
    installation = ctx.policy.current(None).documents[0]
    layer = _installed(ctx, entry)
    warnings: tuple[WarningRecord, ...] = ()
    if file is not None:
        source = _compile(
            ctx,
            file,
            graph_ref if layer_of_source(_read(file)) == GRAPH_LAYER else None,
            "policy explain",
        )
        warnings = source.warnings
        if source.entry is None:
            installation = source.compiled.document
        else:
            layer = source.compiled.document
    policy = policy_set(installation, layer)
    record = _token(ctx, token) if token is not None else None
    caller = (
        server_actor(record, ctx.actor.client.version)
        if record is not None
        else _app_caller(ctx.actor, project)
    )
    if record is not None:
        project = record.project
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
    admin = record is not None and record.scope == ADMIN_SCOPE
    default = list(PROJECT_ADMIN_ACTIONS) if admin else _app_actions()
    actions = [action] if action is not None else default
    decisions = [authorize(caller, a, resource, _PROBE, policy) for a in actions]
    warning = project_warning(caller, policy)
    data: dict[str, JsonValue] = {
        "graph": entry.name,
        "project": project,
        "token": token,
        "scope": scope_of(caller, policy),
        "policy_hash": policy.hash,
        "level": graph_level(caller, entry.id, policy),
        "withheld": withheld_actions(caller, entry.id, policy),
        "maintains": maintains(caller, entry.id, policy),
        "resource": resource,
        "decisions": [_decision_json(d) for d in decisions],
    }
    return Outcome(data, (*warnings, *((warning,) if warning is not None else ())))


def for_graph(ctx: AdminContext, entry: GraphEntry) -> dict[str, JsonValue]:
    """The policies as they bear on ``entry``: ``layers`` (installation, then the graph's
    own) with the rules that can decide on it, ``callers`` with what ``explain`` answers
    for each project, no project and each token, and ``graphs`` (id to name) for the
    resources the rules name."""
    source = FilePolicySource(ctx.roots.data_root)  # the files now: a view runs for long
    installation, own = source.installation(), source.graph(entry.id)
    policy = policy_set(installation, own)
    resource = f"graph:{entry.id}"
    layers: list[JsonValue] = []
    for layer, document, installed in (
        (INSTALLATION_LAYER, installation, installation is not BUILTIN_POLICY),
        (GRAPH_LAYER, own or {}, own is not None),
    ):
        layers.append(
            {
                "layer": layer,
                "installed": installed,
                "hash": hash_value(document) if document else None,
                "rules": [_rule_json(r) for r in rules(document) if _decides_on(r, resource)],
                "obligations": _obligations(document),
                "types": document.get("types") or {},
            }
        )
    projects = strings(installation, "projects")
    callers: list[JsonValue] = [
        {"project": p, **_access(_app_caller(ctx.actor, p), entry, policy)}
        for p in (*projects, None)  # the maintainer's own principal, through graph
    ]
    records = parse_tokens(read_tokens_file(tokens_path(ctx.roots.data_root)))
    today = ctx.clock.now().date()
    for record in sorted(records, key=lambda r: r.name):
        caller = server_actor(record, ctx.actor.client.version)
        callers.append(
            {
                "token": record.name,
                "project": record.project,
                "scope": record.scope,
                "expired": not record.valid_on(today),
                **_access(caller, entry, policy),
            }
        )
    graphs = ctx.catalog.list_graphs(include_archived=True)
    return {
        "policy_hash": policy.hash,
        "layers": layers,
        "projects": list(projects),
        "callers": callers,
        "graphs": {str(e.id): e.name for e in graphs},
    }


# ---- helpers -----------------------------------------------------------------------


def _compile(ctx: AdminContext, file: Path, graph_ref: str | None, command: str) -> _Source:
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
    entry = ctx.resolve(graph_ref, command) if graph_ref is not None else None
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


def _apply_installation(
    ctx: AdminContext,
    registry: RegistrySession,
    compiled: Compiled,
    old: str | None,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    reason: str,
) -> None:
    """Intent, one mirror per graph stream, rename, confirmation (B5.15).

    Every graph's writer lock is held from its mirror until the confirmation,
    so no write lands between a graph's mirror and the switch; a write that
    decided before re-checks the policy under its lock (B6.14). A crash
    before the rename leaves an interrupted intent whose mirrors are void.
    """
    change = events.policy_change(old, compiled.hash)
    receipt = ctx.audit.append_installation(_event(ctx, decisions, meta, change, reason))
    mirror = events.installation_policy_change(old, compiled.hash)
    with contextlib.ExitStack() as stack:
        mirrors = []
        for graph_id in sorted(ctx.maintenance.graph_ids(), key=str):  # one lock order
            session = stack.enter_context(
                ctx.maintenance.maintenance_session(graph_id, timeout_s=ctx.lock_timeout_s)
            )
            entry = session.manifest().entry
            mirrors.append(ctx.intent(session, entry, decisions, meta, mirror, reason=reason))
        registry.replace_installation_policy(compiled.document, receipt)
        # The confirmation proves the switch and anchors the mirrors (B5.15).
        ctx.audit.append_installation(_event(ctx, decisions, meta, None, reason, anchors=mirrors))


def _unmirrored(ctx: AdminContext, installed: str | None) -> bool:
    """A graph that existed at the last installation policy change has no mirror of it
    (applied before mirrors existed, or a confirmation lost): applying the same policy
    again records the mirrors."""
    if installed is None:
        return False
    stream = verify_stream(ctx.audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM)
    changes = installation_changes(stream.events, InstalledPolicy(installed))
    last = next((c for c in reversed(changes) if c.committed), None)
    if last is None:
        return False
    created = graph_creations(stream.events)
    return any(
        created.get(str(g), 0) < last.seq
        and last.mirrored is not None
        and str(g) not in last.mirrored
        for g in ctx.maintenance.graph_ids()
    )


def _event(
    ctx: AdminContext,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    change: Mapping[str, JsonValue] | None,
    reason: str,
    *,
    anchors: Sequence[AuditReceipt] = (),
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
        anchors=anchors,
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


def _token(ctx: AdminContext, name: str) -> TokenRecord:
    check_name(name, "--token")
    records = parse_tokens(read_tokens_file(tokens_path(ctx.roots.data_root)))
    found = next((r for r in records if r.name == name), None)
    if found is None:
        raise GraphCliError(
            "admin.token_not_found", f"no token named {name}", details={"name": name}
        )
    return found


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


# What a compiled rule stands for, by the prefix of its id (``access_control.compiler``).
_RULE_KINDS: Final = (
    ("grant.", "grant"),
    ("maint.", "maintainer"),
    ("scope.", "scope"),
    ("no_project.", "scope"),
    ("deny.", "deny"),
    ("type.", "type_limit"),
    ("builtin.", "builtin"),
)


def _decides_on(rule: Mapping[str, JsonValue], graph: str) -> bool:
    """Whether ``rule`` can decide on ``graph:<id>`` or a type in it: a resource glob
    covers it and no ``unless`` condition on resources alone lifts the rule there."""
    if not any(glob(p, graph) or p.startswith(graph + "/") for p in strings(rule, "resource")):
        return False
    conditions = rule.get("unless")
    return not any(
        isinstance(condition, Mapping)
        and set(condition) == {"resource"}
        and any(glob(p, graph) for p in strings(condition, "resource"))
        for condition in (conditions if isinstance(conditions, list) else ())
    )


def _rule_json(rule: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The rule with its ``kind`` and the ``level`` its actions are, if any."""
    rule_id = str(rule.get("id"))
    kind = next((k for prefix, k in _RULE_KINDS if rule_id.startswith(prefix)), "rule")
    if kind == "scope" and rule_id.endswith(":maintain"):
        kind = "maintain"
    actions = strings(rule, "action")
    level = next((lv for lv in ("read", "write") if actions == list(level_actions(lv))), None)
    return {**rule, "kind": kind, "level": level}


def _obligations(document: Mapping[str, JsonValue]) -> list[JsonValue]:
    found = document.get("obligations")
    return [o for o in found if isinstance(o, Mapping)] if isinstance(found, list) else []


def _access(caller: ActorContext, entry: GraphEntry, policy: PolicySet) -> dict[str, JsonValue]:
    return {
        "principal": caller.principal.id,
        "entrypoint": caller.client.entrypoint.value,
        "level": graph_level(caller, entry.id, policy),
        "withheld": withheld_actions(caller, entry.id, policy),
        "maintains": maintains(caller, entry.id, policy),
    }


def _graph_name(entry: GraphEntry | None) -> str | None:
    return entry.name if entry is not None else None
