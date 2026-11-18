"""``graph-admin files extract`` and ``files extractors``: the derived metadata of stored files
(file metadata plan M12, step 6).

``extract`` brings the metadata records of a graph, or of the named files, to the current version
of every extractor its model enables (``services.file_metadata``). It is maintenance: authorized as
``maint.reindex`` on the graph (the maintain-level action nearest to "refresh derived data", in
``PROJECT_MAINT_ACTIONS``), a denial audited like any other. The records are written through the
write service as the maintainer, one tx per (file, extractor) with the source
``{name: <extractor>, version, run, kind: extractor}``. ``--dry-run`` runs the extractors and
writes nothing. ``extractors`` lists what this install can extract; it reads no graph.
``records_behind`` is the part of ``files verify`` that names records older than their extractor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graph_cli.extractors import registry
from graph_cli.extractors.model import Extractor
from graph_cli.extractors.runner import ExtractLimits
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.file_metadata import (
    MAINTAIN_ACTION,
    ExtractRequest,
    FileMetadataService,
)
from graph_cli.services.file_metadata_plan import enabled_extractors, read_records
from graph_cli.services.models import ModelCache
from graph_cli.services.write import WriteConfig, WriteService
from graph_cli_admin.services_admin.context import AdminContext


@dataclass(frozen=True, slots=True)
class ExtractOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int | None


def service(ctx: AdminContext) -> FileMetadataService:
    """The extraction service as the maintainer runs it: a write service of its own over the
    context's catalog, audit log and policy, the limits of ``[extract]``."""
    writer = WriteService(
        catalog=ctx.catalog,
        audit=ctx.audit,
        policy=ctx.policy,
        clock=ctx.clock,
        ids=ctx.ids,
        env=ctx.env,
        config=WriteConfig(lock_timeout_s=ctx.lock_timeout_s),
    )
    return FileMetadataService(
        catalog=ctx.catalog,
        writer=writer,
        ids=ctx.ids,
        policy=ctx.policy,
        limits=ExtractLimits(ctx.config.extract_timeout_s, ctx.config.extract_max_memory_mib),
    )


def extract(
    ctx: AdminContext,
    *,
    graph_ref: str,
    files: Sequence[str],
    extractors: Sequence[str],
    retry_failed: bool,
    dry_run: bool,
) -> ExtractOutcome:
    entry = ctx.resolve(graph_ref, "files extract")
    meta = RequestMeta.of(
        "files extract",
        {
            "graph": str(entry.id),
            "files": list(files),
            "extractors": list(extractors),
            "retry_failed": retry_failed,
            "dry_run": dry_run,
        },
    )
    ctx.authorize([(MAINTAIN_ACTION, f"graph:{entry.id}")], request=meta, graph=entry.id)
    report = service(ctx).extract(
        ExtractRequest(
            actor=ctx.actor,
            graph=str(entry.id),
            request=meta,
            files=tuple(files) if files else None,
            extractors=tuple(extractors) if extractors else None,
            retry_failed=retry_failed,
            dry_run=dry_run,
        )
    )
    return ExtractOutcome(entry, report.to_json(), report.last_seq)


def extractors() -> dict[str, JsonValue]:
    """What this install can extract, ``format`` first: name, version, the detected types it
    accepts, the record class (and part class), the module that enables it and its summary."""
    items: list[JsonValue] = []
    for extractor in registry.extractors().values():
        item: dict[str, JsonValue] = {
            "name": extractor.name,
            "version": extractor.version,
            "accepts": list(extractor.accepted_types) or ["*"],
            "record_class": extractor.record_class,
            "part_class": extractor.part_class,
            "module": {"id": extractor.module.id, "min_version": extractor.module.min_version},
            "summary": extractor.summary,
        }
        items.append(item)
    return {"extractors": items}


def records_behind(
    ctx: AdminContext,
    entry: GraphEntry,
    extractor_registry: Mapping[str, Extractor] | None = None,
) -> dict[str, JsonValue]:
    """Per enabled extractor how many records are older than the extractor, and the extractors
    whose module the graph imports too old (``files verify``; nothing is written)."""
    try:
        model = ModelCache(ctx.catalog).load(entry)
    except GraphCliError as error:  # a broken pin is for ``graph check``; verify still answers
        return {"behind": [], "module_outdated": [], "unavailable": error.code}
    enabled, outdated = enabled_extractors(
        model, extractor_registry if extractor_registry is not None else registry.extractors()
    )
    read, _ = ctx.catalog.store_for(entry.id)
    snapshot = read.snapshot()
    behind: list[JsonValue] = []
    for extractor in enabled:
        records = list(read_records(snapshot, model, extractor).values())
        counts = [r for r in records if r.extractor_version < extractor.version]
        newer = [r for r in records if r.extractor_version > extractor.version]
        if counts or newer:
            behind.append(
                {
                    "extractor": extractor.name,
                    "version": extractor.version,
                    "records": len(records),
                    "behind": len(counts),
                    "newer": len(newer),
                }
            )
    return {"behind": behind, "module_outdated": [m.to_json() for m in outdated]}
