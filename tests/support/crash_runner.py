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
from graph_cli.storage.s3.settings import ObjectStoreSettings
from support.migrations import allow_migrate
from support.procs import spawn
from support.s3_graphs import settings_spec
from support.txbuilder import allow_ops


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
        blobs=tx.blobs,
    )
    return tx_to_json(record)


def _ops(changeset: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    ops = changeset.get("ops", [])
    assert isinstance(ops, list)
    return [op for op in ops if isinstance(op, Mapping)]


def tx_from_payload(data: Mapping[str, JsonValue]) -> AuthorizedTx:
    record = tx_from_json(data)
    return AuthorizedTx(
        graph_id=record.graph_id,
        tx_id=record.tx_id,
        event_id=record.event_id,
        seq=record.seq,
        recorded_at=record.recorded_at,
        actor=record.actor,
        decisions=(
            allow_migrate(record.graph_id)
            if "migration" in record.changeset
            else allow_ops(record.graph_id, _ops(record.changeset))
        ),
        changeset=record.changeset,
        changeset_hash=record.changeset_hash,
        resolution=record.resolution,
        revisions=record.revisions,
        alias_allocations=record.alias_allocations,
        ontology=record.ontology,
        reason=record.reason,
        idempotency_key=record.idempotency_key,
        request=record.request,
        blobs=record.blobs,
    )


def run_child(
    graph_dir: Path,
    graph_id: str,
    point: str,
    *,
    tx: AuthorizedTx | None = None,
    audit_body: Mapping[str, JsonValue] | None = None,
    blob_file: Path | None = None,
    object_store: ObjectStoreSettings | None = None,
    promote: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one commit (or, without ``tx``, one empty session) and return the outcome.

    With ``blob_file`` the child stages that file before the lock and publishes it
    under the lock before the intent (W.11b); with ``promote`` it promotes the staged
    blob before the lock (W.5b). ``object_store`` names the fake store of a graph whose
    stored files are there."""
    args: dict[str, Any] = {"graph_dir": str(graph_dir), "graph_id": graph_id, "point": point}
    if tx is not None:
        args.update(tx=tx_payload(tx), audit=dict(audit_body or {}))
    if blob_file is not None:
        args["blob_file"] = str(blob_file)
    if object_store is not None:
        args["object_store"] = settings_spec(object_store)
    if promote:
        args["promote"] = True
    proc = spawn("crash", args)
    out, err = proc.communicate(timeout=60)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def run_migration_child(
    graph_dir: Path,
    graph_id: str,
    point: str,
    *,
    tx: AuthorizedTx,
    audit_body: Mapping[str, JsonValue],
    marker: Mapping[str, JsonValue],
) -> subprocess.CompletedProcess[str]:
    """Run one release-shaped migration: marker, intent, ``migrate``, marker removal."""
    args: dict[str, Any] = {
        "graph_dir": str(graph_dir),
        "graph_id": graph_id,
        "point": point,
        "tx": tx_payload(tx),
        "audit": dict(audit_body),
        "marker": dict(marker),
    }
    proc = spawn("migrate", args)
    out, err = proc.communicate(timeout=60)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
