"""Run the extractors over the files of a graph and write what they find (file metadata plan).

One run reads the graph once (``file_metadata_plan``), then goes file by file: it runs the
``format`` extractor first, decides from its result which of the other enabled extractors
apply, runs each in a child process (``extractors.runner``) on the file's bytes and writes
the result as one reconcile document per (file, extractor): one tx whose source is the
extraction run (kind ``extractor``), reason ``extract <name>@<version>``. A second run right
after the first writes nothing: every record is at the current version, and a document that
changes nothing makes no tx.

What a run never does: fail as a whole because one document is refused (that problem is
listed and the run goes on), go on after a failure that is not about one document (a refused
right, a lock timeout, a storage or graph error end it; what was written stays, a rerun
continues), record a transient result (it is reported and retried next time), or touch data of
people (the managed classes are written by this path only, ``services.managed``).

Who may run it and what the caller learns. A run over the whole graph or over named files is
maintenance and needs the maintain permission (``maint.reindex``: derived data refresh, the
maintain-level action nearest to it); ``file add`` extracts the file it added and nothing else
and needs no more than the right to write. Everything the run reads and reports goes through
the caller's view (the types it sees, the model it sees): a file of a hidden type is not in the
run, a reference to it is ``entity.not_found``, and no id, media type or count of it is in the
report. The records are written as the caller, so the write service judges every document
with the caller's rights. ``dry_run`` still runs the extractors (a failure or a regression
cannot be known otherwise) and lets the write service judge each document without committing
it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from graph_cli.access_control.authorize import authorize
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
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import IdProvider, TermRef
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.ports.policy import PolicySet, PolicySource
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
from graph_cli.services.read import denial_error
from graph_cli.services.refs import resolve_ref
from graph_cli.services.visibility import Visibility, VisibleSnapshot
from graph_cli.services.write import WriteRequest, WriteService

FORMAT: str = "format"
MAINTAIN_ACTION: str = "maint.reindex"
MAX_LISTED: int = 20
Runner = Callable[[str, Path, str, ExtractLimits], ExtractionResult | Transient]
TARGET_REFERENCED = "data.referential.target_referenced"
# A refusal of one document (this file, this extractor): listed, and the run goes on. Anything
# else (``access.*``, a lock timeout, ``backend.*``, graph state, a model or pin problem) is not
# about one document and ends the run.
_DOCUMENT_REFUSALS = frozenset({"write.invalid", "term.unknown", "entity.not_found"})
_DOCUMENT_REFUSAL_PREFIXES = ("reconcile.", "changeset.", "conflict.")


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
    # False for ``file add``: the one new file in ``files``, no maintain permission needed,
    # nothing about other files is read or written
    maintenance: bool = True


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
    newer_version: int = 0
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
            "newer_version": self.newer_version,
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
    newer_versions: list[dict[str, JsonValue]] = field(default_factory=list)

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
            "newer_versions": list(self.newer_versions[:MAX_LISTED]),
            "newer_version_count": len(self.newer_versions),
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
        self._corrupt: GraphCliError | None = None

    def path(self) -> Path:
        """The path, or ``backend.corrupt`` (the stored copy is missing or does not match its
        hash; raised again without reading again for the next extractor of the same file)."""
        if self._corrupt is not None:
            raise self._corrupt
        if self._path is None:
            try:
                opened = open_stored(self._read, self._revision, self._ref)
                if opened.local_path is not None:
                    self._path = opened.local_path
                else:
                    self._path = self._stack.enter_context(materialized(opened.start()))
            except GraphCliError as error:
                if error.code == "backend.corrupt":
                    self._corrupt = error
                raise
        return self._path


class FileMetadataService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        writer: WriteService,
        ids: IdProvider,
        policy: PolicySource,
        registry: Mapping[str, Extractor] | None = None,
        limits: ExtractLimits = DEFAULT_LIMITS,
        runner: Runner | None = None,
    ) -> None:
        self._catalog = catalog
        self._writer = writer
        self._ids = ids
        self._policy = policy
        self._visibility = Visibility()
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
        raw_model = self._models.load(entry)
        policy = self._policy.current(entry.id)
        if request.maintenance:
            self._require_maintain(request, entry, policy)
        elif request.files is None:
            raise GraphCliError("internal.error", "an add-time extraction names its file")
        view = self._visibility.of(request.actor, entry, policy, raw_model)
        model = self._visibility.model(entry, raw_model, view)
        read, _ = self._catalog.store_for(entry.id)
        raw = read.snapshot()
        seen: Snapshot = VisibleSnapshot(raw, view.types_only()) if view.types_limited else raw
        report = ExtractionReport(dry_run=request.dry_run)
        enabled, outdated = enabled_extractors(model, self._registry)
        report.module_outdated = outdated
        by_name = {e.name: e for e in enabled}
        if FORMAT not in by_name:  # no ``format``: nothing can dispatch
            report.enabled = False
            return report
        selected = self._selected(request, by_name)
        family = file_types(model)
        records = {e.name: read_records(seen, model, e) for e in enabled}
        revisions = self._files(request, seen, family)
        report.files = len(revisions)
        report.run = str(self._ids.new_id()) if request.named_run else None
        tallies = {e.name: ExtractorTally(e.name, e.version) for e in enabled}
        for revision in revisions:
            self._file(request, report, tallies, enabled, selected, records, revision, model, read)
        if request.files is None:  # M13: records follow their file
            self._retract_stale(request, report, model, raw, seen)
        report.extractors = [tallies[e.name] for e in enabled if e.name in selected | {FORMAT}]
        return report

    def _require_maintain(
        self, request: ExtractRequest, entry: GraphEntry, policy: PolicySet
    ) -> None:
        """A run over the whole graph or over named files needs the maintain permission."""
        ctx = AuthzContext(op_count=0, has_reason=False, dry_run=request.dry_run)
        decision = authorize(request.actor, MAINTAIN_ACTION, f"graph:{entry.id}", ctx, policy)
        if decision.effect != "allow":
            raise denial_error(decision)

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
        self, request: ExtractRequest, seen: Snapshot, family: Sequence[str]
    ) -> list[Revision]:
        """The files the caller sees: every active one, or the ones named (a reference to a file
        the caller does not see is ``entity.not_found``, as for one that does not exist)."""
        types = tuple(TermRef(t) for t in family)
        if request.files is None:
            return list(active_files(seen, types))
        found: list[Revision] = []
        for ref in request.files:
            entity, _ = resolve_ref(ref, seen)
            revision = seen.get(entity)
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
                if decision in ("current", "newer_version"):
                    if decision == "current":
                        tally.current += 1
                    elif write:
                        tally.newer_version += 1
                        assert record is not None
                        report.newer_versions.append(
                            {
                                "extractor": extractor.name,
                                "file": file_id,
                                "stored_version": record.extractor_version,
                                "version": extractor.version,
                            }
                        )
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
                    try:
                        path = blob.path()
                    except GraphCliError as error:
                        if error.code != "backend.corrupt":
                            raise  # the store cannot be asked: not about this file
                        detail = "the stored copy is missing or does not match its hash"
                        self._transient(report, tally, extractor, file_id, detail)
                        continue
                    ran = self._runner(extractor.name, path, recorded, self._limits)
                    if isinstance(ran, Transient):
                        self._transient(report, tally, extractor, file_id, ran.detail)
                        continue
                    outcome = ran
                if extractor.name == FORMAT and outcome is not None:
                    detected = _detected_of(outcome)
                self._write(request, report, tally, extractor, model, file_id, outcome, record)

    @staticmethod
    def _transient(
        report: ExtractionReport,
        tally: ExtractorTally,
        extractor: Extractor,
        file_id: str,
        detail: str,
    ) -> None:
        tally.transient += 1
        report.transient.append({"extractor": extractor.name, "file": file_id, "detail": detail})

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
        """Write one document; the counts of its record scope, or ``None`` when this document
        was refused (listed in the report; the run goes on). Every other error ends the run."""
        try:
            result = self._writer.apply(
                WriteRequest(
                    request.actor, request.graph, changeset, request.dry_run, request.request
                )
            )
        except GraphCliError as error:
            if not _is_document_refusal(error.code):
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
        raw: Snapshot,
        seen: Snapshot,
    ) -> None:
        """M13: the records and parts of files that are no longer active. Covers every extractor
        of the registry whose record class the pinned model has (also one whose module is too
        old to run it) and, with ``--extractor``, only the named ones. The records are the ones
        the caller sees; whether a file is active is read from the whole graph (a file the
        caller does not see is not gone). A record of a class no extractor of this install
        writes is left alone: nothing could own the write."""
        for extractor in self._registry.values():
            if extractor.record_class not in model.classes:
                continue
            if request.extractors is not None and extractor.name not in request.extractors:
                continue
            records = read_records(seen, model, extractor)
            for file_id in stale_file_ids(raw, model, extractor, records):
                changeset = inactive_changeset(model, extractor, file_id, run=report.run)
                tally = ExtractorTally(extractor.name, extractor.version)
                if self._apply(request, report, tally, extractor, file_id, changeset) is not None:
                    report.retracted += 1


def _is_document_refusal(code: str) -> bool:
    return code in _DOCUMENT_REFUSALS or code.startswith(_DOCUMENT_REFUSAL_PREFIXES)


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
