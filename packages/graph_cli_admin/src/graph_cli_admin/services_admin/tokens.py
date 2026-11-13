"""``graph-admin token create|list|revoke|role add|role remove``: bearer tokens of the
server API (B14.11).

A token is created with ``maint.token_create``, revoked with ``maint.token_revoke`` and
given or relieved of roles with ``maint.token_update`` on ``installation``: under the
registry lock the installation event comes first (``change.token{name, hash_prefix, ...}``;
an update records the full role set after the change), then the token file is replaced, so
``audit verify`` can replay the events against the file. The secret is returned once by
``create`` and never stored, logged or audited; the file keeps its SHA-256, events a 16-hex
prefix of that. A token's scope is ``app`` (the app surface of the server)
or ``admin`` (its admin API, acting through the entrypoint ``maint``); an
admin token bound to a project is a project admin, which maintains only the
graphs the project's scope says ``maintain`` for. ``create`` warns when the
installed policy grants a new app token, as entrypoint ``server``, nothing
on any graph, or would not let a new admin token maintain (a project admin:
any graph). ``list`` reads the file without a lock and records nothing.

Roles (B6.7) belong to app tokens only: an admin token is refused them. A role that is
assigned (``create``, ``role add``) must be declared by the installed installation policy
(``admin.role_unknown``); removing one never needs that, so a role the policy dropped can
still be taken off. A change that changes nothing records nothing and answers
``changed: false``. ``create``, ``role add`` and ``role remove`` warn like ``create`` when the
installed policy grants the token, with its roles as they are afterwards, nothing.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.documents import declared_roles
from graph_cli.access_control.effective import graph_level, maintains
from graph_cli.audit import events
from graph_cli.catalog.identity.request import TOKEN_SOURCE, token_principal
from graph_cli.catalog.tokens import (
    ADMIN_SCOPE,
    APP_SCOPE,
    SCOPES,
    TOKEN_NAME_RE,
    TOKEN_PREFIX,
    TokenRecord,
    new_token,
    parse_tokens,
    token_hash,
    tokens_document,
)
from graph_cli.kernel.actor import (
    ABSENT,
    MAX_ROLES,
    ActorContext,
    Assurance,
    ClientInfo,
    Entrypoint,
    Facet,
    is_valid_asserted_value,
    is_valid_role_name,
)
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.token_files import read_tokens_file, tokens_path
from graph_cli_admin.services_admin.context import AdminContext

CREATE: Final = "maint.token_create"
REVOKE: Final = "maint.token_revoke"
UPDATE: Final = "maint.token_update"
SERVER_PROGRAM: Final = "graph-server"
_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROBE: Final = AuthzContext(op_count=1, has_reason=True, dry_run=False)
# A graph resource no installation has: what a policy grants on every graph.
_ANY_GRAPH: Final = "graph:00000000-0000-7000-8000-000000000000"


@dataclass(frozen=True, slots=True)
class Outcome:
    data: dict[str, JsonValue]
    warnings: tuple[WarningRecord, ...] = ()
    text: str | None = None


def create(
    ctx: AdminContext,
    *,
    name: str,
    project: str | None,
    expires: str | None,
    reason: str | None,
    scope: str = APP_SCOPE,
    roles: Sequence[str] = (),
) -> Outcome:
    check_name(name)
    if scope not in SCOPES:
        raise _usage("--scope", f"{scope!r} is not one of {', '.join(SCOPES)}", scope)
    held = check_roles(roles, "--role", scope)
    if project is not None and not is_valid_asserted_value(project):
        raise _usage("--project", f"{project!r} is not a project name", project)
    today = ctx.clock.now().date()
    if expires is not None:
        _check_expires(expires, today)
    args: dict[str, JsonValue] = {"name": name, "project": project, "expires": expires}
    if scope != APP_SCOPE:
        args["scope"] = scope
    if held:
        args["roles"] = list(held)
    meta = RequestMeta.of("token create", args)
    decisions = ctx.authorize([(CREATE, "installation")], request=meta, reason=reason)
    check_declared(ctx, held)
    secret = new_token()
    record = TokenRecord(
        name,
        token_hash(secret),
        project,
        expires,
        format_timestamp(ctx.clock.now()),
        scope,
        held,
    )
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_tokens(registry.read_tokens())
        if any(r.name == name for r in records):
            raise GraphCliError(
                "admin.token_exists", f"a token named {name} exists", details={"name": name}
            )
        receipt = ctx.audit.append_installation(
            events.maintenance(
                actor=ctx.actor,
                decisions=decisions,
                request=meta,
                ts=ctx.clock.now(),
                event_id=ctx.event_id(),
                env=ctx.env,
                change=events.token_change(
                    name,
                    record.hash,
                    project=project,
                    expires=expires,
                    created=True,
                    scope=scope,
                    roles=held,
                ),
                reason=reason,
            )
        )
        registry.replace_tokens(tokens_document([*records, record]), receipt)
    if scope == ADMIN_SCOPE:
        warnings = () if maintains_any(ctx, record) else (_not_maintainer(record),)
    else:
        warnings = () if granted(ctx, record) else (_not_granted(name),)
    data: dict[str, JsonValue] = {**_listed(record, today), "token": secret}
    text = f"Token {name} created. It is shown only now; keep it secret:\n{secret}\n"
    return Outcome(data, warnings, text)


def list_tokens(ctx: AdminContext, *, role: str | None = None) -> Outcome:
    """Every token, or those holding ``role`` (a role the policy no longer declares too)."""
    if role is not None:
        check_roles([role], "--role")
    today = ctx.clock.now().date()
    records = parse_tokens(read_tokens_file(tokens_path(ctx.roots.data_root)))
    shown = [r for r in records if role is None or role in r.roles]
    return Outcome({"tokens": [_listed(r, today) for r in sorted(shown, key=lambda r: r.name)]})


def revoke(ctx: AdminContext, *, name: str, reason: str | None) -> Outcome:
    check_name(name)
    meta = RequestMeta.of("token revoke", {"name": name})
    decisions = ctx.authorize([(REVOKE, "installation")], request=meta, reason=reason)
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_tokens(registry.read_tokens())
        found = next((r for r in records if r.name == name), None)
        if found is None:
            raise GraphCliError(
                "admin.token_not_found", f"no token named {name}", details={"name": name}
            )
        receipt = ctx.audit.append_installation(
            events.maintenance(
                actor=ctx.actor,
                decisions=decisions,
                request=meta,
                ts=ctx.clock.now(),
                event_id=ctx.event_id(),
                env=ctx.env,
                change=events.token_change(name, found.hash, created=False),
                reason=reason,
            )
        )
        registry.replace_tokens(tokens_document([r for r in records if r is not found]), receipt)
    return Outcome({"name": name, "revoked": True, "hash_prefix": found.hash_prefix})


def update_roles(
    ctx: AdminContext,
    *,
    name: str,
    roles: Sequence[str],
    add: bool,
    reason: str | None,
) -> Outcome:
    """Add roles to or remove them from an app token. The installation event holds the full
    role set after the change; a change that changes nothing writes none. Only roles that
    would be newly assigned must be declared."""
    check_name(name)
    named = check_roles(roles, "role")
    meta = RequestMeta.of(
        f"token role {'add' if add else 'remove'}", {"name": name, "roles": list(named)}
    )
    decisions = ctx.authorize([(UPDATE, "installation")], request=meta, reason=reason)
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_tokens(registry.read_tokens())
        found = next((r for r in records if r.name == name), None)
        if found is None:
            raise GraphCliError(
                "admin.token_not_found", f"no token named {name}", details={"name": name}
            )
        if found.scope == ADMIN_SCOPE:
            raise _usage("name", f"{name} is an admin token; roles are for app tokens", name)
        after = (
            tuple(sorted({*found.roles, *named}))
            if add
            else tuple(r for r in found.roles if r not in named)
        )
        updated = replace(found, roles=after)
        changed = after != found.roles
        if changed:
            if add:
                check_declared(ctx, [r for r in named if r not in found.roles])
            check_count(after, "role")
            receipt = ctx.audit.append_installation(
                events.maintenance(
                    actor=ctx.actor,
                    decisions=decisions,
                    request=meta,
                    ts=ctx.clock.now(),
                    event_id=ctx.event_id(),
                    env=ctx.env,
                    change=events.token_update_change(name, found.hash, after),
                    reason=reason,
                )
            )
            registry.replace_tokens(
                tokens_document([updated if r is found else r for r in records]), receipt
            )
    warnings = () if granted(ctx, updated) else (_not_granted(name),)
    data: dict[str, JsonValue] = {
        "name": name,
        "roles": list(after),
        "changed": changed,
        "hash_prefix": found.hash_prefix,
    }
    return Outcome(data, warnings)


def server_actor(record: TokenRecord, version: str) -> ActorContext:
    """The actor a request with this token would have, without asserted facets: the
    entrypoint ``server`` for an app token, ``maint`` for an admin token, the token's roles."""
    project = (
        Facet(Assurance.VERIFIED, id=record.project, source=TOKEN_SOURCE)
        if record.project is not None
        else ABSENT
    )
    return ActorContext(
        principal=token_principal(record),
        agent=ABSENT,
        session=ABSENT,
        device=ABSENT,
        project=project,
        client=ClientInfo(
            SERVER_PROGRAM,
            version,
            Entrypoint.MAINT if record.scope == ADMIN_SCOPE else Entrypoint.SERVER,
        ),
        pid=None,
        roles=record.roles,
    )


def granted(ctx: AdminContext, record: TokenRecord) -> bool:
    """Whether the installed policy grants a request with this token a level on any graph."""
    actor = server_actor(record, ctx.actor.client.version)
    graphs = ctx.catalog.list_graphs()
    if not graphs:
        decision = authorize(actor, "read.get", _ANY_GRAPH, _PROBE, ctx.policy.current(None))
        return decision.effect == "allow"
    return any(graph_level(actor, g.id, ctx.policy.current(g.id)) != "none" for g in graphs)


def maintains_any(ctx: AdminContext, record: TokenRecord) -> bool:
    """Whether an admin token may maintain: a project admin any graph, else the installation."""
    actor = server_actor(record, ctx.actor.client.version)
    if record.project is not None:
        return any(
            maintains(actor, g.id, ctx.policy.current(g.id)) for g in ctx.catalog.list_graphs()
        )
    decision = authorize(actor, CREATE, "installation", _PROBE, ctx.policy.current(None))
    return decision.effect == "allow"


def _listed(record: TokenRecord, today: date) -> dict[str, JsonValue]:
    return {
        "name": record.name,
        "scope": record.scope,
        "project": record.project,
        "expires": record.expires,
        "expired": not record.valid_on(today),
        "created_at": record.created_at,
        "hash_prefix": record.hash_prefix,
        "roles": list(record.roles),
    }


def check_roles(roles: Sequence[str], argument: str, scope: str = APP_SCOPE) -> tuple[str, ...]:
    """Role names as the tokens keep them (sorted, unique): each a valid name, at most
    ``MAX_ROLES``, none for an admin token. A token given by mistake is not echoed."""
    if roles and scope == ADMIN_SCOPE:
        raise _usage(argument, "roles are for app tokens; an admin token has none", None)
    for role in roles:
        if role.startswith(TOKEN_PREFIX):
            raise _usage(argument, "this is a token, not a role name", None)
        if not is_valid_role_name(role):
            raise _usage(
                argument, f"{role!r} is not a role name (lowercase letters, digits, _)", role
            )
    held = tuple(sorted(set(roles)))
    check_count(held, argument)
    return held


def check_count(roles: Sequence[str], argument: str) -> None:
    if len(roles) > MAX_ROLES:
        raise _usage(argument, f"{len(roles)} roles; a token holds at most {MAX_ROLES}", len(roles))


def check_declared(ctx: AdminContext, roles: Sequence[str]) -> None:
    """``admin.role_unknown`` for the first role the installed installation policy does not
    declare, with the declared ones and the closest of them."""
    known = sorted(declared_roles(ctx.policy.current(None).documents[0]))
    for role in roles:
        if role not in known:
            raise GraphCliError(
                "admin.role_unknown",
                f"the installed installation policy does not declare role {role}"
                + (f" (declared: {', '.join(known)})" if known else " (it declares no roles)")
                + "; declare it under roles and apply the policy",
                details={
                    "role": role,
                    "known": list(known),
                    "did_you_mean": list(difflib.get_close_matches(role, known, n=5)),
                },
            )


def check_name(name: str, argument: str = "name") -> None:
    """A token name; a token's secret given by mistake is refused without echoing it."""
    if name.startswith(TOKEN_PREFIX):
        raise _usage(argument, "this is a token, not a token name; pass the name", None)
    if not TOKEN_NAME_RE.match(name):
        raise _usage(argument, f"{name!r} is not a token name ({TOKEN_NAME_RE.pattern})", name)


def _check_expires(text: str, today: date) -> None:
    try:
        day = date.fromisoformat(text) if _DATE_RE.match(text) else None
    except ValueError:
        day = None
    if day is None:
        raise _usage("--expires", f"{text!r} is not a date (YYYY-MM-DD)", text)
    if day < today:
        raise _usage("--expires", f"{text} is in the past", text)


def _not_granted(name: str) -> WarningRecord:
    return WarningRecord(
        "admin.token_not_granted",
        f"the installed policy grants token {name} nothing on any graph; add a grant with"
        " entrypoint: server and apply the policy",
        {"name": name},
    )


def _not_maintainer(record: TokenRecord) -> WarningRecord:
    name, project = record.name, record.project
    if project is not None:
        message = (
            f"the installed policy lets admin token {name} of project {project} maintain no"
            f" graph; give the project maintain for a graph in scopes.{project} and apply the"
            " policy"
        )
    else:
        message = (
            f"the installed policy does not let admin token {name} maintain; add"
            f' {{id: "token:{name}"}} to maintainers and apply the policy'
        )
    return WarningRecord("admin.token_not_maintainer", message, {"name": name})


def _usage(argument: str, message: str, actual: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, "actual": actual},
    )
