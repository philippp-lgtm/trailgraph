"""A graph with files and stub extractors for the extraction service tests.

The stubs stand in for extractors that do not exist yet or must change on demand: a ``pdf``
that counts the page objects of the file, and a ``sqlite`` whose parts the test scripts. Their
output depends on the bytes only, like a real extractor's; ``run_in_process`` is the runner of
the tests that do not need a child process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from graph_cli.extractors.format import FORMAT
from graph_cli.extractors.model import (
    ExtractionResult,
    Extractor,
    ModuleRef,
    PartResult,
    RunInput,
    Transient,
    accepts_types,
)
from graph_cli.extractors.runner import ExtractLimits
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.services.file_metadata import ExtractRequest, FileMetadataService
from graph_cli.services.files import FileAddRequest
from support.services import ServiceEnv

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "files"
PDF = "application/pdf"
SQLITE = "application/vnd.sqlite3"


def pdf_stub(
    version: int = 1,
    *,
    accepts: bool = True,
    fail: Callable[[bytes], str | None] | None = None,
    module_min: str = "1.0.0",
) -> Extractor:
    """Counts ``/Type /Page `` objects; ``fail(bytes)`` may return a problem code."""

    def run(source: RunInput) -> ExtractionResult:
        data = source.path.read_bytes()
        problem = None if fail is None else fail(data)
        if problem is not None:
            return ExtractionResult.failed(problem)
        values: dict[str, JsonValue] = {
            "page_count": data.count(b"/Type /Page "),
            "encrypted": False,
        }
        if version >= 2:
            values["pdf_version"] = "1.4"
        return ExtractionResult(status="ok", values=values)

    return Extractor(
        name="pdf",
        version=version,
        module=ModuleRef("files_pdf", module_min),
        record_class="files_pdf:PdfMetadata",
        accepts=accepts_types(PDF) if accepts else (lambda detected, recorded: False),
        run=run,
        summary="test stub",
    )


@dataclass
class Tables:
    """What the ``sqlite`` stub reports: part name to (kind, column names)."""

    parts: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)


def sqlite_stub(tables: Tables, version: int = 1) -> Extractor:
    def run(source: RunInput) -> ExtractionResult:
        del source
        parts = tuple(
            PartResult(
                name,
                {"kind": kind, "column_count": len(columns), "column_names": list(columns)},
            )
            for name, (kind, columns) in sorted(tables.parts.items())
        )
        counts = [kind for kind, _ in tables.parts.values()]
        return ExtractionResult(
            status="ok",
            values={
                "table_count": counts.count("table"),
                "view_count": counts.count("view"),
            },
            parts=parts,
        )

    return Extractor(
        name="sqlite",
        version=version,
        module=ModuleRef("files_sqlite", "1.0.0"),
        record_class="files_sqlite:SqliteMetadata",
        accepts=accepts_types(SQLITE),
        run=run,
        summary="test stub",
        part_class="files_sqlite:SqliteObject",
        part_relation="files_sqlite:has_object",
    )


def registry(*extractors: Extractor) -> dict[str, Extractor]:
    return {e.name: e for e in (FORMAT, *extractors)}


def run_in_process(
    registry: Mapping[str, Extractor],
) -> Callable[[str, Path, str, ExtractLimits], ExtractionResult | Transient]:
    def runner(
        name: str, path: Path, recorded: str, limits: ExtractLimits
    ) -> ExtractionResult | Transient:
        del limits
        return registry[name].run(RunInput(path, recorded))

    return runner


class Graph:
    """A memory (or files) graph on ``notes_and_sources`` 1.3.0 with files from the corpus."""

    def __init__(self, env: ServiceEnv) -> None:
        self.env = env
        self.files: dict[str, str] = {}  # corpus name -> file id

    def add(self, name: str, *, media_type: str | None = None) -> str:
        added = self.env.make_files().add(
            FileAddRequest(
                actor=self.env.actor,
                graph=self.env.name,
                path=CORPUS / name,
                request=RequestMeta("file add", hash_value({"name": name})),
                media_type=media_type,
                no_origin=True,
            )
        )
        file_id = str(added.entity["id"])
        self.files[name] = file_id
        return file_id

    def service(
        self,
        extractors: Mapping[str, Extractor],
        *,
        runner: Callable[[str, Path, str, ExtractLimits], ExtractionResult | Transient]
        | None = None,
    ) -> FileMetadataService:
        return FileMetadataService(
            catalog=self.env.catalog,
            writer=self.env.service,
            ids=self.env.ids,
            registry=extractors,
            runner=runner if runner is not None else run_in_process(extractors),
        )

    def request(self, **kw: object) -> ExtractRequest:
        return ExtractRequest(
            actor=self.env.actor,
            graph=self.env.name,
            request=RequestMeta("files extract", hash_value({})),
            **kw,  # type: ignore[arg-type]
        )

    def records(self, record_class: str) -> dict[str, Revision]:
        """Active records of a class by file id, read from the stored state."""
        page = self.env.snapshot().query(Query(types=(TermRef(record_class),), limit=10_000))
        return {str(r.attrs["key"]).split("/")[1]: r for r in page.items}

    def parts(self, part_class: str) -> dict[str, Revision]:
        page = self.env.snapshot().query(Query(types=(TermRef(part_class),), limit=10_000))
        return {str(r.attrs["key"]): r for r in page.items}
