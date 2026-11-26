"""``graph-admin enroll create|list|revoke`` and the exchange behind ``POST /v1/enroll`` (B14.21).

An enrollment code lets one client get one token bound to a project without
anyone handing it a token: a maintainer creates the code (``maint.token_create``
on ``installation``: whoever may create tokens may let a client create one),
passes it on out of band, and the client exchanges it once (``graph connect``).
A code always names a project, so it never yields an unbound token: the
maintainer's own token stays where it is. It may carry roles (declared by the installed
installation policy, app tokens only) that its token gets. Under the registry lock the
installation event comes first (``change.enrollment``), then the enrollment
file is replaced, so ``audit verify`` can replay the events against it. The
code is returned once by ``create`` and never stored, logged or audited.

The exchange needs no token: the code is the authorization, recorded as the
decision ``enrollment:<id>`` of a ``maint.token_create`` event whose actor is
the enrollment (principal ``enrollment:<id>``, project verified). It finds the
code without a lock first, so a wrong code takes no lock and writes nothing.
Under the lock it checks the code again and refuses one the installation
stream already used or revoked (an exchange cut short before the file
changed). The token is named ``<project>.<label>``, the client choosing only
the label, and it gets the roles of the code; a name that is taken or that a
policy rule names leaves the code unused. The code leaves the file before the token is
written. A check (``check: true``) only says what the code is for; nothing is written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Final

from graph_cli.access_control.effective import graph_level, maintains
from graph_cli.audit import events
from graph_cli.catalog.enrollments import (
    DEFAULT_LIFETIME,
    ID_RE,
    EnrollmentRecord,
    code_hash,
    default_token_name,
    enrollments_document,
    find,
    new_code,
    new_id,
    parse_enrollments,
    parse_lifetime,
)
from graph_cli.catalog.tokens import (
    ADMIN_SCOPE,
    SCOPES,
    TokenRecord,
    new_token,
    parse_tokens,
    token_hash,
    tokens_document,
)
from graph_cli.kernel.actions import Action
from graph_cli.kernel.actor import (
    ABSENT,
    ActorContext,
    Assurance,
    ClientInfo,
    Facet,
    is_valid_asserted_value,
)
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.token_files import enrollments_path, read_tokens_file
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.tokens import (
    CREATE,
    REVOKE,
    Outcome,
    check_declared,
    check_roles,
    granted,
    maintains_any,
    not_catalog_only,
    server_actor,
)

ENROLLMENT_SOURCE: Final = "enrollment"
EXCHANGE: Final = "enroll exchange"


def create(
    ctx: AdminContext,
    *,
    project: str,
    scope: str,
    expires_in: str | None,
    reason: str | None,
    roles: Sequence[str] = (),
) -> Outcome:
    if not is_valid_asserted_value(project):
        raise _usage("--project", f"{project!r} is not a project name", project)
    if scope not in SCOPES:
        raise _usage("--scope", f"{scope!r} is not one of {', '.join(SCOPES)}", scope)
    lifetime = DEFAULT_LIFETIME if expires_in is None else parse_lifetime(expires_in)
    if lifetime is None:
        raise _usage(
            "--expires-in",
            f"{expires_in!r} is not a duration of at most a day (<n>m or <n>h)",
            expires_in,
        )
    held = check_roles(roles, "--role", scope)
    now = ctx.clock.now()
    args: dict[str, JsonValue] = {"project": project, "scope": scope, "expires_in": expires_in}
    if held:
        args["roles"] = list(held)
    meta = RequestMeta.of("enroll create", args)
    decisions = ctx.authorize([(CREATE, "installation")], request=meta, reason=reason)
    check_declared(ctx, held)
    code = new_code()
    record = EnrollmentRecord(
        new_id(),
        code_hash(code),
        project,
        scope,
        format_timestamp(now + lifetime),
        format_timestamp(now),
        held,
    )
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_enrollments(registry.read_enrollments())
        while any(r.id == record.id for r in records):
            record = replace(record, id=new_id())
        change = events.enrollment_change(
            record.id,
            record.hash,
            project=project,
            scope=scope,
            expires_at=record.expires_at,
            roles=record.roles,
        )
        receipt = ctx.audit.append_installation(
            events.maintenance(
                actor=ctx.actor,
                decisions=decisions,
                request=meta,
                ts=ctx.clock.now(),
                event_id=ctx.event_id(),
                env=ctx.env,
                change={"enrollment": change},
                reason=reason,
            )
        )
        registry.replace_enrollments(enrollments_document([*records, record]), receipt)
    probe = _probe(record)
    usable = maintains_any(ctx, probe) if scope == ADMIN_SCOPE else granted(ctx, probe)
    warnings = () if usable else (_unusable(project, scope),)
    if scope != ADMIN_SCOPE:
        warnings = (*warnings, *not_catalog_only(ctx, probe))
    data: dict[str, JsonValue] = {**_listed(record, now), "code": code}
    text = (
        f"Enrollment {record.id} for project {project} (scope {scope}) created, valid until"
        f" {record.expires_at}. The code is shown only now and works once; give it to the"
        f" person who connects, who runs:\n  graph connect <endpoint> --code {code}\n"
    )
    return Outcome(data, warnings, text)


def list_enrollments(ctx: AdminContext) -> Outcome:
    now = ctx.clock.now()
    records = parse_enrollments(read_tokens_file(enrollments_path(ctx.roots.data_root)))
    return Outcome({"enrollments": [_listed(r, now) for r in sorted(records, key=_order)]})


def revoke(ctx: AdminContext, *, enrollment_id: str, reason: str | None) -> Outcome:
    if not ID_RE.match(enrollment_id):
        shown = None if enrollment_id.startswith("gce_") else enrollment_id
        raise _usage("id", "not an enrollment id (12 hex digits; see enroll list)", shown)
    meta = RequestMeta.of("enroll revoke", {"id": enrollment_id})
    decisions = ctx.authorize([(REVOKE, "installation")], request=meta, reason=reason)
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_enrollments(registry.read_enrollments())
        found = next((r for r in records if r.id == enrollment_id), None)
        if found is None:
            raise GraphCliError(
                "admin.enrollment_not_found",
                f"no enrollment {enrollment_id} (used, revoked or never created)",
                details={"id": enrollment_id},
            )
        receipt = ctx.audit.append_installation(
            events.maintenance(
                actor=ctx.actor,
                decisions=decisions,
                request=meta,
                ts=ctx.clock.now(),
                event_id=ctx.event_id(),
                env=ctx.env,
                change={"enrollment": events.enrollment_change(found.id, found.hash)},
                reason=reason,
            )
        )
        remaining = [r for r in records if r is not found]
        registry.replace_enrollments(enrollments_document(remaining), receipt)
    return Outcome({"id": enrollment_id, "revoked": True, "hash_prefix": found.hash_prefix})


def exchange(
    ctx: AdminContext, *, code: str, label: str | None, check: bool
) -> dict[str, JsonValue]:
    """What ``POST /v1/enroll`` answers: with ``check`` the code's project, scope, expiry
    and the graphs a token of it would use; else the new token (once), its name and the
    graphs it can use. The token is named ``<project>.<label>``: the client picks only the
    label, so it can never take over a name a policy rule grants something to."""
    record = _usable(
        parse_enrollments(read_tokens_file(enrollments_path(ctx.roots.data_root))),
        code,
        ctx.clock.now(),
    )
    if check:
        graphs = _usable_graphs(ctx, _probe(record))
        return {"enrollment": {**_described(record), "graphs": graphs}}
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        records = parse_enrollments(registry.read_enrollments())
        record = _usable(records, code, ctx.clock.now())
        if _ended(ctx, record):
            raise refused()  # an exchange or revoke cut short before the file changed
        name = default_token_name(record.project, label or record.id)
        tokens = parse_tokens(registry.read_tokens())
        _check_name(ctx, name, tokens)
        secret = new_token()
        token = TokenRecord(
            name,
            token_hash(secret),
            project=record.project,
            created_at=format_timestamp(ctx.clock.now()),
            scope=record.scope,
            roles=record.roles,
        )
        decision = Decision(
            "allow",
            Action(CREATE),
            "installation",
            rule_id=f"enrollment:{record.id}",
            policy_hash=ctx.policy.current(None).hash,
        )
        change = events.token_change(
            name,
            token.hash,
            project=record.project,
            expires=None,
            created=True,
            scope=record.scope,
            roles=record.roles,
        )
        receipt = ctx.audit.append_installation(
            events.maintenance(
                actor=enrollment_actor(record, ctx.actor.client),
                decisions=(decision,),
                request=RequestMeta.of(EXCHANGE, {"enrollment": record.id, "name": name}),
                ts=ctx.clock.now(),
                event_id=ctx.event_id(),
                env=ctx.env,
                change={
                    **change,
                    "enrollment": events.enrollment_change(record.id, record.hash),
                },
            )
        )
        remaining = [r for r in records if r.id != record.id]
        registry.replace_enrollments(enrollments_document(remaining), receipt)
        registry.replace_tokens(tokens_document([*tokens, token]), receipt)
    return {
        "token": secret,
        "name": name,
        "project": record.project,
        "scope": record.scope,
        "graphs": _usable_graphs(ctx, token),
    }


def _check_name(ctx: AdminContext, name: str, tokens: tuple[TokenRecord, ...]) -> None:
    """A free token name that no installed policy rule names (as ``token:<name>`` or as a
    principal name): a new token never inherits what was meant for another. Else the
    code stays unused and the client may pick another label."""
    taken = any(t.name == name for t in tokens)
    if not taken:
        documents = list(ctx.policy.current(None).documents)
        for entry in ctx.catalog.list_graphs():
            documents += ctx.policy.current(entry.id).documents
        taken = any(_names(document, name) for document in documents)
    if taken:
        raise GraphCliError(
            "usage.invalid_argument",
            f"label: the token name {name} is taken or named by the policy; choose another"
            " label (the code is still unused)",
            details={"argument": "label", "actual": name},
        )


def _names(value: JsonValue, name: str) -> bool:
    """Whether a compiled policy document has a rule on the principal of token ``name``."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            wanted = {"principal_id": f"token:{name}", "principal_name": name}.get(str(key))
            found = item if isinstance(item, list) else [item]
            if wanted is not None and wanted in found:
                return True
            if _names(item, name):
                return True
    elif isinstance(value, list):
        return any(_names(item, name) for item in value)
    return False


def _ended(ctx: AdminContext, record: EnrollmentRecord) -> bool:
    """Whether the installation stream already used or revoked this code: an exchange or a
    revoke cut short between its event and the file leaves the code listed. Read under the
    registry lock; exchanges are rare, so the scan is affordable."""
    marker = record.id.encode()
    for line in ctx.audit.read_lines(INSTALLATION_STREAM):
        if marker not in line.data:
            continue
        try:
            event = json.loads(line.data)
        except ValueError:
            continue
        change = event.get("change") if isinstance(event, dict) else None
        if not isinstance(change, dict):
            continue
        entry = change.get("enrollment")
        if not isinstance(entry, dict) or entry.get("id") != record.id:
            continue
        if event.get("actions") == [REVOKE] or "token" in change:
            return True
    return False


def enrollment_actor(record: EnrollmentRecord, client: ClientInfo) -> ActorContext:
    """Who exchanges a code: the enrollment itself, its project verified by the code."""
    return ActorContext(
        principal=Facet(Assurance.VERIFIED, id=f"enrollment:{record.id}", source=ENROLLMENT_SOURCE),
        agent=ABSENT,
        session=ABSENT,
        device=ABSENT,
        project=Facet(Assurance.VERIFIED, id=record.project, source=ENROLLMENT_SOURCE),
        client=client,
        pid=None,
    )


def _usable(records: tuple[EnrollmentRecord, ...], code: str, now: datetime) -> EnrollmentRecord:
    """The record of a listed code that has not expired; else one answer for an unknown,
    used, revoked, expired or malformed code (B14.3: the message does not say which)."""
    found = find(records, code)
    if found is None or not found.valid_at(now):
        raise refused()
    return found


def refused() -> GraphCliError:
    """The one answer to a code that cannot be used."""
    return GraphCliError(
        "remote.unauthorized",
        "the server does not accept this enrollment code (unknown, used, revoked or"
        " expired); ask the maintainer for a new one",
    )


def _usable_graphs(ctx: AdminContext, token: TokenRecord) -> list[JsonValue]:
    """The graphs a request with ``token`` can use: an app token's visible graphs, an admin
    token's maintained ones."""
    actor = server_actor(token, ctx.actor.client.version)
    names: list[JsonValue] = []
    for entry in sorted(ctx.catalog.list_graphs(), key=lambda g: g.name):
        policy = ctx.policy.current(entry.id)
        if token.scope == ADMIN_SCOPE:
            usable = maintains(actor, entry.id, policy)
        else:
            usable = graph_level(actor, entry.id, policy) != "none"
        if usable:
            names.append(entry.name)
    return names


def _probe(record: EnrollmentRecord) -> TokenRecord:
    """A token as the code would yield it, to ask the policy what it would get."""
    name = default_token_name(record.project, record.id)
    return TokenRecord(
        name, record.hash, project=record.project, scope=record.scope, roles=record.roles
    )


def _described(record: EnrollmentRecord) -> dict[str, JsonValue]:
    return {
        "id": record.id,
        "project": record.project,
        "scope": record.scope,
        "expires_at": record.expires_at,
    }


def _listed(record: EnrollmentRecord, now: datetime) -> dict[str, JsonValue]:
    return {
        **_described(record),
        "expired": not record.valid_at(now),
        "created_at": record.created_at,
        "hash_prefix": record.hash_prefix,
        "roles": list(record.roles),
    }


def _order(record: EnrollmentRecord) -> tuple[str, str]:
    return record.created_at, record.id


def _unusable(project: str, scope: str) -> WarningRecord:
    if scope == ADMIN_SCOPE:
        return WarningRecord(
            "admin.token_not_maintainer",
            f"the installed policy lets admin tokens of project {project} maintain no graph;"
            f" give the project maintain for a graph in scopes.{project} and apply the policy",
            {"project": project},
        )
    return WarningRecord(
        "admin.token_not_granted",
        f"the installed policy grants tokens of project {project} nothing on any graph; add"
        " a grant with entrypoint: server (or for the project) and apply the policy",
        {"project": project},
    )


def _usage(argument: str, message: str, actual: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, "actual": actual},
    )
