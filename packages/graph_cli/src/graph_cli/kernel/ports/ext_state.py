"""Extension state port [RES] (contracts section 2, B10). Shape reserved, implemented later."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.storage import WriteSession


@dataclass(frozen=True, slots=True)
class StateDoc:
    key: str
    value: JsonValue
    hash: str
    updated_seq: int


class ExtensionState(Protocol):
    def get(self, graph: GraphId, ns: str, key: str) -> StateDoc | None: ...

    def put(
        self,
        session: WriteSession,
        graph: GraphId,
        ns: str,
        key: str,
        value: JsonValue,
        *,
        expect_hash: str | None,
        receipt: AuditReceipt,
    ) -> StateDoc: ...
