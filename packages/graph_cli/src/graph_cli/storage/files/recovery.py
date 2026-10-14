"""Roll-forward recovery under the graph lock (contracts section 4.2, B4.11, B5.9).

1. Truncate a torn or invalid last line of the graph audit stream and the tx
   log. Interior damage stops recovery with ``backend.corrupt``.
2. Write every missing revision file of the txs after the checkpoint;
   existing files must match byte for byte.
3. Rewrite ``head.json`` when it is missing, unreadable or behind the log.
4. Hand an operational note to the op-log callback (written by catalog).

Recovery is deterministic from committed data, idempotent when interrupted
(fault points ``recovery.*``) and not audited.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import RecoveryReport
from graph_cli.storage.common.faults import CrashPoint, fire
from graph_cli.storage.files.headfile import HeadFile
from graph_cli.storage.files.segments import LogScan, OpLog, SegmentedLog, truncation_note
from graph_cli.storage.files.store import (
    FileStore,
    LogState,
    corruption_details,
    materialize,
    write_head,
)


@dataclass(frozen=True, slots=True)
class Recovered:
    """The clean state after recovery, ready for a write session."""

    state: LogState
    audit: LogScan
    report: RecoveryReport


def recover(store: FileStore, *, oplog: OpLog | None = None) -> Recovered:
    """Run section 4.2. The caller holds the graph lock."""
    try:
        return _recover(store, oplog)
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"recovery failed: {exc.strerror or exc}"
        ) from exc


def _recover(store: FileStore, oplog: OpLog | None) -> Recovered:
    state = store.read_state()
    if state.scan.corruption is not None:
        raise GraphCliError(
            "backend.corrupt",
            "the tx log is damaged; writes are refused until repair",
            details=corruption_details(state.scan.corruption),
        )
    audit = store.scan_audit(state.checkpoint)
    if audit.corruption is not None:
        raise GraphCliError(
            "backend.corrupt",
            "the graph audit stream is damaged; writes are refused until repair",
            details=corruption_details(audit.corruption),
        )
    notes: list[dict[str, JsonValue]] = []
    # 1. torn or invalid tails
    truncated = False
    for log, result in ((store.auditlog, audit), (store.txlog, state.scan)):
        if result.tail != "clean":
            notes.append(_truncate(store, log, result))
            truncated = True
    if truncated:
        fire(store.faults, CrashPoint.RECOVERY_AFTER_TRUNCATE)
        state = replace(state, scan=replace(state.scan, tail="clean", tail_bytes=b""))
        audit = replace(audit, tail="clean", tail_bytes=b"")
    # 2. missing revision files
    if state.tail:
        written = materialize(store, state.tail)
        fire(store.faults, CrashPoint.RECOVERY_AFTER_MATERIALIZE)
        if written:
            notes.append({"event": "revisions_materialized", "files": list(written)})
    # 3. checkpoint
    end = state.end
    if end.seq > 0 and (state.checkpoint is None or state.checkpoint.seq != end.seq):
        assert state.scan.position is not None
        head = state.head
        assert head.tx_id is not None and head.recorded_at is not None
        checkpoint = HeadFile(
            seq=head.seq,
            tx_id=head.tx_id,
            hash=head.hash,
            recorded_at=head.recorded_at,
            txlog=state.scan.position,
            audit_link=audit.end,
            audit=audit.position,
            alias_counters=store.view_at(end.seq).alias_counters(),
        )
        write_head(store, checkpoint)
        notes.append({"event": "head_rewritten", "seq": end.seq})
        state = replace(state, checkpoint=checkpoint, tail=(), warnings=())
    # 4. operational log
    if notes and oplog is not None:
        oplog({"event": "recovery", "graph_id": str(store.graph_id), "steps": list(notes)})
    report = RecoveryReport(recovered=bool(notes), details={"steps": list(notes)})
    return Recovered(state=state, audit=audit, report=report)


def _truncate(store: FileStore, log: SegmentedLog, result: LogScan) -> dict[str, JsonValue]:
    assert result.position is not None
    stream = "txlog" if log is store.txlog else store.stream
    note = truncation_note(log, result, stream=stream)
    log.truncate(result.position, store.syncer)
    return note
