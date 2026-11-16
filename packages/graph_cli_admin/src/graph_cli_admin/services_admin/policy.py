"""``graph-admin policy check|show|diff|apply|explain`` (contracts B5.15, B6.13-B6.16).

An authored policy file is compiled against this installation: graph names
resolve to ids, and a graph policy is checked against the graph's pinned
model (an unknown type warns with ``admin.policy_unknown_type``) and its deny
rules against the roles the installed installation policy declares (an unknown
role warns with ``policy.role_unknown``). An installation policy's ``reads`` and ``writes``
grants are checked against the pinned model of each graph they name, and a graph policy's term
deny rules against its graph's: an unknown type warns with ``admin.policy_unknown_type``, an
attribute or relation the type lacks with ``admin.policy_unknown_term``, a write no call
can satisfy with ``admin.policy_unusable_write``; an abstract type is ``policy.invalid``.
A grant's ``markings`` are checked against the same models: a marking the model does not declare
warns with ``admin.policy_unknown_marking``, and a ``writes`` entry that names or covers a marked
attribute without the grant holding its markings warns with ``admin.policy_unusable_write``.
An installation policy that names no grant for a marking some graph's pinned model uses warns
with ``admin.marking_ungranted`` (``check``, ``diff``, ``apply`` and ``explain --file``; only
maintainer surfaces show those attributes then).
Its ``id`` says which layer it is; a graph policy needs ``--graph``, an installation
policy refuses it.

``apply`` is ``maint.policy_change``. An installation policy that no longer declares a role
that a token still holds, or an installed graph policy names, warns with
``admin.role_orphaned`` (one warning per role; the membership stays, no grant can name
the role, deny rules of graph policies naming it still apply).
The installation policy is an
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
from no project, or a request with a given server token, or a server token that holds
given roles (with the project verified when one is named), would get: with the installed
policy or with a file. Roles the policy used for the explanation does not declare warn
with ``policy.role_unknown`` but are explained all the same, so that a policy file can be
tried before it is applied.

``for_graph`` is what the live view shows of one graph: the rules of both
installed layers that can decide on it, the obligations, the graph's type
limits, and what ``explain`` answers for every project, for no project and
for every token. It reads the installed files again, as ``show`` does.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import Compiled, compile_policy, layer_of_source
from graph_cli.access_control.documents import (
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    RESOURCE_RE,
    declared_roles,
    glob,
    read_limited,
    rules,
    strings,
)
from graph_cli.access_control.effective import (
    graph_level,
    held_markings,
    maintains,
    project_warning,
    read_allowance,
    scope_of,
    withheld_actions,
    write_allowance,
    write_grant_types,
)
from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.access_control.terms import (
    TermModel,
    UnusableMarkedWrite,
    marking_resource,
    term_resource,
)
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
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditEventDraft, AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import RegistrySession
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.term_model import (
    FILE_ADD_ATTRIBUTES,
    concrete_types,
    extended_types,
    term_model,
)
from graph_cli.services.visibility import attribute_mask, filter_model
from graph_cli.storage.files.policy_files import installation_policy_path, read_policy_file
from graph_cli.storage.files.token_files import read_tokens_file, tokens_path
from graph_cli_admin.services_admin import marking_warnings
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.tokens import check_name, check_roles, server_actor

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
    warnings = (
        source.warnings if entry is not None else (*source.warnings, *_orphaned(ctx, compiled))
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
        return Outcome(data, warnings)
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


def _orphaned(ctx: AdminContext, compiled: Compiled) -> tuple[WarningRecord, ...]:
    """A warning for each role the installed installation policy declares, the new one does
    not, and a token still holds or an installed graph policy names."""
    dropped = declared_roles(ctx.policy.current(None).documents[0]) - declared_roles(
        compiled.document
    )
    if not dropped:
        return ()
    records = parse_tokens(read_tokens_file(tokens_path(ctx.roots.data_root)))
    source = FilePolicySource(ctx.roots.data_root)
    named: dict[str, list[str]] = {}
    for entry in ctx.catalog.list_graphs(include_archived=True):
        document = source.graph(entry.id)
        for role in _roles_named(document) if document is not None else ():
            named.setdefault(role, []).append(entry.name)
    warnings: list[WarningRecord] = []
    for role in sorted(dropped):
        held = sorted(r.name for r in records if role in r.roles)
        graphs = sorted(named.get(role, []))
        if held or graphs:
            warnings.append(
                WarningRecord(
                    "admin.role_orphaned",
                    f"the new installation policy no longer declares role {role}, which"
                    f" {_holders(held, graphs)}; the membership stays and no grant can name it,"
                    " while graph policy deny rules naming it still apply, until the role is"
                    " declared again or taken off",
                    {"role": role, "tokens": held, "graphs": graphs},
                )
            )
    return tuple(warnings)


def _holders(tokens: Sequence[str], graphs: Sequence[str]) -> str:
    parts = []
    if tokens:
        parts.append("tokens still hold (" + ", ".join(tokens) + ")")
    if graphs:
        parts.append("graph policies still name (" + ", ".join(graphs) + ")")
    return " and ".join(parts)


def _roles_named(value: JsonValue) -> set[str]:
    """The role names a compiled graph policy's rules and conditions name."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "role":
                found |= {item} if isinstance(item, str) else set(strings(value, "role"))
            else:
                found |= _roles_named(item)
    elif isinstance(value, list):
        for item in value:
            found |= _roles_named(item)
    return found


def explain(
    ctx: AdminContext,
    *,
    graph_ref: str,
    project: str | None,
    action: str | None,
    type_curie: str | None,
    file: Path | None,
    token: str | None = None,
    roles: Sequence[str] = (),
    attribute: str | None = None,
    relation: str | None = None,
    marking: str | None = None,
) -> Outcome:
    if marking is not None:
        _check_marking_question(marking, action, type_curie, attribute, relation)
    if attribute is not None and relation is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            "--attribute and --relation exclude each other: one decision on one term",
            details={"argument": "relation"},
        )
    term = attribute if attribute is not None else relation
    term_argument = "attribute" if attribute is not None else "relation"
    for needed, value in (("type", type_curie), ("action", action)):
        if term is not None and value is None:
            raise GraphCliError(
                "usage.missing_argument",
                f"--{term_argument} decides on a term of a type: name the {needed} with --{needed}",
                details={"argument": needed},
            )
    if token is not None and project is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            "--token and --project exclude each other: a token names its own project",
            details={"argument": "project"},
        )
    if token is not None and roles:
        raise GraphCliError(
            "usage.invalid_argument",
            "--token and --role exclude each other: a token has its own roles",
            details={"argument": "role"},
        )
    held = check_roles(roles, "--role")
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
    unknown = sorted(set(held) - declared_roles(installation))
    if unknown:
        warnings = (*warnings, _roles_not_declared(unknown))
    record = _token(ctx, token) if token is not None else None
    if held:
        caller = _role_caller(ctx.actor, project, held)
    elif record is not None:
        caller = server_actor(record, ctx.actor.client.version)
    else:
        caller = _app_caller(ctx.actor, project)
    if record is not None:
        project = record.project
    resource = f"graph:{entry.id}" + (f"/type:{type_curie}" if type_curie is not None else "")
    if marking is not None:
        marked = marking_resource(entry.id, marking)
        if marked is None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--marking {marking}: not a marking CURIE",
                details={"argument": "marking"},
            )
        resource = marked
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
    type_decision: Decision | None = None
    if term is not None:
        assert action is not None  # checked above
        fine = term_resource(entry.id, str(type_curie), "attr" if attribute else "rel", term)
        if fine is None:
            what = "an attribute name" if attribute else "a relation CURIE"
            raise GraphCliError(
                "usage.invalid_argument",
                f"--{term_argument} {term}: not {what}",
                details={"argument": term_argument},
            )
        # the type decision is shown next to it: an exact type rule does not match the terms
        # below it, and a write needs both
        type_decision = authorize(caller, action, resource, _PROBE, policy)
        resource = fine
    admin = record is not None and record.scope == ADMIN_SCOPE
    default = list(PROJECT_ADMIN_ACTIONS) if admin else _app_actions()
    actions = [action] if action is not None else default
    decisions = [authorize(caller, a, resource, _PROBE, policy) for a in actions]
    warning = project_warning(caller, policy)
    data: dict[str, JsonValue] = {
        "graph": entry.name,
        "project": project,
        "token": token,
        "roles": list(caller.roles),
        "scope": scope_of(caller, policy),
        "policy_hash": policy.hash,
        "level": graph_level(caller, entry.id, policy),
        "withheld": withheld_actions(caller, entry.id, policy),
        "maintains": maintains(caller, entry.id, policy),
        "resource": resource,
        "decisions": [_decision_json(d) for d in decisions],
    }
    if type_decision is not None:
        data["type_decision"] = _decision_json(type_decision)
    if action is None:
        data.update(_allowances(entry, caller, policy, data["level"], _pinned_model(ctx, entry)))
    return Outcome(data, (*warnings, *((warning,) if warning is not None else ())))


def _check_marking_question(
    marking: str,
    action: str | None,
    type_curie: str | None,
    attribute: str | None,
    relation: str | None,
) -> None:
    """``--marking`` asks one decision (``read.get``, the only action a policy can name on a
    marking) on one marking of the graph: no type, attribute or relation goes with it."""
    for other, value in (("attribute", attribute), ("relation", relation), ("type", type_curie)):
        if value is not None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--marking and --{other} exclude each other: a marking belongs to the graph,"
                " not to a type",
                details={"argument": "marking"},
            )
    if action is None:
        raise GraphCliError(
            "usage.missing_argument",
            "--marking decides on a marking of the graph: name the action with --action read.get",
            details={"argument": "action"},
        )
    if action != "read.get":
        raise GraphCliError(
            "usage.invalid_argument",
            f"--marking {marking}: only read.get decides on a marking, not {action}",
            details={"argument": "action"},
        )


def _pinned_model(ctx: AdminContext, entry: GraphEntry) -> CompiledModel | None:
    """The graph's pinned model; ``None`` when it cannot be read."""
    with contextlib.suppress(GraphCliError):
        return pinned_model(ctx.catalog, entry)
    return None


def _allowances(
    entry: GraphEntry,
    caller: ActorContext,
    policy: PolicySet,
    level: JsonValue,
    model: CompiledModel | None,
) -> dict[str, JsonValue]:
    """What ``whoami`` shows of what the caller may read and write, as extra keys: ``reads``
    (the types it sees, when it sees only some), ``markings`` (the markings it holds, when the
    graph's model uses some it lacks) and ``writes`` (its write allow list, over what it sees).
    ``model`` is the graph's pinned model (``None`` when it cannot be read: then ``writes`` is
    decided on the types alone and there is neither ``reads`` nor ``markings``)."""
    used = frozenset() if model is None else model.used_markings()
    held = held_markings(caller, entry.id, policy, used) if used else frozenset()
    attributes = attribute_mask(model, held) if model is not None and used else None
    limited = read_limited(policy.documents, entry.id)
    if not limited and attributes is None and not write_grant_types(caller, entry.id, policy):
        return {}
    found: dict[str, JsonValue] = {}
    terms = None if model is None else term_model(model)
    visible = None
    if limited and model is not None:
        shown = read_allowance(caller, entry.id, policy, concrete_types(model.classes))
        if shown is not None:
            visible = frozenset(shown)
            found["reads"] = list(shown)
    if attributes is not None:
        found["markings"] = sorted(held)
    if model is not None and (visible is not None or attributes is not None):
        terms = term_model(filter_model(model, visible, attributes))
    writes = write_allowance(caller, entry.id, policy, terms, level=str(level))
    if writes is not None:
        found["writes"] = writes
    return found


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
    model = _pinned_model(ctx, entry)
    callers: list[JsonValue] = [
        {"project": p, **_access(_app_caller(ctx.actor, p), entry, policy, model)}
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
                "roles": list(record.roles),
                "expired": not record.valid_on(today),
                **_access(caller, entry, policy, model),
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
    entries = ctx.catalog.list_graphs(include_archived=True)
    graphs = {e.name: e.id for e in entries}
    entry = ctx.resolve(graph_ref, command) if graph_ref is not None else None
    own = pinned_model(ctx.catalog, entry) if entry is not None else None
    loaded: dict[GraphId, CompiledModel | None] = {}

    def model_of(found: GraphEntry) -> CompiledModel | None:
        """The pinned model of a graph, read once; ``None`` (not checked) for an archived graph
        and one whose model cannot be read."""
        if found.state == "archived":
            return None
        if entry is not None and own is not None and found.id == entry.id:
            return own
        if found.id not in loaded:
            try:
                loaded[found.id] = pinned_model(ctx.catalog, found)
            except GraphCliError:
                loaded[found.id] = None
        return loaded[found.id]

    def models(graph: GraphId) -> TermModel | None:
        """The pinned model of a graph a rule names, condensed (see ``model_of``)."""
        found = next((e for e in entries if e.id == graph), None)
        model = model_of(found) if found is not None else None
        return term_model(model) if model is not None else None

    classes = own.classes if own is not None else None
    compiled = compile_policy(
        data,
        file=str(file),
        graphs=graphs,
        graph=entry.id if entry is not None else None,
        known_types=frozenset(classes) if classes is not None else None,
        extended_types=extended_types(classes) if classes is not None else frozenset(),
        # A graph policy is applied independently of the installation policy: its role
        # names are checked against the installed one (a warning, like an unknown type).
        roles=declared_roles(ctx.policy.current(None).documents[0]) if entry is not None else None,
        models=models,
    )
    names = {e.id: e.name for e in entries}
    role_warning = (
        WarningRecord(
            "policy.role_unknown",
            "the installed installation policy does not declare role(s) "
            + ", ".join(compiled.unknown_roles)
            + "; tokens can be given only declared roles, so a rule naming one matches only"
            " tokens that still hold it",
            {"roles": list(compiled.unknown_roles)},
        ),
    )
    warnings = (
        *(
            WarningRecord(
                "admin.policy_unknown_type",
                f"the graph's model has no type {name}; the limit applies once it has",
                {"graph": entry.name if entry else None, "type": name},
            )
            for name in compiled.unknown_types
        ),
        *(
            WarningRecord(
                "admin.policy_unknown_type",
                f"the model of graph {names[w.graph]} has no type {w.type}; the write grant"
                " applies once it has",
                {"graph": names[w.graph], "type": w.type},
            )
            for w in compiled.unknown_write_types
        ),
        *(
            WarningRecord(
                "admin.policy_unknown_type",
                f"the model of graph {names[r.graph]} has no type {r.type}; the read allow list"
                " lets nobody see it until it has",
                {"graph": names[r.graph], "type": r.type},
            )
            for r in compiled.unknown_read_types
        ),
        *(
            WarningRecord(
                "admin.policy_unknown_term",
                f"type {t.type} of graph {names[t.graph]} has no {_KIND_TEXT[t.kind]} {t.term};"
                " nobody can write a term that does not exist (fail closed), so the policy"
                " entry has no effect until a release adds it",
                {"graph": names[t.graph], "type": t.type, "term": t.term, "kind": t.kind},
            )
            for t in compiled.unknown_terms
        ),
        *(
            WarningRecord(
                "admin.policy_unusable_write",
                _unusable_message(u.verb, u.type, names[u.graph], u.attributes, u.file),
                {
                    "graph": names[u.graph],
                    "type": u.type,
                    "verb": u.verb,
                    "attributes": list(u.attributes),
                },
            )
            for u in compiled.unusable_writes
        ),
        *(
            WarningRecord(
                "admin.policy_unusable_write",
                f"a create of {h.type} in graph {names[h.graph]} needs a {h.relation} edge to a"
                f" type the grant's reads hide ({', '.join(h.range)}); a caller with only this"
                " grant cannot create it unless another grant (rights add up across roles) lets"
                " it see one",
                {
                    "graph": names[h.graph],
                    "type": h.type,
                    "verb": "create",
                    "relation": h.relation,
                    "range": list(h.range),
                },
            )
            for h in compiled.hidden_range_creates
        ),
        *(
            WarningRecord(
                "admin.policy_unknown_marking",
                f"the model of graph {names[m.graph]} declares no marking {m.marking}; the grant"
                " shows nobody anything until a release declares it",
                {"graph": names[m.graph], "marking": m.marking},
            )
            for m in compiled.unknown_markings
        ),
        *(
            WarningRecord(
                "admin.policy_unusable_write",
                _marked_message(w, names[w.graph]),
                {
                    "graph": names[w.graph],
                    "type": w.type,
                    "verb": w.verb,
                    "attributes": list(w.attributes),
                    "markings": list(w.markings),
                },
            )
            for w in compiled.unusable_marked_writes
        ),
    )
    ungranted = (
        _ungranted(compiled, entries, model_of) if compiled.layer == INSTALLATION_LAYER else ()
    )
    return _Source(
        compiled,
        entry,
        (*warnings, *ungranted, *(role_warning if compiled.unknown_roles else ())),
    )


def _ungranted(
    compiled: Compiled,
    entries: Sequence[GraphEntry],
    model_of: Callable[[GraphEntry], CompiledModel | None],
) -> tuple[WarningRecord, ...]:
    """``admin.marking_ungranted`` for every graph whose pinned model uses a marking that no
    grant of the installation policy ``compiled`` names (an archived graph and one whose model
    cannot be read are not checked)."""
    future = policy_set(compiled.document)
    found: list[WarningRecord] = []
    for graph in sorted(entries, key=lambda e: e.name):
        model = model_of(graph)
        if model is not None and model.used_markings():
            found += marking_warnings.ungranted_warnings(future, graph, model)
    return tuple(found)


_KIND_TEXT: Final = {
    "attribute": "attribute",
    "relation": "relation",
    "lifecycle": "lifecycle attribute",
}


def _marked_message(write: UnusableMarkedWrite, graph: str) -> str:
    attributes, markings = ", ".join(write.attributes), ", ".join(write.markings)
    plural = "s" if len(write.attributes) != 1 else ""
    head = f"{write.verb} of {write.type} in graph {graph} names or covers marked attribute{plural}"
    if write.blocks_create:
        return (
            f"{head} {attributes}, which every create must give, and the grant does not hold"
            f" {markings}; a caller must see an attribute to write it, so no caller with only"
            " this grant can create it (rights add up: another grant of the caller may hold"
            " the marking)"
        )
    return (
        f"{head} {attributes} and the grant does not hold {markings}; a caller must see an"
        " attribute to write it, so the right is dead for a caller with only this grant (rights"
        " add up: another grant of the caller may hold the marking)"
    )


# What ``graph file add`` sets itself (services/files.py): a create list must name these.
def _unusable_message(
    verb: str, type_curie: str, graph: str, attributes: Sequence[str], file: bool
) -> str:
    listed = ", ".join(attributes)
    if verb == "update":
        return (
            f"update of {type_curie} in graph {graph} lists lifecycle attribute(s) {listed}; an"
            " update never touches a lifecycle attribute, so no such write can succeed (grant"
            " transition instead)"
        )
    text = (
        f"create of {type_curie} in graph {graph} lists attributes but leaves out {listed}, which"
        " every create must give; no such write can succeed"
    )
    if file:
        text += (
            f". {type_curie} is a stored file: `graph file add` sets "
            f"{', '.join(FILE_ADD_ATTRIBUTES)} itself (and origin when a root matches), so a"
            " create list for it must name them"
        )
    return text


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


def _role_caller(
    maintainer: ActorContext, project: str | None, roles: tuple[str, ...]
) -> ActorContext:
    """A request of an app token that holds ``roles``, bound to ``project`` when one is named:
    what ``server_actor`` makes of a token record. The token's name is no valid token name,
    so no rule naming a real token matches it."""
    if project is not None and not is_valid_asserted_value(project):
        raise GraphCliError(
            "usage.invalid_argument",
            f"--project {project}: not a valid project name",
            details={"argument": "project"},
        )
    preview = TokenRecord("(roles)", "", project=project, roles=roles)
    return server_actor(preview, maintainer.client.version)


def _roles_not_declared(roles: Sequence[str]) -> WarningRecord:
    return WarningRecord(
        "policy.role_unknown",
        "the installation policy used here does not declare role(s) "
        + ", ".join(roles)
        + "; they are explained all the same, but tokens can be given only declared roles",
        {"roles": list(roles)},
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
    """Whether ``rule`` can decide on ``graph:<id>``, a type in it or one of its markings
    (``marking:<id>/<curie>``): a resource glob covers it and no ``unless`` condition on
    resources alone lifts the rule there."""
    markings = "marking:" + graph.removeprefix("graph:") + "/"
    if not any(
        glob(p, graph) or p.startswith((graph + "/", markings)) for p in strings(rule, "resource")
    ):
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


def _access(
    caller: ActorContext, entry: GraphEntry, policy: PolicySet, model: CompiledModel | None
) -> dict[str, JsonValue]:
    access: dict[str, JsonValue] = {
        "principal": caller.principal.id,
        "entrypoint": caller.client.entrypoint.value,
        "level": graph_level(caller, entry.id, policy),
        "withheld": withheld_actions(caller, entry.id, policy),
        "maintains": maintains(caller, entry.id, policy),
    }
    access.update(_allowances(entry, caller, policy, access["level"], model))
    return access


def _graph_name(entry: GraphEntry | None) -> str | None:
    return entry.name if entry is not None else None
