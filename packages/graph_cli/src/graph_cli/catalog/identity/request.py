"""The actor of a server request (contracts B2, B14.12).

The principal is the bearer token, verified by the server: ``token:<name>``.
Agent, session, device and project are asserted by the client in the
request's ``actor`` object (``source: client``), with the B2.4 grammar: a
malformed value makes its facet absent with ``actor.facet_malformed`` and
never blocks. The roles of the token are the actor's roles (policy subjects, B6.7); a
client cannot assert them. A token bound to a project verifies the project facet; a
request that asserts another project is ``access.project_mismatch``. The
entrypoint is ``server`` whatever the request says. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.catalog.tokens import TokenRecord
from graph_cli.kernel.actor import (
    ABSENT,
    ActorContext,
    Assurance,
    ClientInfo,
    Facet,
    is_valid_asserted_value,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord

SOURCE: Final = "client"
TOKEN_SOURCE: Final = "token"
# facet -> the keys a client may send for it, the first one is its id
_FACET_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "agent": ("id", "type"),
    "session": ("id", "parent"),
    "device": ("id",),
}
ACTOR_KEYS: Final = frozenset({*_FACET_KEYS, "project"})


def token_principal(token: TokenRecord) -> Facet:
    return Facet(Assurance.VERIFIED, id=f"token:{token.name}", name=token.name, source=TOKEN_SOURCE)


def request_actor(
    asserted: Mapping[str, JsonValue] | None, *, token: TokenRecord, client: ClientInfo
) -> tuple[ActorContext, tuple[WarningRecord, ...]]:
    """The request's actor. ``asserted`` is the request's ``actor`` object, already a JSON
    object; an unknown key or a facet that is not an object is ``usage.invalid_input``."""
    given = asserted or {}
    unknown = sorted(set(given) - ACTOR_KEYS)
    if unknown:
        raise _shape(f"unknown actor keys: {', '.join(unknown)}")
    warnings: list[WarningRecord] = []
    facets = {name: _facet(name, given.get(name), warnings) for name in _FACET_KEYS}
    project = _project(given.get("project"), token, warnings)
    return (
        ActorContext(
            principal=token_principal(token),
            agent=facets["agent"],
            session=facets["session"],
            device=facets["device"],
            project=project,
            client=client,
            pid=None,
            roles=token.roles,
        ),
        tuple(warnings),
    )


def _facet(name: str, value: JsonValue, warnings: list[WarningRecord]) -> Facet:
    if value is None:
        return ABSENT
    if not isinstance(value, Mapping):
        raise _shape(f"actor.{name} must be an object")
    keys = _FACET_KEYS[name]
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise _shape(f"unknown keys in actor.{name}: {', '.join(unknown)}")
    parts: dict[str, str] = {}
    for key in keys:
        part = value.get(key)
        if part is None:
            continue
        if not isinstance(part, str) or not is_valid_asserted_value(part):
            warnings.append(_malformed(name))
            return ABSENT
        parts[key] = part
    if "id" not in parts:
        return ABSENT
    return Facet(Assurance.ASSERTED, source=SOURCE, **parts)


def _project(value: JsonValue, token: TokenRecord, warnings: list[WarningRecord]) -> Facet:
    if value is not None and not isinstance(value, str):
        raise _shape("actor.project must be a string")
    if token.project is not None:
        if value is not None and value != token.project:
            raise GraphCliError(
                "access.project_mismatch",
                f"this token is bound to project {token.project!r}, not {value!r}",
                details={"project": token.project},
            )
        return Facet(Assurance.VERIFIED, id=token.project, source=TOKEN_SOURCE)
    if value is None or value == "":
        return ABSENT
    if not is_valid_asserted_value(value):
        warnings.append(_malformed("project"))
        return ABSENT
    return Facet(Assurance.ASSERTED, id=value, source=SOURCE)


def _malformed(facet: str) -> WarningRecord:
    return WarningRecord(
        "actor.facet_malformed",
        f"ignored a malformed {facet} identity value from the client",
        {"facet": facet, "source": SOURCE},
    )


def _shape(message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_input", message, details={"argument": "actor"})
