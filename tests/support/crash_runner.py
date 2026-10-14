"""Crash runner (K tests, contracts K.d): a child process commits with a SIGKILL fault hook.

The parent builds the ``AuthorizedTx`` and the audit draft and passes them as
JSON, so the child needs no ontology and no builder. The child wires a
``FaultHook`` that sends ``SIGKILL`` to its own process at the named point.
"""

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import TxRecord, tx_from_json, tx_to_json
from support.procs import spawn


def tx_payload(tx: AuthorizedTx) -> dict[str, JsonValue]:
    record = TxRecord(
        graph_id=tx.graph_id,
        tx_id=tx.tx_id,
        seq=tx.seq,
        recorded_at=tx.recorded_at,
        event_id=tx.event_id,
        actor=tx.actor,
        ontology=tx.ontology,
        request=tx.request,
        reason=tx.reason,
        idempotency_key=tx.idempotency_key,
        changeset=tx.changeset,
        changeset_hash=tx.changeset_hash,
        resolution=tx.resolution,
        revisions=tx.revisions,
        alias_allocations=tx.alias_allocations,
        prev_hash="",
        hash="",
    )
    return tx_to_json(record)


def tx_from_payload(data: Mapping[str, JsonValue]) -> AuthorizedTx:
    record = tx_from_json(data)
    return AuthorizedTx(
        graph_id=record.graph_id,
        tx_id=record.tx_id,
        event_id=record.event_id,
        seq=record.seq,
        recorded_at=record.recorded_at,
        actor=record.actor,
        decisions=(),
        changeset=record.changeset,
        changeset_hash=record.changeset_hash,
        resolution=record.resolution,
        revisions=record.revisions,
        alias_allocations=record.alias_allocations,
        ontology=record.ontology,
        reason=record.reason,
        idempotency_key=record.idempotency_key,
        request=record.request,
    )


def run_child(
    graph_dir: Path,
    graph_id: str,
    point: str,
    *,
    tx: AuthorizedTx | None = None,
    audit_body: Mapping[str, JsonValue] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one commit (or, without ``tx``, one empty session) and return the outcome."""
    args: dict[str, Any] = {"graph_dir": str(graph_dir), "graph_id": graph_id, "point": point}
    if tx is not None:
        args.update(tx=tx_payload(tx), audit=dict(audit_body or {}))
    proc = spawn("crash", args)
    out, err = proc.communicate(timeout=60)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
