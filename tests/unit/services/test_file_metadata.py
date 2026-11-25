"""The extraction service on both backends (file metadata plan, steps 3 and 4).

Files of the golden corpus are added to a ``notes_and_sources`` 1.3.0 graph (two kinds of
metadata imported); extractors are the real ``format`` and stubs whose output the tests script.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.fake_s3 import FakeS3
from support.file_metadata import (
    Graph,
    Tables,
    pdf_stub,
    registry,
    sqlite_stub,
)
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BuiltinPolicySource, policy_set
from graph_cli.extractors.model import ExtractionResult, RunInput, Transient
from graph_cli.extractors.runner import ExtractLimits
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.file_metadata import FileMetadataService
from graph_cli.services.write import WriteRequest, WriteResult, WriteService

FORMAT_CLASS = "files:FormatMetadata"
PDF_CLASS = "files_pdf:PdfMetadata"
SQLITE_CLASS = "files_sqlite:SqliteMetadata"
PART_CLASS = "files_sqlite:SqliteObject"


@pytest.fixture(params=["memory", "files"])
def graph(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Graph]:
    env = ServiceEnv(str(request.param), "notes_and_sources", tmp_path / "root", version="1.3.0")
    yield Graph(env)


def seq(graph: Graph) -> int:
    return graph.env.head_seq()


def test_first_run_writes_one_record_per_file_and_applicable_extractor(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    text = graph.add("notes.txt")
    renamed = graph.add("text_renamed.pdf")
    extractors = registry(pdf_stub())
    report = graph.service(extractors).extract(graph.request())
    assert report.enabled and report.files == 3
    formats = graph.records(FORMAT_CLASS)
    assert set(formats) == {pdf, text, renamed}
    assert formats[pdf].attrs["detected_media_type"] == "application/pdf"
    assert formats[renamed].attrs["detected_media_type"] == "text/plain"
    assert formats[renamed].attrs["media_type_mismatch"] is False  # recorded: octet-stream
    assert set(graph.records(PDF_CLASS)) == {pdf}  # a text file named .pdf never reaches pdf
    assert graph.records(PDF_CLASS)[pdf].attrs["page_count"] == 1
    tallies = {t.name: t for t in report.extractors}
    assert (tallies["format"].created, tallies["pdf"].created) == (3, 1)
    assert report.problems == [] and report.regressions == []


def test_a_record_points_at_its_file_and_holds_what_the_extractor_said(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub())).extract(graph.request())
    record = graph.records(PDF_CLASS)[pdf]
    assert record.attrs["key"] == f"pdf/{pdf}"
    assert (record.attrs["extractor"], record.attrs["extractor_version"]) == ("pdf", 1)
    assert record.attrs["status"] == "ok"
    assert [(e.rel, str(e.target)) for e in record.edges] == [("files:metadata_of", pdf)]


def test_each_write_is_one_tx_that_names_the_extraction_run(graph: Graph) -> None:
    graph.add("one_page.pdf")
    before = seq(graph)
    report = graph.service(registry(pdf_stub())).extract(graph.request())
    txs = [tx for tx in graph.env.txs() if tx.seq > before]
    assert len(txs) == 2  # format and pdf of one file
    assert {tx.reason for tx in txs} == {"extract format@1", "extract pdf@1"}
    for tx in txs:
        source = tx.request.source if tx.request is not None else None
        assert source is not None and source.kind == "extractor"
        assert source.name in {"format", "pdf"} and source.version == "1"
        assert source.run == report.run


def test_a_second_run_writes_nothing(graph: Graph) -> None:
    graph.add("one_page.pdf")
    graph.add("database.sqlite")
    service = graph.service(registry(pdf_stub(), sqlite_stub(_tables())))
    service.extract(graph.request())
    head = seq(graph)
    again = service.extract(graph.request())
    assert seq(graph) == head
    assert all(t.created == t.updated == 0 for t in again.extractors)
    assert {t.name: t.current for t in again.extractors} == {"format": 2, "pdf": 1, "sqlite": 1}


def _tables() -> Tables:
    return Tables({"orders": ("table", ("id", "customer")), "open_orders": ("view", ("id",))})


def test_a_new_version_updates_the_same_record(graph: Graph) -> None:
    pdf = graph.add("three_pages.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    first = graph.records(PDF_CLASS)[pdf]
    report = graph.service(registry(pdf_stub(2))).extract(graph.request())
    second = graph.records(PDF_CLASS)[pdf]
    assert second.id == first.id and second.rev == first.rev + 1
    assert (second.attrs["extractor_version"], second.attrs["page_count"]) == (2, 3)
    assert second.attrs["pdf_version"] == "1.4"  # a version may add attributes
    assert [(e.rel, str(e.target)) for e in second.edges] == [("files:metadata_of", pdf)]
    assert {t.name: t.updated for t in report.extractors}["pdf"] == 1
    assert report.regressions == []


def test_a_failed_run_is_a_recorded_result_and_unsets_the_values(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    broken = pdf_stub(2, fail=lambda data: "pdf.unreadable")
    report = graph.service(registry(broken)).extract(graph.request())
    record = graph.records(PDF_CLASS)[pdf]
    assert record.attrs["status"] == "failed" and record.attrs["problem"] == "pdf.unreadable"
    assert "page_count" not in record.attrs and "encrypted" not in record.attrs
    assert report.regressions == [
        {"extractor": "pdf", "file": pdf, "from": "ok", "to": "failed", "problem": "pdf.unreadable"}
    ]
    assert {t.name: t.failed for t in report.extractors}["pdf"] == 1


def test_a_failed_record_runs_again_only_when_asked(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    broken = pdf_stub(1, fail=lambda data: "limit.time")
    graph.service(registry(broken)).extract(graph.request())
    head = seq(graph)
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    assert seq(graph) == head  # same version: nothing runs
    report = graph.service(registry(pdf_stub(1))).extract(graph.request(retry_failed=True))
    record = graph.records(PDF_CLASS)[pdf]
    assert (record.attrs["status"], record.attrs["page_count"]) == ("ok", 1)
    assert "problem" not in record.attrs
    assert report.regressions == []


def test_an_extractor_that_no_longer_applies_leaves_a_not_applicable_record(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    report = graph.service(registry(pdf_stub(2, accepts=False))).extract(graph.request())
    record = graph.records(PDF_CLASS)[pdf]
    assert record.attrs["status"] == "not_applicable"
    assert "page_count" not in record.attrs
    assert [r["to"] for r in report.regressions] == ["not_applicable"]
    head = seq(graph)
    graph.service(registry(pdf_stub(2, accepts=False))).extract(graph.request())
    assert seq(graph) == head
    history = [r.attrs.get("page_count") for r in _history(graph, record.id)]
    assert history == [1, None]  # what pdf@1 claimed stays in the history


def _history(graph: Graph, entity: EntityId) -> list[Revision]:
    revisions: list[Revision] = []
    for tx in graph.env.txs():
        revisions.extend(r for r in tx.revisions if r.id == entity)
    return revisions


def test_transient_results_are_reported_and_never_recorded(graph: Graph) -> None:
    graph.add("one_page.pdf")
    extractors = registry(pdf_stub())
    head = seq(graph)
    report = graph.service(
        extractors, runner=lambda name, path, recorded, limits: Transient("killed")
    ).extract(graph.request())
    assert seq(graph) == head
    assert report.transient == [
        {"extractor": "format", "file": graph.files["one_page.pdf"], "detail": "killed"}
    ]
    assert {t.name: t.transient for t in report.extractors}["format"] == 1
    assert graph.records(FORMAT_CLASS) == {}


def test_a_dry_run_runs_the_extractors_and_writes_nothing(graph: Graph) -> None:
    graph.add("one_page.pdf")
    head = seq(graph)
    report = graph.service(registry(pdf_stub())).extract(graph.request(dry_run=True))
    assert seq(graph) == head
    assert {t.name: t.created for t in report.extractors} == {"format": 1, "pdf": 1}
    assert graph.records(FORMAT_CLASS) == {}


def test_a_dry_run_lists_regressions(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    head = seq(graph)
    report = graph.service(registry(pdf_stub(2, accepts=False))).extract(
        graph.request(dry_run=True)
    )
    assert seq(graph) == head
    assert [(r["extractor"], r["file"]) for r in report.regressions] == [("pdf", pdf)]


def test_the_selection_limits_what_is_written_and_names_are_checked(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.add("notes.txt")
    service = graph.service(registry(pdf_stub()))
    report = service.extract(graph.request(files=(pdf,), extractors=("format",)))
    assert report.files == 1
    assert set(graph.records(FORMAT_CLASS)) == {pdf}
    assert graph.records(PDF_CLASS) == {}
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request(extractors=("nope",)))
    assert caught.value.code == "usage.invalid_argument"
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request(files=("FILE-0099",)))
    assert caught.value.code in {"entity.not_found", "usage.invalid_argument"}


def test_a_graph_without_the_files_metadata_module_reports_it_off(tmp_path: Path) -> None:
    env = ServiceEnv("memory", "inventory", tmp_path / "root")
    graph = Graph(env)
    report = graph.service(registry()).extract(graph.request())
    assert report.enabled is False and report.files == 0


def test_an_extractor_whose_module_is_too_old_is_reported_not_run(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    stub = pdf_stub(1, module_min="1.5.0")
    report = graph.service(registry(stub)).extract(graph.request())
    assert [m.to_json() for m in report.module_outdated] == [
        {"extractor": "pdf", "module": "files_pdf", "have": "1.0.0", "need": "1.5.0"}
    ]
    assert graph.records(PDF_CLASS) == {}
    assert pdf in graph.records(FORMAT_CLASS)


def test_the_records_of_a_retracted_file_are_retracted(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    keep = graph.add("notes.txt")
    service = graph.service(registry(pdf_stub()))
    service.extract(graph.request())
    graph.env.apply({"op": "retract", "id": pdf}, reason="not wanted")
    report = service.extract(graph.request())
    assert report.retracted == 2  # its format and pdf records
    assert set(graph.records(FORMAT_CLASS)) == {keep}
    assert graph.records(PDF_CLASS) == {}
    txs = [tx for tx in graph.env.txs() if str(tx.reason).endswith(": file not active")]
    assert len(txs) == 2
    head = seq(graph)
    assert service.extract(graph.request()).retracted == 0
    assert seq(graph) == head


def test_parts_follow_the_version_by_key(graph: Graph) -> None:
    db = graph.add("database.sqlite")
    tables = Tables(
        {"orders": ("table", ("id", "customer")), "customers": ("table", ("id", "name"))}
    )
    service = graph.service(registry(sqlite_stub(tables, 1)))
    service.extract(graph.request())
    first = graph.parts(PART_CLASS)
    record = graph.records(SQLITE_CLASS)[db]
    assert set(first) == {f"sqlite/{db}/orders", f"sqlite/{db}/customers"}
    assert sorted(
        str(e.target) for e in record.edges if e.rel == "files_sqlite:has_object"
    ) == sorted(str(p.id) for p in first.values())
    # version 2: orders gets more columns, customers is gone, views appear
    tables.parts = {
        "orders": ("table", ("id", "customer", "total")),
        "open_orders": ("view", ("id",)),
    }
    report = graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    second = graph.parts(PART_CLASS)
    assert set(second) == {f"sqlite/{db}/orders", f"sqlite/{db}/open_orders"}
    kept = second[f"sqlite/{db}/orders"]
    assert kept.id == first[f"sqlite/{db}/orders"].id  # the identity stays
    assert kept.attrs["column_count"] == 3
    new_record = graph.records(SQLITE_CLASS)[db]
    assert new_record.id == record.id
    assert sorted(
        str(e.target) for e in new_record.edges if e.rel == "files_sqlite:has_object"
    ) == sorted(str(p.id) for p in second.values())
    gone = first[f"sqlite/{db}/customers"]
    stored = graph.env.snapshot().get(gone.id)
    assert stored is not None and stored.record_status == "retracted"
    assert report.problems == []
    head = seq(graph)
    graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    assert seq(graph) == head


def test_a_part_scope_without_parts_retracts_what_a_version_stopped_producing(graph: Graph) -> None:
    db = graph.add("database.sqlite")
    tables = Tables({"orders": ("table", ("id",))})
    graph.service(registry(sqlite_stub(tables, 1))).extract(graph.request())
    tables.parts = {}
    graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    assert graph.parts(PART_CLASS) == {}
    record = graph.records(SQLITE_CLASS)[db]
    assert [e for e in record.edges if e.rel == "files_sqlite:has_object"] == []
    assert record.attrs["status"] == "ok"


def test_the_service_hands_the_extractor_a_readable_path_for_every_backend(graph: Graph) -> None:
    graph.add("table.csv")
    seen: list[tuple[str, bool, bytes]] = []
    extractors = registry()

    def runner(
        name: str, path: Path, recorded: str, limits: ExtractLimits
    ) -> ExtractionResult | Transient:
        seen.append((name, path.is_file(), path.read_bytes()[:3]))
        return extractors[name].run(RunInput(path, recorded))

    graph.service(extractors, runner=runner).extract(graph.request())
    assert seen == [("format", True, b"id,")]


def test_a_graph_relation_that_refuses_blocks_the_whole_change_of_a_record(tmp_path: Path) -> None:
    """A table a report points at (``on_target_inactive: refuse``) stays: the record is left at
    its old version, nothing is written half, and the report names the block (M8)."""
    model = _guarded_model(tmp_path)
    env = ServiceEnv("memory", "guarded", tmp_path / "root", model_override=model)
    graph = Graph(env)
    db = graph.add("database.sqlite")
    tables = Tables({"orders": ("table", ("id",)), "customers": ("table", ("id",))})
    graph.service(registry(sqlite_stub(tables, 1))).extract(graph.request())
    orders = graph.parts(PART_CLASS)[f"sqlite/{db}/orders"]
    customers = graph.parts(PART_CLASS)[f"sqlite/{db}/customers"]
    env.apply(
        {
            "op": "create",
            "type": "gd:Report",
            "attrs": {"title": "Orders"},
            "local": "r",
            "edges": [{"rel": "gd:about_table", "target": str(customers.id)}],
        }
    )
    tables.parts = {"orders": ("table", ("id", "total"))}  # customers is no longer produced
    head = seq(graph)
    report = graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    assert seq(graph) == head  # nothing was written half
    assert {t.name: t.blocked for t in report.extractors}["sqlite"] == 1
    assert [p["code"] for p in report.problems] == ["blocked_by_references"]
    record = graph.records(SQLITE_CLASS)[db]
    assert record.attrs["extractor_version"] == 1  # the record stays at its old version
    assert graph.parts(PART_CLASS)[f"sqlite/{db}/orders"].id == orders.id
    # once the report is retracted the next run goes through
    env.apply({"op": "retract", "id": "REP-0001"}, reason="obsolete")
    graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    assert graph.records(SQLITE_CLASS)[db].attrs["extractor_version"] == 2
    assert f"sqlite/{db}/customers" not in graph.parts(PART_CLASS)


def _guarded_model(tmp_path: Path) -> CompiledModel:
    directory = tmp_path / "ontology"
    directory.mkdir()
    (directory / "guarded.yaml").write_text(GUARDED)
    outcome = compile_bundle("guarded", LayeredSource(DirectorySource(directory)))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


GUARDED = """\
format: graph_cli.ir/1
module:
  id: guarded
  prefix: gd
  version: 1.0.0
  summary: A graph that points at parts of database files and refuses to lose them.
  imports:
    - {module: files, version: "^1.1"}
    - {module: files_sqlite, version: "^1.0"}

classes:
  Report:
    summary: A report about a table of a database file.
    display_prefix: REP
    attributes:
      title:
        summary: Title.
        type: string
        required: true
        role: label

relations:
  about_table:
    summary: The table a report is about.
    domain: Report
    range: files_sqlite:SqliteObject
    on_target_inactive: refuse
"""


READ_ONLY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: read}
no_project: {"*": read}
"""


class _Policy:
    def __init__(self, graph: Graph, text: str) -> None:
        graphs = {graph.env.name: graph.env.entry.id}
        document = compile_policy(
            text.encode(), file="installation.yaml", graphs=graphs, graph=None, roles=frozenset()
        ).document
        self.policy = policy_set(document)

    def current(self, graph: object) -> object:
        return self.policy

    def refresh(self) -> None:
        pass


def test_a_caller_without_write_rights_ends_the_run_at_the_first_denial(graph: Graph) -> None:
    graph.add("one_page.pdf")
    graph.add("notes.txt")
    writer = graph.env.make_service(_Policy(graph, READ_ONLY))
    service = graph.service(registry(pdf_stub()), writer=writer)
    head = seq(graph)
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request())
    assert caught.value.code.startswith("access.")
    assert seq(graph) == head


def test_objects_without_a_local_path_are_read_through_a_private_copy(tmp_path: Path) -> None:
    """Object storage has no path: the bytes stream into a private file for one run."""
    with FakeS3() as fake:
        env = ServiceEnv(
            "s3",
            "notes_and_sources",
            tmp_path / "root",
            version="1.3.0",
            object_store=fake.settings(),
        )
        graph = Graph(env)
        pdf = graph.add("one_page.pdf")
        graph.add("notes.txt")
        report = graph.service(registry(pdf_stub())).extract(graph.request())
        assert report.problems == []
        assert set(graph.records(PDF_CLASS)) == {pdf}
        assert len(graph.records(FORMAT_CLASS)) == 2
        assert [p for p in Path(tempfile.gettempdir()).glob("graph_cli_extract_*")] == []


def test_the_real_extractor_child_runs_on_every_backend(graph: Graph) -> None:
    """No stub runner: ``format`` runs in a child process on the stored bytes."""
    binary = graph.add("binary_renamed.csv")
    graph.add("database.sqlite")
    service = FileMetadataService(
        catalog=graph.env.catalog,
        writer=graph.env.service,
        ids=graph.env.ids,
        policy=BuiltinPolicySource(),
    )
    report = service.extract(graph.request())
    assert report.problems == [] and report.transient == []
    record = graph.records(FORMAT_CLASS)[binary]
    assert record.attrs["detected_media_type"] == "application/octet-stream"
    assert record.attrs["media_type_mismatch"] is True  # recorded text/csv, bytes are binary
    assert record.attrs["size"] == 512
    sqlite = next(r for f, r in graph.records(FORMAT_CLASS).items() if f != binary)
    assert sqlite.attrs["detected_media_type"] == "application/vnd.sqlite3"


# ---- versions: no downgrade, stale not_applicable ---------------------------------------


def test_an_older_install_never_downgrades_a_newer_record(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(2))).extract(graph.request())
    before = graph.records(PDF_CLASS)[pdf]
    assert before.attrs["extractor_version"] == 2
    head = seq(graph)
    report = graph.service(registry(pdf_stub(1))).extract(graph.request())
    assert seq(graph) == head  # nothing written
    assert graph.records(PDF_CLASS)[pdf].attrs["extractor_version"] == 2
    tally = next(t for t in report.extractors if t.name == "pdf")
    assert tally.newer_version == 1 and tally.created == tally.updated == 0
    assert report.newer_versions == [
        {"extractor": "pdf", "file": pdf, "stored_version": 2, "version": 1}
    ]
    assert report.to_json()["newer_version_count"] == 1
    again = graph.service(registry(pdf_stub(2))).extract(graph.request())
    assert again.newer_versions == [] and seq(graph) == head


def test_a_newer_record_is_not_turned_into_not_applicable_either(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(2))).extract(graph.request())
    head = seq(graph)
    graph.service(registry(pdf_stub(1, accepts=False))).extract(graph.request())
    assert seq(graph) == head
    assert graph.records(PDF_CLASS)[pdf].attrs["status"] == "ok"


def test_a_stale_not_applicable_record_runs_again_when_the_extractor_applies_again(
    graph: Graph,
) -> None:
    """The format of a file is detected differently by a later version: ``pdf`` ruled it out
    (``not_applicable``), then accepts it again at the same version; the record is stale."""
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    graph.service(registry(pdf_stub(1, accepts=False))).extract(graph.request())
    assert graph.records(PDF_CLASS)[pdf].attrs["status"] == "not_applicable"
    report = graph.service(registry(pdf_stub(1))).extract(graph.request())
    record = graph.records(PDF_CLASS)[pdf]
    assert record.attrs["status"] == "ok" and record.attrs["page_count"] == 1
    assert next(t for t in report.extractors if t.name == "pdf").updated == 1
    head = seq(graph)
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    assert seq(graph) == head


# ---- errors: what ends a run and what does not ------------------------------------------


class _Writer:
    """A write service that raises ``code`` for the document of the ``nth`` call."""

    def __init__(self, inner: WriteService, code: str, nth: int) -> None:
        self.inner, self.code, self.nth, self.calls = inner, code, nth, 0
        self.timings = inner.timings

    def apply(self, request: WriteRequest) -> WriteResult:
        self.calls += 1
        if self.calls == self.nth:
            raise GraphCliError(self.code, "scripted")
        return self.inner.apply(request)


def test_a_lock_timeout_ends_the_run_and_what_was_written_stays(graph: Graph) -> None:
    for name in ("one_page.pdf", "notes.txt", "table.csv"):
        graph.add(name)
    writer = _Writer(graph.env.service, "backend.lock_timeout", 2)
    service = graph.service(registry(pdf_stub()), writer=writer)  # type: ignore[arg-type]
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request())
    assert caught.value.code == "backend.lock_timeout"
    assert writer.calls == 2  # the third document was not tried
    assert len(graph.records(FORMAT_CLASS)) == 1
    graph.service(registry(pdf_stub())).extract(graph.request())  # a rerun goes on
    assert len(graph.records(FORMAT_CLASS)) == 3


@pytest.mark.parametrize(
    "code", ["write.invalid", "reconcile.shape", "changeset.malformed", "conflict.seq_mismatch"]
)
def test_a_refused_document_is_listed_and_the_run_goes_on(graph: Graph, code: str) -> None:
    graph.add("one_page.pdf")
    graph.add("notes.txt")
    writer = _Writer(graph.env.service, code, 1)
    report = graph.service(registry(pdf_stub()), writer=writer).extract(  # type: ignore[arg-type]
        graph.request()
    )
    assert [p["code"] for p in report.problems] == [code]
    assert len(graph.records(FORMAT_CLASS)) == 1  # the other format record was written


@pytest.mark.parametrize("code", ["backend.unavailable", "graph.archived", "internal.error"])
def test_other_errors_end_the_run(graph: Graph, code: str) -> None:
    graph.add("notes.txt")
    writer = _Writer(graph.env.service, code, 1)
    service = graph.service(registry(), writer=writer)  # type: ignore[arg-type]
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request())
    assert caught.value.code == code


@pytest.mark.doc_claim("security_model.extraction_object_store_checked")
def test_a_corrupt_stored_copy_is_transient_for_that_file_only(tmp_path: Path) -> None:
    with FakeS3() as fake:
        env = ServiceEnv(
            "s3",
            "notes_and_sources",
            tmp_path / "root",
            version="1.3.0",
            object_store=fake.settings(),
        )
        graph = Graph(env)
        bad = graph.add("one_page.pdf")
        good = graph.add("notes.txt")
        digest = env.snapshot().get(EntityId(parse_id(bad)))
        assert digest is not None
        stored = str(digest.attrs["content"]).split(":")[-1]
        (key,) = [k for k in fake.keys() if stored in k]  # noqa: SIM118
        original = fake.get(key)
        assert original is not None
        fake.corrupt_get(key, bytes(len(original)))  # same length, other bytes
        extractors = registry(pdf_stub())
        parsed: list[bytes] = []  # what the parsers were handed

        def runner(
            name: str, path: Path, recorded: str, limits: ExtractLimits
        ) -> ExtractionResult | Transient:
            parsed.append(path.read_bytes())
            return extractors[name].run(RunInput(path, recorded))

        report = graph.service(extractors, runner=runner).extract(graph.request())
        assert [t["file"] for t in report.transient] == [bad]  # once: the format run only
        assert original not in parsed and bytes(len(original)) not in parsed  # no parser saw it
        assert parsed  # the good file was parsed
        assert report.problems == []
        assert set(graph.records(FORMAT_CLASS)) == {good}  # the run went on to the other file
        assert graph.records(PDF_CLASS) == {}
        assert [p for p in Path(tempfile.gettempdir()).glob("graph_cli_extract_*")] == []


# ---- records follow their file: every class, the selection ------------------------------


def test_the_records_of_an_extractor_whose_module_is_outdated_follow_their_file(
    graph: Graph,
) -> None:
    pdf = graph.add("one_page.pdf")
    graph.service(registry(pdf_stub())).extract(graph.request())
    assert pdf in graph.records(PDF_CLASS)
    graph.env.apply({"op": "retract", "id": pdf}, reason="not wanted")
    outdated = registry(pdf_stub(1, module_min="9.0.0"))
    report = graph.service(outdated).extract(graph.request())
    assert [m.extractor for m in report.module_outdated] == ["pdf"]
    assert graph.records(PDF_CLASS) == {}  # retracted although pdf cannot run here
    assert graph.records(FORMAT_CLASS) == {}


def test_the_selection_limits_the_cleanup_of_records_too(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    service = graph.service(registry(pdf_stub()))
    service.extract(graph.request())
    graph.env.apply({"op": "retract", "id": pdf}, reason="not wanted")
    report = service.extract(graph.request(extractors=("format",)))
    assert report.retracted == 1
    assert graph.records(FORMAT_CLASS) == {}
    assert pdf in graph.records(PDF_CLASS)  # not selected: left for a run that selects it
    assert service.extract(graph.request()).retracted == 1
    assert graph.records(PDF_CLASS) == {}


def test_a_new_record_and_its_new_parts_are_one_write(graph: Graph) -> None:
    db = graph.add("database.sqlite")
    tables = Tables({"orders": ("table", ("id",)), "customers": ("table", ("id",))})
    before = {tx.seq for tx in graph.env.txs()}
    graph.service(registry(sqlite_stub(tables))).extract(graph.request())
    sqlite_txs = [
        tx for tx in graph.env.txs() if tx.seq not in before and tx.reason == "extract sqlite@1"
    ]
    assert len(sqlite_txs) == 1
    assert len(graph.parts(PART_CLASS)) == 2 and db in graph.records(SQLITE_CLASS)


def test_a_part_is_retired_while_its_record_stays(graph: Graph) -> None:
    db = graph.add("database.sqlite")
    tables = Tables({"orders": ("table", ("id",)), "customers": ("table", ("id",))})
    graph.service(registry(sqlite_stub(tables, 1))).extract(graph.request())
    record = graph.records(SQLITE_CLASS)[db]
    tables.parts = {"orders": ("table", ("id",))}
    graph.service(registry(sqlite_stub(tables, 2))).extract(graph.request())
    assert set(graph.parts(PART_CLASS)) == {f"sqlite/{db}/orders"}
    after = graph.records(SQLITE_CLASS)[db]
    assert after.id == record.id and after.attrs["extractor_version"] == 2


def test_a_failed_and_a_not_applicable_record_still_point_at_their_file(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    text = graph.add("notes.txt")
    service = graph.service(registry(pdf_stub(1, fail=lambda data: "pdf.unreadable")))
    service.extract(graph.request())
    failed = graph.records(PDF_CLASS)[pdf]
    assert failed.attrs["status"] == "failed"
    assert [str(e.target) for e in failed.edges if e.rel == "files:metadata_of"] == [pdf]
    graph.service(registry(pdf_stub(2, accepts=False))).extract(graph.request())
    gone = graph.records(PDF_CLASS)[pdf]
    assert gone.attrs["status"] == "not_applicable"
    assert [str(e.target) for e in gone.edges if e.rel == "files:metadata_of"] == [pdf]
    assert text not in graph.records(PDF_CLASS)


# ---- who may run it and what the caller learns -------------------------------------------


def test_a_run_over_the_graph_needs_the_maintain_permission(graph: Graph) -> None:
    graph.add("one_page.pdf")
    head = seq(graph)
    service = graph.service(registry(pdf_stub()))
    for files in (None, ("FILE-0001",)):
        with pytest.raises(GraphCliError) as caught:
            service.extract(graph.request(actor=graph.env.actor, files=files))
        assert caught.value.code.startswith("access.")
    assert seq(graph) == head


def test_an_add_time_run_extracts_the_new_file_and_reports_only_it(graph: Graph) -> None:
    first = graph.add("one_page.pdf")
    second = graph.add("table.csv")
    service = graph.service(registry(pdf_stub()))
    report = service.extract(
        graph.request(actor=graph.env.actor, files=(second,), maintenance=False, named_run=False)
    )
    assert report.files == 1 and report.run is None
    assert set(graph.records(FORMAT_CLASS)) == {second}  # nothing about ``first``
    assert first not in graph.records(PDF_CLASS)
    assert report.retracted == 0


def test_an_add_time_run_without_a_file_is_a_bug(graph: Graph) -> None:
    with pytest.raises(GraphCliError) as caught:
        graph.service(registry()).extract(graph.request(maintenance=False))
    assert caught.value.code == "internal.error"


MAINTAINER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
HIDE_SCANS = """\
schema: graph_cli.policy/1
id: graph
types:
  "nas:Scan": none
"""


def _limited(graph: Graph) -> _Limited:
    graphs = {graph.env.name: graph.env.entry.id}
    installation = compile_policy(
        MAINTAINER.encode(), file="installation.yaml", graphs=graphs, graph=None, roles=frozenset()
    ).document
    layer = compile_policy(
        HIDE_SCANS.encode(),
        file="graph.yaml",
        graphs=graphs,
        graph=graph.env.entry.id,
        roles=frozenset(),
    ).document
    return _Limited(policy_set(installation, layer))


class _Limited:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def _caller(graph: Graph) -> ActorContext:
    return graph.env.actor  # the app: it never maintains, and the layer limits what it sees


def test_a_caller_who_does_not_see_a_file_type_cannot_run_over_the_graph(graph: Graph) -> None:
    """Whole-graph and named-file runs are maintenance: refused before any file is looked up,
    so a hidden file and one that does not exist are answered alike."""
    hidden = graph.add("table.csv", type="nas:Scan")
    policy = _limited(graph)
    service = graph.service(
        registry(pdf_stub()), policy=policy, writer=graph.env.make_service(policy)
    )
    ghost = str(graph.env.ids.new_id())
    answers = []
    for files in (None, (hidden,), (ghost,)):
        with pytest.raises(GraphCliError) as caught:
            service.extract(graph.request(actor=_caller(graph), files=files))
        answers.append((caught.value.code, caught.value.details))
    assert len({str(a) for a in answers}) == 1 and answers[0][0].startswith("access.")
    assert graph.records(FORMAT_CLASS) == {}


def test_an_add_time_run_reports_only_the_callers_file_and_hides_the_others(
    graph: Graph,
) -> None:
    visible = graph.add("one_page.pdf")
    hidden = graph.add("table.csv", type="nas:Scan")
    policy = _limited(graph)
    service = graph.service(
        registry(pdf_stub()), policy=policy, writer=graph.env.make_service(policy)
    )
    request = graph.request(
        actor=_caller(graph), files=(visible,), maintenance=False, named_run=False
    )
    report = service.extract(request)
    assert report.files == 1
    assert set(graph.records(FORMAT_CLASS)) == {visible}
    shown = json.dumps(report.to_json())
    assert hidden not in shown and "text/csv" not in shown


def test_an_add_time_reference_to_a_hidden_file_is_not_found(graph: Graph) -> None:
    hidden = graph.add("table.csv", type="nas:Scan")
    policy = _limited(graph)
    service = graph.service(
        registry(pdf_stub()), policy=policy, writer=graph.env.make_service(policy)
    )
    ghost = str(graph.env.ids.new_id())
    answers = []
    for ref in (hidden, ghost):
        request = graph.request(actor=_caller(graph), files=(ref,), maintenance=False)
        with pytest.raises(GraphCliError) as caught:
            service.extract(request)
        answers.append((caught.value.code, caught.value.message.replace(ref, "<ref>")))
    assert answers[0] == answers[1] and answers[0][0] == "entity.not_found"
    assert graph.records(FORMAT_CLASS) == {}


# ---- what an add-time run answers, and how far a run that ended early got -----------------


def test_an_add_time_run_reports_the_records_of_its_file_and_the_last_seq(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    graph.add("table.csv")
    service = graph.service(registry(pdf_stub()))
    report = service.extract(
        graph.request(actor=graph.env.actor, files=(pdf,), maintenance=False, named_run=False)
    )
    assert [(m["extractor"], m["version"], m["status"]) for m in report.metadata] == [
        ("format", 1, "ok"),
        ("pdf", 1, "ok"),
    ]
    stored = graph.records(PDF_CLASS)[pdf]
    assert report.metadata[1]["id"] == str(stored.id)
    assert report.last_seq == seq(graph) and report.warnings() == ()
    again = service.extract(
        graph.request(actor=graph.env.actor, files=(pdf,), maintenance=False, named_run=False)
    )
    assert again.metadata == report.metadata and again.last_seq is None  # nothing written


def test_a_failed_record_is_answered_with_its_problem(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")
    service = graph.service(registry(pdf_stub(1, fail=lambda data: "pdf.unreadable")))
    report = service.extract(
        graph.request(actor=graph.env.actor, files=(pdf,), maintenance=False, named_run=False)
    )
    failed = next(m for m in report.metadata if m["extractor"] == "pdf")
    assert failed["status"] == "failed" and failed["problem"] == "pdf.unreadable"


def test_what_could_not_be_done_is_one_warning(graph: Graph) -> None:
    pdf = graph.add("one_page.pdf")

    def runner(
        name: str, path: Path, recorded: str, limits: ExtractLimits
    ) -> ExtractionResult | Transient:
        if name == "pdf":
            return Transient("the extractor was killed")
        return registry(pdf_stub())[name].run(RunInput(path, recorded))

    service = graph.service(registry(pdf_stub()), runner=runner)
    report = service.extract(
        graph.request(actor=graph.env.actor, files=(pdf,), maintenance=False, named_run=False)
    )
    assert [m["extractor"] for m in report.metadata] == ["format"]  # nothing recorded for pdf
    (warning,) = report.warnings()
    assert warning.code == "file.extraction_incomplete"
    assert warning.details == {"extractors": ["pdf"]}


def test_a_run_that_ends_early_says_how_far_it_got(graph: Graph) -> None:
    for name in ("one_page.pdf", "notes.txt", "table.csv"):
        graph.add(name)
    writer = _Writer(graph.env.service, "backend.lock_timeout", 3)
    service = graph.service(registry(pdf_stub()), writer=writer)  # type: ignore[arg-type]
    with pytest.raises(GraphCliError) as caught:
        service.extract(graph.request())
    progress = caught.value.details["progress"]
    assert isinstance(progress, dict)
    assert progress["files"] == 3 and progress["files_done"] == 1
    assert progress["records_written"] == 2 and progress["last_seq"] == seq(graph)


def test_an_add_time_run_of_a_caller_who_may_not_write_records_is_quiet(graph: Graph) -> None:
    """The dry run finds the missing right unaudited: the documents are skipped, the report
    names the right, and no event is appended to any audit stream."""
    pdf = graph.add("one_page.pdf")
    writer = graph.env.make_service(_Policy(graph, READ_ONLY))
    service = graph.service(registry(pdf_stub()), writer=writer)
    events = list(graph.env.audit.read_lines(INSTALLATION_STREAM))
    head = seq(graph)
    report = service.extract(
        graph.request(actor=graph.env.actor, files=(pdf,), maintenance=False, named_run=False)
    )
    assert [(d["extractor"], d["action"]) for d in report.denied] == [
        ("format", "write.create"),
        ("pdf", "write.create"),
    ]
    assert report.metadata == [] and seq(graph) == head
    (warning,) = report.warnings()
    assert warning.code == "file.extraction_incomplete"
    assert "you may not write its metadata records (write.create on graph:" in warning.message
    assert warning.details["extractors"] == ["format", "pdf"]
    assert list(graph.env.audit.read_lines(INSTALLATION_STREAM)) == events  # nothing audited
