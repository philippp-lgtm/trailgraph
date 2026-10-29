"""``graph-admin graph check``: the data of a graph against an ontology (batch, R.7).

- ``check_graph`` on the head snapshot: the checks of the write path plus
  the ones only a batch check runs, rules with ``when: batch`` and ``in``
  cardinality. A read: no graph lock, no audit event, nothing written.
- Under the pinned release by default (checked against the pin, B7.6).
  With an authored ontology the same data is checked under that ontology
  instead, without releasing it (what a major release checks, warnings
  included).
- Authorized as ``maint.audit_verify`` on the graph, like ``files verify``.
- The published function catalog is checked against the same model
  (``graph_cli.kinetic.check_catalog``): a function that no longer resolves
  (``function.stale`` for the stored query text) fails with
  ``admin.functions_broken``; ``functions`` in the data lists the problems.
- Error findings fail with ``admin.data_errors`` (counts and findings in the
  details); warnings and info findings alone succeed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import has_errors
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.compile import check_catalog
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli_admin.services_admin.audit import counts_of
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import compile_source, pinned_model
from graph_cli_admin.services_admin.release import functions_broken


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


def check(
    ctx: AdminContext, *, graph_ref: str, source: Path | None, module: str | None
) -> CheckOutcome:
    entry = ctx.resolve(graph_ref, "graph check")
    meta = RequestMeta.of("graph check", {"graph": str(entry.id)})
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    ctx.read_data(entry, meta)
    if source is None:
        model = pinned_model(ctx.catalog, entry)
    else:
        model = compile_source(source, module).model
    read, _ = ctx.catalog.store_for(entry.id)
    snapshot = read.snapshot()
    findings = check_graph(model, snapshot)
    codes = Counter(f.code for f in findings)
    slot = ctx.catalog.read_functions(entry.id)
    problems = check_catalog(slot.compiled, model) if slot is not None else []
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "ontology": {"bundle": model.bundle_id, "version": model.bundle_version},
        "pinned": source is None,
        "counts": counts_of(findings),
        "codes": dict(sorted(codes.items())),
        "findings": [f.to_json() for f in findings],
        "functions": {
            "count": slot.count if slot is not None else 0,
            "problems": [p.to_json() for p in problems],
        },
    }
    if has_errors(findings):
        raise GraphCliError(
            "admin.data_errors",
            f"the data of {entry.name} has errors under {model.bundle_version}: {data['counts']}",
            details={k: data[k] for k in ("ontology", "counts", "codes", "findings")},
        )
    if problems:
        raise functions_broken(
            problems, {"to": model.bundle_version, "pinned": source is None}, advice=""
        )
    return CheckOutcome(entry, data, snapshot.seq, snapshot.warnings)
