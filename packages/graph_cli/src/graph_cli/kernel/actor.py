"""Actor context: who is acting and how sure we are (contracts B2).

Built once per invocation by the composition root; immutable; serialized
identically into tx records and audit events (B2.9). ``roles`` are the roles
of the server token a request came with (policy subjects, B6.7); every other
actor has none, and an actor without roles serializes exactly as before.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import (
    JsonShapeError,
    opt_int,
    opt_list,
    opt_str,
    req_obj,
    req_str,
)

ASSERTED_VALUE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ROLE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")

_FACET_KEYS: Final = ("id", "name", "type", "parent", "source")
_FACETS: Final = ("principal", "agent", "session", "device", "project")


class Assurance(StrEnum):
    VERIFIED = "verified"
    ASSERTED = "asserted"
    ABSENT = "absent"


class Entrypoint(StrEnum):
    APP = "app"
    MAINT = "maint"
    SERVER = "server"  # graph-server: the app surface over HTTP (B14)


def is_valid_asserted_value(value: str) -> bool:
    """B2.4 grammar for asserted facet values."""
    return ASSERTED_VALUE_RE.match(value) is not None


def is_valid_role_name(value: str) -> bool:
    """Grammar of a role name (B6.7): lowercase letters, digits and ``_``, up to 64."""
    return ROLE_NAME_RE.match(value) is not None


@dataclass(frozen=True, slots=True)
class Facet:
    """Generic identity facet; unused fields stay ``None``."""

    assurance: Assurance
    id: str | None = None
    name: str | None = None
    type: str | None = None
    parent: str | None = None
    source: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        if self.assurance is Assurance.ABSENT:
            return {"assurance": Assurance.ABSENT.value}
        data: dict[str, JsonValue] = {"assurance": self.assurance.value}
        for key in _FACET_KEYS:
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, JsonValue], path: str) -> Facet:
        return cls(
            assurance=_assurance(req_str(data, "assurance", path)),
            id=opt_str(data, "id", path),
            name=opt_str(data, "name", path),
            type=opt_str(data, "type", path),
            parent=opt_str(data, "parent", path),
            source=opt_str(data, "source", path),
        )


ABSENT: Final = Facet(Assurance.ABSENT)


@dataclass(frozen=True, slots=True)
class ClientInfo:
    name: str
    version: str
    entrypoint: Entrypoint


@dataclass(frozen=True, slots=True)
class ActorContext:
    principal: Facet
    agent: Facet
    session: Facet
    device: Facet
    project: Facet
    client: ClientInfo
    pid: int | None
    roles: tuple[str, ...] = ()  # sorted, unique

    def to_json(self) -> dict[str, JsonValue]:
        """Every facet key is present; an absent facet is ``{"assurance": "absent"}``.
        ``roles`` is present only when the actor holds any."""
        data: dict[str, JsonValue] = {name: getattr(self, name).to_json() for name in _FACETS}
        data["client"] = {
            "name": self.client.name,
            "version": self.client.version,
            "entrypoint": self.client.entrypoint.value,
        }
        if self.pid is not None:
            data["pid"] = self.pid
        if self.roles:
            data["roles"] = list(self.roles)
        return data

    def label(self) -> str:
        """Who acts, in one word: the agent if the harness named one, else the principal,
        else the entrypoint."""
        for facet in (self.agent, self.principal):
            for value in (facet.name, facet.id, facet.type):
                if value:
                    return value
        return self.client.entrypoint.value

    def session_key(self) -> str | None:
        """The session one agent's commands share: its id, else its name."""
        return self.session.id or self.session.name

    @classmethod
    def from_json(cls, data: Mapping[str, JsonValue]) -> ActorContext:
        """Parse a persisted actor. Shape errors raise ``JsonShapeError``."""

        def facet(name: str) -> Facet:
            return Facet.from_json(req_obj(data, name), f"{name}.")

        client = req_obj(data, "client")
        entrypoint_text = req_str(client, "entrypoint", "client.")
        try:
            entrypoint = Entrypoint(entrypoint_text)
        except ValueError:
            raise GraphCliError(
                "format.unsupported_version", "unknown entrypoint in actor"
            ) from None
        return cls(
            client=ClientInfo(
                name=req_str(client, "name", "client."),
                version=req_str(client, "version", "client."),
                entrypoint=entrypoint,
            ),
            pid=opt_int(data, "pid"),
            principal=facet("principal"),
            agent=facet("agent"),
            session=facet("session"),
            device=facet("device"),
            project=facet("project"),
            roles=_roles(data),
        )


def _assurance(value: str) -> Assurance:
    try:
        return Assurance(value)
    except ValueError:
        # Closed enum in a persisted document (V.5).
        raise GraphCliError("format.unsupported_version", f"unknown assurance {value!r}") from None


def _roles(data: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The optional ``roles`` list: role names, sorted and unique as written."""
    names: list[str] = []
    for item in opt_list(data, "roles"):
        if not isinstance(item, str) or not is_valid_role_name(item):
            raise JsonShapeError(f"roles: {item!r} is not a role name")
        names.append(item)
    if names != sorted(set(names)):
        raise JsonShapeError("roles: must be sorted and unique")
    return tuple(names)
