"""Policy source port (contracts section 2, B6.8)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId


@dataclass(frozen=True, slots=True)
class PolicySet:
    hash: str
    documents: tuple[Mapping[str, JsonValue], ...]


class PolicySource(Protocol):
    def current(self, graph: GraphId | None) -> PolicySet: ...
