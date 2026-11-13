"""Authored ontologies: compile, validate, diff, and the files of a release (contracts B7).

An authored ontology is a directory of ``<module_id>.yaml`` files in the
maintainer workspace; the main module is named explicitly or is the only
module file there (``migration.yaml``, the migration of a major release, is no
module). Validation and diff are reads: no lock, no audit (B5.10).
``release_files`` builds the immutable release directory (B7.4, B7.5) as a
pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import graph_cli
from graph_cli.kernel.canonical import (
    CanonicalError,
    JsonValue,
    hash_bytes,
    parse_json,
    pretty_bytes,
)
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, has_errors
from graph_cli.kernel.ids import format_id
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.diff import (
    BumpCheck,
    DiffReport,
    check_bump,
    check_renames,
    classify,
    module_bumps,
)
from graph_cli.ontology_layer.exporters.jsonschema import export_jsonschema
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version, bump_level
from graph_cli.ontology_layer.vocabulary import (
    IR_FORMAT,
    MIGRATION_FILE,
    MODULE_ID_RE,
    RESERVED_MODULE_IDS,
)

RELEASE_SCHEMA: Final = "graph_cli.ontology_release/1"
COMPILED_FILE: Final = "compiled.json"
DIFF_FILE: Final = "diff.json"
RELEASE_FILE: Final = "release.json"


@dataclass(frozen=True, slots=True)
class Compiled:
    model: CompiledModel
    findings: tuple[Finding, ...]  # warnings and infos; errors raise
    sources: Mapping[str, bytes]  # authored module files that were loaded, by module id

    @property
    def pin(self) -> OntologyPin:
        return OntologyPin(
            self.model.bundle_id, self.model.bundle_version, self.model.artifact_hash
        )


@dataclass(frozen=True, slots=True)
class PinnedModel:
    """A pinned release's model with the hash it was verified against (B7.6).

    The pin covers the stored document. ``model.artifact_hash`` re-serializes
    with the current writer and differs for artifacts written before a key
    was added, so release metadata takes the pinned hash.
    """

    model: CompiledModel
    artifact_hash: str


class _RecordingSource:
    """The workspace directory; remembers which modules the bundle loaded from it."""

    def __init__(self, directory: Path) -> None:
        self._directory = DirectorySource(directory)
        self.loaded: dict[str, bytes] = {}

    def read(self, module_id: str) -> bytes | None:
        data = self._directory.read(module_id)
        if data is not None:
            self.loaded[module_id] = data
        return data


def main_module(directory: Path, module: str | None) -> str:
    if not directory.is_dir():
        raise GraphCliError(
            "usage.invalid_argument",
            f"{directory} is not a directory",
            details={"argument": "source"},
        )
    if module is not None:
        if not MODULE_ID_RE.match(module):  # a module id never names another directory
            raise GraphCliError(
                "usage.invalid_argument",
                f"--module {module!r} is not a module id ({MODULE_ID_RE.pattern})",
                details={"argument": "module"},
            )
        if module in RESERVED_MODULE_IDS or not (directory / f"{module}.yaml").is_file():
            raise GraphCliError(
                "usage.invalid_argument",
                f"{directory} has no module file {module}.yaml",
                details={"argument": "module"},
            )
        return module
    candidates = sorted(
        p.stem for p in directory.glob("*.yaml") if p.stem not in RESERVED_MODULE_IDS
    )
    if len(candidates) != 1:
        raise GraphCliError(
            "usage.missing_argument",
            f"{directory} holds {len(candidates)} modules; name the main one with --module",
            details={"argument": "module", "candidates": list(candidates)},
        )
    return candidates[0]


def compile_source(directory: Path, module: str | None = None) -> Compiled:
    """Load, validate and compile; any error finding is ``admin.model_invalid`` (B7.13)."""
    main = main_module(directory, module)
    source = _RecordingSource(directory)
    outcome = compile_bundle(main, LayeredSource(source))
    if outcome.model is None or has_errors(outcome.findings):
        raise GraphCliError(
            "admin.model_invalid",
            f"the ontology {main} has error findings",
            details={"findings": [f.to_json() for f in outcome.findings]},
        )
    return Compiled(outcome.model, outcome.findings, dict(sorted(source.loaded.items())))


def summary(compiled: Compiled) -> dict[str, JsonValue]:
    model = compiled.model
    return {
        "bundle": model.bundle_id,
        "version": model.bundle_version,
        "artifact_hash": model.artifact_hash,
        "modules": [m.id for m in model.modules],
        "findings": [f.to_json() for f in compiled.findings],
    }


def pinned_model(catalog: Catalog, entry: GraphEntry) -> CompiledModel:
    """The compiled model a graph is pinned to, checked against the pin (B7.6)."""
    data = catalog.read_release_file(entry.id, entry.pin.version, COMPILED_FILE)
    return load_compiled(data, expected_hash=entry.pin.artifact_hash)


def read_migration(directory: Path) -> bytes | None:
    """The bytes of ``migration.yaml`` in a version directory; ``None`` without one. Parsing
    and checking are ``ontology_layer.migration`` (the raw bytes are what a release keeps)."""
    path = directory / MIGRATION_FILE
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot read {path}: {exc.strerror or exc}",
            details={"argument": "source"},
        ) from exc


def installed_migration(read_file: Callable[[str], bytes]) -> bytes | None:
    """The ``migration.yaml`` an installed release keeps (``read_file`` reads a file of its
    directory); ``None`` for a release without one. The file is checked against the hash
    ``release.json`` lists."""
    try:
        metadata = parse_json(read_file(RELEASE_FILE))
    except CanonicalError as exc:
        raise GraphCliError("backend.corrupt", f"{RELEASE_FILE} is unreadable: {exc}") from exc
    listed = metadata.get("files") if isinstance(metadata, Mapping) else None
    if not isinstance(listed, Mapping) or MIGRATION_FILE not in listed:
        return None
    data = read_file(MIGRATION_FILE)
    if hash_bytes(data) != listed[MIGRATION_FILE]:
        raise GraphCliError(
            "backend.corrupt", f"{MIGRATION_FILE} differs from its hash in {RELEASE_FILE}"
        )
    return data


def diff(old: CompiledModel, new: CompiledModel) -> tuple[DiffReport, BumpCheck]:
    report = classify(old, new)
    return report, check_bump(report)


def rename_warnings(old: CompiledModel, new: CompiledModel) -> tuple[WarningRecord, ...]:
    """Judge the ``renamed_from`` declarations of ``new`` against ``old``: an invalid one is
    ``ontology.rename_invalid`` (the problems in the details), a leftover of the previous
    release is the warning ``ontology.rename_stale``. Run by ``ontology diff`` and the release
    plan, not by ``validate`` (there is no old model)."""
    problems = check_renames(old, new)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        raise GraphCliError(
            "ontology.rename_invalid",
            f"{len(errors)} renamed_from declarations do not fit {old.bundle_version} ->"
            f" {new.bundle_version}: {errors[0].message}",
            details={
                "from": old.bundle_version,
                "to": new.bundle_version,
                "count": len(errors),
                "problems": [p.to_json() for p in errors],
            },
        )
    stale = [p for p in problems if p.severity == "warning"]
    if not stale:
        return ()
    return (
        WarningRecord(
            "ontology.rename_stale",
            f"{len(stale)} renamed_from declarations were applied by the previous release;"
            " remove them",
            {
                "from": old.bundle_version,
                "to": new.bundle_version,
                "count": len(stale),
                "problems": [p.to_json() for p in stale],
            },
        ),
    )


def module_bump_warnings(
    old: CompiledModel, new: CompiledModel, report: DiffReport
) -> tuple[WarningRecord, ...]:
    """``admin.module_bump_insufficient`` per module whose own version does not cover the
    changes to its terms. A warning, not a refusal: the release is versioned by the main
    module, which ``check_bump`` enforces."""
    return tuple(
        WarningRecord(
            "admin.module_bump_insufficient",
            f"module {bump.module}: {bump.from_version} -> {bump.to_version} does not cover"
            f" {bump.level} changes",
            bump.to_json(),
        )
        for bump in module_bumps(old, new, report)
        if bump.check == "insufficient"
    )


def release_files(
    compiled: Compiled,
    *,
    previous: PinnedModel | None,
    release_id: UUID,
    released_at: datetime,
    migration: bytes | None = None,
) -> dict[str, bytes]:
    """Every file of ``ontology/<version>/``; ``release.json`` lists the others' sha256.
    ``migration`` is the release's ``migration.yaml`` as authored (part of the release
    identity, so it is listed like the other files)."""
    model = compiled.model
    files: dict[str, bytes] = {COMPILED_FILE: pretty_bytes(model.to_json())}
    metadata: dict[str, JsonValue] = {
        "schema": RELEASE_SCHEMA,
        "release_id": format_id(release_id),
        "bundle": model.bundle_id,
        "version": model.bundle_version,
        "released_at": format_timestamp(released_at),
        "artifact_hash": model.artifact_hash,
    }
    if previous is not None:
        report, _ = diff(previous.model, model)
        files[DIFF_FILE] = pretty_bytes(report.to_json())
        metadata["previous"] = {
            "version": previous.model.bundle_version,
            "artifact_hash": previous.artifact_hash,
        }
        declared = _declared_level(previous.model.bundle_version, model.bundle_version)
        metadata["classification"] = {"declared": declared, "computed": report.level}
    for name, schema in export_jsonschema(model).items():
        files[f"exports/jsonschema/{name}"] = pretty_bytes(schema)
    for module_id, data in compiled.sources.items():
        files[f"source/{module_id}.yaml"] = data
    if migration is not None:
        files[MIGRATION_FILE] = migration
    metadata["compiler"] = {
        "name": "graph_cli",
        "version": graph_cli.__version__,
        "ir_format": IR_FORMAT,
    }
    metadata["files"] = {name: hash_bytes(data) for name, data in sorted(files.items())}
    files[RELEASE_FILE] = pretty_bytes(metadata)
    return files


def _declared_level(old: str, new: str) -> str:
    return bump_level(Version.parse(old), Version.parse(new))
