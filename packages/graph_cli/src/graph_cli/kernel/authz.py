"""Authorization decision types (contracts B6). The decision logic lives in access_control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from graph_cli.kernel.actions import Action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TxId
from graph_cli.kernel.records import OntologyPin, RequestMeta, Revision


@dataclass(frozen=True, slots=True)
class Obligations:
    require_reason: bool = False
    max_batch: int | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"require_reason": self.require_reason}
        if self.max_batch is not None:
            data["max_batch"] = self.max_batch
        return data


@dataclass(frozen=True, slots=True)
class Decision:
    effect: Literal["allow", "deny"]
    action: Action
    resource: str  # installation | graph:<id> | graph:<id>/type:<curie>  (B6.9)
    rule_id: str
    policy_hash: str
    obligations: Obligations = field(default_factory=Obligations)
    code: str | None = None  # set iff deny

    def __post_init__(self) -> None:
        if (self.effect == "deny") != (self.code is not None):
            raise GraphCliError("internal.error", "a decision has a code iff it denies")

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "effect": self.effect,
            "action": self.action,
            "resource": self.resource,
            "rule_id": self.rule_id,
            "policy_hash": self.policy_hash,
            "obligations": self.obligations.to_json(),
        }
        if self.code is not None:
            data["code"] = self.code
        return data


@dataclass(frozen=True, slots=True)
class AuthzContext:
    op_count: int
    has_reason: bool
    dry_run: bool


@dataclass(frozen=True, slots=True)
class AuthorizedTx:
    """Everything storage needs to commit one tx. Built only by ``services.write``."""

    graph_id: GraphId
    tx_id: TxId
    event_id: EventId
    seq: int
    recorded_at: datetime
    actor: ActorContext
    decisions: tuple[Decision, ...]
    changeset: Mapping[str, JsonValue]
    changeset_hash: str
    resolution: Mapping[str, JsonValue]
    revisions: tuple[Revision, ...]
    alias_allocations: tuple[tuple[EntityId, str], ...]
    ontology: OntologyPin
    reason: str | None
    idempotency_key: str | None
    request: RequestMeta
    blobs: tuple[str, ...] = ()  # sorted hashes of every blob value in ``revisions``

    def allowed_actions(self) -> frozenset[Action]:
        return frozenset(d.action for d in self.decisions if d.effect == "allow")
