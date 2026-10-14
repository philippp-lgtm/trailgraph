"""``graph-admin audit verify`` and ``audit anchor`` (contracts B5.11, B5.12).

``verify`` combines storage ``verify()`` (tx log, revision files, head,
manifest pin) with the audit verifier (chains, shapes, tx <-> intent,
maintenance effects, pin events, anchors). Order, without cascades:

1. the installation stream, for its findings and its anchors;
2. each graph under its maintenance lock: storage findings, then the graph
   stream with the txs (only if the tx log is clean) and the maintenance
   effects (the manifest pin only if storage found it intact);
3. the anchors against the graph streams.

Then a ``maint.audit_verify`` event anchors the heads of the graphs without
error findings (anchoring a damaged head would bless it). A damaged
installation stream cannot be appended to; the verification is then not
recorded (warning ``admin.verify_not_recorded``). Error findings make the
command fail with ``admin.integrity_errors``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graph_cli.audit import events
from graph_cli.audit.verify import (
    MaintEffects,
    StreamReport,
    check_anchors,
    effect_anchors,
    verify_graph,
    verify_stream,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, Severity, has_errors, sort_findings
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.storage import GraphStoreRead
from graph_cli.kernel.records import TxRecord
from graph_cli.storage import finding_codes as storage_codes
from graph_cli_admin.services_admin.context import AdminContext, request


@dataclass(frozen=True, slots=True)
class Verification:
    data: dict[str, JsonValue]
    findings: tuple[Finding, ...]
    warnings: tuple[WarningRecord, ...]

    def raise_on_errors(self) -> None:
        if has_errors(self.findings):
            counts = self.data["counts"]
            raise GraphCliError(
                "admin.integrity_errors",
                f"verification found errors: {counts}",
                details={
                    "counts": counts,
                    "findings": [f.to_json() for f in self.findings],
                },
            )


def verify(ctx: AdminContext) -> Verification:
    meta = request("audit verify", {})
    decisions = ctx.authorize([("maint.audit_verify", "installation")], request=meta)
    installation = verify_stream(
        ctx.audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM
    )
    anchored = effect_anchors(installation.events)
    findings: list[Finding] = list(installation.findings)
    reports: dict[str, StreamReport] = {}
    graphs: list[JsonValue] = []
    clean: list[StreamReport] = []
    for graph_id in ctx.maintenance.graph_ids():
        graph_findings, report = _verify_graph(ctx, graph_id, anchored.get(str(graph_id)))
        reports[str(graph_id)] = report
        findings += graph_findings
        ok = not has_errors(graph_findings)
        graphs.append({"id": str(graph_id), "clean": ok, "seq": report.head[0]})
        if ok and report.head[0] > 0:
            clean.append(report)
    anchor_findings = check_anchors(installation.events, {g: r.events for g, r in reports.items()})
    findings += anchor_findings
    ordered = tuple(sort_findings(findings))
    warnings: tuple[WarningRecord, ...] = ()
    recorded = False
    if has_errors(installation.findings):
        warnings = (_not_recorded("the installation audit stream is damaged"),)
    else:
        heads = [events.StreamHead(r.stream, r.head[0], r.head[1]) for r in clean]
        try:
            ctx.audit.append_installation(
                events.maintenance(
                    actor=ctx.actor,
                    decisions=decisions,
                    request=meta,
                    ts=ctx.clock.now(),
                    event_id=ctx.event_id(),
                    env=ctx.env,
                    anchors=heads,
                )
            )
            recorded = True
        except GraphCliError as exc:
            if exc.code != "audit.unavailable":
                raise
            warnings = (_not_recorded(exc.message),)
    data: dict[str, JsonValue] = {
        "counts": _counts(ordered),
        "graphs": graphs,
        "recorded": recorded,
        "findings": [f.to_json() for f in ordered],
    }
    return Verification(data, ordered, warnings)


def _verify_graph(
    ctx: AdminContext, graph_id: GraphId, anchored: frozenset[int] | None
) -> tuple[list[Finding], StreamReport]:
    with ctx.maintenance.maintenance_session(graph_id, timeout_s=ctx.lock_timeout_s) as session:
        storage = list(session.verify())
        codes = {f.code for f in storage}
        pin_intact = storage_codes.PIN_MISMATCH not in codes
        effects = MaintEffects(
            manifest_pin=session.manifest().entry.pin if pin_intact else None,
            releases=session.release_hashes(),
            anchored=anchored or frozenset(),
        )
        txs = None
        if storage_codes.TX_CHAIN_BROKEN not in codes:
            txs = all_txs(ctx.maintenance.store(graph_id))
        report = verify_graph(
            ctx.audit.read_lines(graph_stream(str(graph_id))),
            graph_id=graph_id,
            txs=txs,
            effects=effects,
        )
    return [*storage, *report.findings], report


def all_txs(store: GraphStoreRead) -> list[TxRecord]:
    records: list[TxRecord] = []
    while page := store.changes_since(records[-1].seq if records else 0):
        records.extend(page)
    return records


def anchor(ctx: AdminContext) -> dict[str, JsonValue]:
    """One ``maint.audit_anchor`` event with the chain-verified head of every graph stream."""
    meta = request("audit anchor", {})
    decisions = ctx.authorize([("maint.audit_anchor", "installation")], request=meta)
    heads = []
    for graph_id in ctx.maintenance.graph_ids():
        stream = graph_stream(str(graph_id))
        seq, digest = ctx.audit.head(stream)
        if seq > 0:
            heads.append(events.StreamHead(stream, seq, digest))
    receipt = ctx.audit.append_installation(
        events.maintenance(
            actor=ctx.actor,
            decisions=decisions,
            request=meta,
            ts=ctx.clock.now(),
            event_id=ctx.event_id(),
            env=ctx.env,
            anchors=heads,
        )
    )
    return {"seq": receipt.seq, "anchors": [events.anchor(head) for head in heads]}


def _counts(findings: Sequence[Finding]) -> Mapping[str, JsonValue]:
    counter = Counter(f.severity for f in findings)
    return {severity.value: counter.get(severity, 0) for severity in Severity}


def _not_recorded(reason: str) -> WarningRecord:
    return WarningRecord(
        "admin.verify_not_recorded", f"the verification was not audited: {reason}", {}
    )
