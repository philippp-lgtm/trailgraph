"""``graph-admin token create|list|revoke``: bearer tokens of the server API (B14.11).

A token is created with ``maint.token_create`` and revoked with
``maint.token_revoke`` on ``installation``: under the registry lock the
installation event comes first (``change.token{name, hash_prefix, ...}``),
then the token file is replaced, so ``audit verify`` can replay the events
against the file. The secret is returned once by ``create`` and never
stored, logged or audited; the file keeps its SHA-256, events a 16-hex
prefix of that. A token's scope is ``app`` (the app surface of the server)
or ``admin`` (its admin API, acting through the entrypoint ``maint``); an
admin token is never bound to a project. ``create`` warns when the
installed policy grants a new app token, as entrypoint ``server``, nothing
on any graph, or would not let a new admin token maintain. ``list`` reads
the file without a lock and records nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.effective import graph_level
from graph_cli.audit import events
from graph_cli.catalog.identity.request import TOKEN_SOURCE, token_principal
from graph_cli.catalog.tokens import (
    ADMIN_SCOPE,
    APP_SCOPE,
    SCOPES,
    TOKEN_NAME_RE,
    TokenRecord,
    new_token,
    parse_tokens,
    token_hash,
    tokens_document,
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
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.token_files import read_tokens_file, tokens_path
from graph_cli_admin.services_admin.context import AdminContext

CREATE: Final = "maint.token_create"
REVOKE: Final = "maint.token_revoke"
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
) -> Outcome:
    _check_name(name)
    if scope not in SCOPES:
        raise _usage("--scope", f"{scope!r} is not one of {', '.join(SCOPES)}", scope)
    if project is not None and not is_valid_asserted_value(project):
        raise _usage("--project", f"{project!r} is not a project name", project)
    if project is not None and scope == ADMIN_SCOPE:
        raise _usage(
            "--project", "an admin token has no project: maintenance is not project scoped", project
        )
    today = ctx.clock.now().date()
    if expires is not None:
        _check_expires(expires, today)
    args: dict[str, JsonValue] = {"name": name, "project": project, "expires": expires}
    if scope != APP_SCOPE:
        args["scope"] = scope
    meta = RequestMeta.of("token create", args)
    decisions = ctx.authorize([(CREATE, "installation")], request=meta, reason=reason)
    secret = new_token()
    record = TokenRecord(
        name, token_hash(secret), project, expires, format_timestamp(ctx.clock.now()), scope
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
                    name, record.hash, project=project, expires=expires, created=True, scope=scope
                ),
                reason=reason,
            )
        )
        registry.replace_tokens(tokens_document([*records, record]), receipt)
    if scope == ADMIN_SCOPE:
        warnings = () if _maintains(ctx, record) else (_not_maintainer(name),)
    else:
        warnings = () if _granted(ctx, record) else (_not_granted(name),)
    data: dict[str, JsonValue] = {**_listed(record, today), "token": secret}
    text = f"Token {name} created. It is shown only now; keep it secret:\n{secret}\n"
    return Outcome(data, warnings, text)


def list_tokens(ctx: AdminContext) -> Outcome:
    today = ctx.clock.now().date()
    records = parse_tokens(read_tokens_file(tokens_path(ctx.roots.data_root)))
    return Outcome({"tokens": [_listed(r, today) for r in sorted(records, key=lambda r: r.name)]})


def revoke(ctx: AdminContext, *, name: str, reason: str | None) -> Outcome:
    _check_name(name)
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


def server_actor(record: TokenRecord, version: str) -> ActorContext:
    """The actor a request with this token would have, without asserted facets: the
    entrypoint ``server`` for an app token, ``maint`` for an admin token."""
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
    )


def _granted(ctx: AdminContext, record: TokenRecord) -> bool:
    actor = server_actor(record, ctx.actor.client.version)
    graphs = ctx.catalog.list_graphs()
    if not graphs:
        decision = authorize(actor, "read.get", _ANY_GRAPH, _PROBE, ctx.policy.current(None))
        return decision.effect == "allow"
    return any(graph_level(actor, g.id, ctx.policy.current(g.id)) != "none" for g in graphs)


def _maintains(ctx: AdminContext, record: TokenRecord) -> bool:
    actor = server_actor(record, ctx.actor.client.version)
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
    }


def _check_name(name: str) -> None:
    if not TOKEN_NAME_RE.match(name):
        raise _usage("name", f"{name!r} is not a token name ({TOKEN_NAME_RE.pattern})", name)


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


def _not_maintainer(name: str) -> WarningRecord:
    return WarningRecord(
        "admin.token_not_maintainer",
        f"the installed policy does not let admin token {name} maintain; add"
        f' {{id: "token:{name}"}} to maintainers and apply the policy',
        {"name": name},
    )


def _usage(argument: str, message: str, actual: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, "actual": actual},
    )
