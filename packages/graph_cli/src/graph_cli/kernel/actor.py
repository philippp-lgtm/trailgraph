"""Actor context: who is acting and how sure we are (contracts B2).

Built once per invocation by the composition root; immutable; serialized
identically into tx records and audit events (B2.9).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import opt_int, opt_str, req_obj, req_str

ASSERTED_VALUE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

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

    def to_json(self) -> dict[str, JsonValue]:
        """Every facet key is present; an absent facet is ``{"assurance": "absent"}``."""
        data: dict[str, JsonValue] = {name: getattr(self, name).to_json() for name in _FACETS}
        data["client"] = {
            "name": self.client.name,
            "version": self.client.version,
            "entrypoint": self.client.entrypoint.value,
        }
        if self.pid is not None:
            data["pid"] = self.pid
        return data

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
        )


def _assurance(value: str) -> Assurance:
    try:
        return Assurance(value)
    except ValueError:
        # Closed enum in a persisted document (V.5).
        raise GraphCliError("format.unsupported_version", f"unknown assurance {value!r}") from None
