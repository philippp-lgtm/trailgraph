"""Run the extractors over the files of a graph and write what they find (file metadata plan).

One run reads the graph once (``file_metadata_plan``), then goes file by file: it runs the
``format`` extractor first, decides from its result which of the other enabled extractors
apply, runs each in a child process (``extractors.runner``) on the file's bytes and writes
the result as one reconcile document per (file, extractor): one tx whose source is the
extraction run (kind ``extractor``), reason ``extract <name>@<version>``. A second run right
after the first writes nothing: every record is at the current version, and a document that
changes nothing makes no tx.

What a run never does: fail as a whole because one file or one write fails (the problem is
listed and the run goes on; a refused right ends it, nothing else would differ), record a
transient result (it is reported and retried next time), or touch data of people (the managed
classes are written by this path only, ``services.managed``).

The reads are not authorized per file: this is the maintenance and add-time path, and the
caller's rights are decided by the writes. ``dry_run`` still runs the extractors (a failure or a
regression cannot be known otherwise) and lets the write service judge each document without
committing it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from graph_cli.extractors import registry as extractor_registry
from graph_cli.extractors.model import (
    STATUS_FAILED,
    STATUS_OK,
    ExtractionResult,
    Extractor,
    Transient,
)
from graph_cli.extractors.runner import DEFAULT_LIMITS, ExtractLimits, materialized, run_extractor
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import IdProvider, TermRef
from graph_cli.kernel.ports.catalog import Catalog
from graph_cli.kernel.ports.storage import GraphStoreRead, Snapshot
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.file_metadata_plan import (
    STATUS_NOT_APPLICABLE,
    ModuleOutdated,
    RecordState,
    active_files,
    decide,
    enabled_extractors,
    inactive_changeset,
    read_records,
    record_changeset,
    stale_file_ids,
)
from graph_cli.services.files import file_types, open_stored
from graph_cli.services.models import ModelCache
from graph_cli.services.refs import resolve_ref
from graph_cli.services.write import WriteRequest, WriteService

FORMAT: str = "format"
MAX_LISTED: int = 20
Runner = Callable[[str, Path, str, ExtractLimits], ExtractionResult | Transient]
TARGET_REFERENCED = "data.referential.target_referenced"


@dataclass(frozen=True, slots=True)
class ExtractRequest:
    """One invocation: the files (references, ``None`` = every active file of the graph), the
    extractors to write (``None`` = every enabled one; ``format`` always runs where needed, the
    others dispatch on it), and the switches."""

    actor: ActorContext
    graph: str
    request: RequestMeta
    files: tuple[str, ...] | None = None
    extractors: tuple[str, ...] | None = None
    retry_failed: bool = False
    dry_run: bool = False
    named_run: bool = True  # the source names this invocation (``file add`` does not)


@dataclass(slots=True)
class ExtractorTally:
    """What happened for one extractor in one run."""

    name: str
    version: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    current: int = 0
    not_applicable: int = 0
    failed: int = 0
    transient: int = 0
    blocked: int = 0
    problems: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "version": self.version,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "current": self.current,
            "not_applicable": self.not_applicable,
            "failed": self.failed,
            "transient": self.transient,
            "blocked_by_references": self.blocked,
            "problems": self.problems,
        }


@dataclass(slots=True)
class ExtractionReport:
    """The answer of a run: counts per extractor, regressions (a record that said ``ok`` and now
    says ``failed`` or ``not_applicable``), what could not be done and why."""

    dry_run: bool
    enabled: bool = True
    files: int = 0
    run: str | None = None
    extractors: list[ExtractorTally] = field(default_factory=list)
    module_outdated: list[ModuleOutdated] = field(default_factory=list)
    regressions: list[dict[str, JsonValue]] = field(default_factory=list)
    retracted: int = 0  # records of files that are no longer active (M13)
    problems: list[dict[str, JsonValue]] = field(default_factory=list)
    transient: list[dict[str, JsonValue]] = field(default_factory=list)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "dry_run": self.dry_run,
            "enabled": self.enabled,
            "run": self.run,
            "files": self.files,
            "extractors": [t.to_json() for t in self.extractors],
            "module_outdated": [m.to_json() for m in self.module_outdated],
            "regressions": list(self.regressions[:MAX_LISTED]),
            "regression_count": len(self.regressions),
            "retracted_records": self.retracted,
            "problems": list(self.problems[:MAX_LISTED]),
            "problem_count": len(self.problems),
            "transient": list(self.transient[:MAX_LISTED]),
            "transient_count": len(self.transient),
        }


class _Blob:
    """The bytes of one file as a path, made only when an extractor has to run: the backend's
    own read-only file, else (object storage) a private copy removed with the stack."""

    def __init__(
        self, stack: ExitStack, read: GraphStoreRead, revision: Revision, ref: str
    ) -> None:
        self._stack = stack
        self._revision = revision
        self._read = read
        self._ref = ref
        self._path: Path | None = None

    def path(self) -> Path:
        if self._path is None:
            opened = open_stored(self._read, self._revision, self._ref)
            if opened.local_path is not None:
                self._path = opened.local_path
            else:
                self._path = self._stack.enter_context(materialized(opened.start()))
        return self._path


class FileMetadataService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        writer: WriteService,
        ids: IdProvider,
        registry: Mapping[str, Extractor] | None = None,
        limits: ExtractLimits = DEFAULT_LIMITS,
        runner: Runner | None = None,
    ) -> None:
        self._catalog = catalog
        self._writer = writer
        self._ids = ids
        self._registry = registry if registry is not None else extractor_registry.extractors()
        self._limits = limits
        self._runner: Runner = runner if runner is not None else self._run_child
        self._models = ModelCache(catalog, writer.timings)

    @staticmethod
    def _run_child(
        name: str, path: Path, recorded: str, limits: ExtractLimits
    ) -> ExtractionResult | Transient:
        return run_extractor(name, path, recorded, limits)

    # ---- the run -------------------------------------------------------

    def extract(self, request: ExtractRequest) -> ExtractionReport:
        entry = self._catalog.resolve(request.graph)
        model = self._models.load(entry)
        read, _ = self._catalog.store_for(entry.id)
        snapshot = read.snapshot()
        report = ExtractionReport(dry_run=request.dry_run)
        enabled, outdated = enabled_extractors(model, self._registry)
        report.module_outdated = outdated
        by_name = {e.name: e for e in enabled}
        if FORMAT not in by_name:  # no ``format``: nothing can dispatch
            report.enabled = False
            return report
        selected = self._selected(request, by_name)
        family = file_types(model)
        records = {e.name: read_records(snapshot, model, e) for e in enabled}
        revisions = self._files(request, snapshot, family)
        report.files = len(revisions)
        report.run = str(self._ids.new_id()) if request.named_run else None
        tallies = {e.name: ExtractorTally(e.name, e.version) for e in enabled}
        for revision in revisions:
            self._file(request, report, tallies, enabled, selected, records, revision, model, read)
        if request.files is None:  # M13: records follow their file
            for extractor in enabled:
                self._retract_stale(
                    request, report, model, snapshot, extractor, records[extractor.name]
                )
        report.extractors = [tallies[e.name] for e in enabled if e.name in selected | {FORMAT}]
        return report

    def _selected(self, request: ExtractRequest, by_name: Mapping[str, Extractor]) -> set[str]:
        if request.extractors is None:
            return set(by_name)
        unknown = [n for n in request.extractors if n not in by_name]
        if unknown:
            raise GraphCliError(
                "usage.invalid_argument",
                f"no enabled extractor {unknown[0]!r} in this graph",
                details={
                    "argument": "extractor",
                    "value": unknown[0],
                    "expected": sorted(by_name),
                },
            )
        return set(request.extractors)

    def _files(
        self, request: ExtractRequest, snapshot: Snapshot, family: Sequence[str]
    ) -> list[Revision]:
        types = tuple(TermRef(t) for t in family)
        if request.files is None:
            return list(active_files(snapshot, types))
        found: list[Revision] = []
        for ref in request.files:
            entity, _ = resolve_ref(ref, snapshot)
            revision = snapshot.get(entity)
            if revision is None or revision.record_status != "active":
                raise GraphCliError(
                    "entity.not_found", f"no active file {ref}", details={"ref": ref}
                )
            if revision.type not in family:
                raise GraphCliError(
                    "usage.invalid_argument",
                    f"{ref} is a {revision.type}, not a stored file",
                    details={"argument": "ref", "value": ref, "actual": revision.type},
                )
            found.append(revision)
        return found

    # ---- one file ------------------------------------------------------

    def _file(
        self,
        request: ExtractRequest,
        report: ExtractionReport,
        tallies: Mapping[str, ExtractorTally],
        enabled: Sequence[Extractor],
        selected: set[str],
        records: Mapping[str, Mapping[str, RecordState]],
        revision: Revision,
        model: CompiledModel,
        read: GraphStoreRead,
    ) -> None:
        file_id = str(revision.id)
        recorded = str(revision.attrs.get("media_type"))
        with ExitStack() as stack:
            blob = _Blob(stack, read, revision, file_id)
            detected: str | None = None
            for extractor in enabled:
                record = records[extractor.name].get(file_id)
                if extractor.name == FORMAT:
                    decision = decide(
                        extractor, "", recorded, record, retry_failed=request.retry_failed
                    )
                    write = True
                else:
                    if detected is None:
                        continue  # the format is unknown for now: nothing dispatches
                    decision = decide(
                        extractor, detected, recorded, record, retry_failed=request.retry_failed
                    )
                    write = extractor.name in selected
                tally = tallies[extractor.name]
                outcome: ExtractionResult | None
                if decision == "current":
                    tally.current += 1
                    if extractor.name == FORMAT:
                        detected = _detected(record)
                    continue
                if decision == "already_not_applicable":
                    continue
                if not write:
                    continue
                if decision == "not_applicable":
                    outcome = None
                else:
                    ran = self._runner(extractor.name, blob.path(), recorded, self._limits)
                    if isinstance(ran, Transient):
                        tally.transient += 1
                        report.transient.append(
                            {"extractor": extractor.name, "file": file_id, "detail": ran.detail}
                        )
                        continue
                    outcome = ran
                if extractor.name == FORMAT and outcome is not None:
                    detected = _detected_of(outcome)
                self._write(request, report, tally, extractor, model, file_id, outcome, record)

    def _write(
        self,
        request: ExtractRequest,
        report: ExtractionReport,
        tally: ExtractorTally,
        extractor: Extractor,
        model: CompiledModel,
        file_id: str,
        outcome: ExtractionResult | None,
        record: RecordState | None,
    ) -> None:
        status = STATUS_NOT_APPLICABLE if outcome is None else outcome.status
        changeset = record_changeset(model, extractor, file_id, outcome, run=report.run)
        counts = self._apply(request, report, tally, extractor, file_id, changeset)
        if counts is None:
            return
        tally.created += counts["created"]
        tally.updated += counts["updated"]
        tally.unchanged += counts["unchanged"]
        if status == STATUS_FAILED:
            tally.failed += 1
        elif status == STATUS_NOT_APPLICABLE:
            tally.not_applicable += 1
        if record is not None and record.status == STATUS_OK and status != STATUS_OK:
            report.regressions.append(
                {
                    "extractor": extractor.name,
                    "file": file_id,
                    "from": STATUS_OK,
                    "to": status,
                    "problem": None if outcome is None else outcome.problem,
                }
            )

    def _apply(
        self,
        request: ExtractRequest,
        report: ExtractionReport,
        tally: ExtractorTally,
        extractor: Extractor,
        file_id: str,
        changeset: ChangeSet,
    ) -> Counter[str] | None:
        """Write one document; the counts of its record scope, or ``None`` when it was refused
        (listed in the report; the run goes on, except for a right the caller lacks)."""
        try:
            result = self._writer.apply(
                WriteRequest(
                    request.actor, request.graph, changeset, request.dry_run, request.request
                )
            )
        except GraphCliError as error:
            if error.code.startswith("access."):
                raise
            blocked = _blocked_by_references(error)
            if blocked:
                tally.blocked += 1
            else:
                tally.problems += 1
            report.problems.append(
                {
                    "extractor": extractor.name,
                    "file": file_id,
                    "code": "blocked_by_references" if blocked else error.code,
                    "message": error.message,
                }
            )
            return None
        counts: Counter[str] = Counter()
        block = result.reconcile
        scopes = block.get("scopes") if isinstance(block, dict) else None
        if isinstance(scopes, list) and scopes and isinstance(scopes[0], dict):
            for key in ("created", "updated", "unchanged"):
                value = scopes[0].get(key)
                counts[key] += value if isinstance(value, int) else 0
        return counts

    # ---- records of files that are gone --------------------------------

    def _retract_stale(
        self,
        request: ExtractRequest,
        report: ExtractionReport,
        model: CompiledModel,
        snapshot: Snapshot,
        extractor: Extractor,
        records: Mapping[str, RecordState],
    ) -> None:
        for file_id in stale_file_ids(snapshot, model, extractor, records):
            changeset = inactive_changeset(model, extractor, file_id, run=report.run)
            tally = ExtractorTally(extractor.name, extractor.version)
            if self._apply(request, report, tally, extractor, file_id, changeset) is not None:
                report.retracted += 1


def _blocked_by_references(error: GraphCliError) -> bool:
    """The refusal is a graph relation (``on_target_inactive: refuse``) that still points at a
    part the new version no longer produces."""
    findings = error.details.get("findings") if error.details else None
    if not isinstance(findings, list):
        return False
    return any(isinstance(f, Mapping) and f.get("code") == TARGET_REFERENCED for f in findings)


def _detected(record: RecordState | None) -> str | None:
    if record is None or record.status != STATUS_OK:
        return None
    value = record.attrs.get("detected_media_type")
    return value if isinstance(value, str) else None


def _detected_of(result: ExtractionResult) -> str | None:
    if result.status != STATUS_OK:
        return None
    value = result.values.get("detected_media_type")
    return value if isinstance(value, str) else None
