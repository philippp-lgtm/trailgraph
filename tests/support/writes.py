"""normalize plus validate_write without a store: the pure core of the write pipeline."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from graph_cli.kernel.changeset import parse_changeset
from graph_cli.kernel.findings import Finding, sort_findings
from graph_cli.kernel.ids import EntityId, TxId
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.ontology_layer.data_validator import validate_write
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.normalize import normalize
from graph_cli.temporal_knowledge.working import WorkingState
from support.clock import FixedClock
from support.ids import SeededIdProvider
from support.snapshots import AT, DictSnapshot

TX = TxId(UUID("0192f6d4-2a10-7b41-9c3d-5e6f70819a2b"))


def judge(
    model: CompiledModel,
    *ops: Mapping[str, Any],
    base: Snapshot | None = None,
    refs: Mapping[str, EntityId] | None = None,
    prefixes: Mapping[str, str] | None = None,
    counters: Mapping[str, int] | None = None,
) -> tuple[WorkingState, list[Finding]]:
    """Normalize and validate ``ops``; temporal and data findings together, sorted."""
    base = base if base is not None else DictSnapshot()
    working = normalize(
        parse_changeset({"ops": list(ops)}).ops,
        base,
        model,
        refs=refs or {},
        new_id=SeededIdProvider(FixedClock("2026-10-15T10:00:00Z"), seed=3).new_id,
        recorded_at=AT,
        tx_id=TX,
        tx_seq=base.seq + 1,
        pin_version=model.bundle_version,
        alias_counters=counters or {},
        alias_prefixes=prefixes or {},
    )
    found = validate_write(
        model,
        base,
        working,
        touched=tuple(working.revisions),
        effects=working.effects,
        alias_allocations=working.alias_allocations,
        local_names={entity: name for name, entity in working.locals.items()},
    )
    return working, sort_findings([*working.findings, *found])


def codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]
