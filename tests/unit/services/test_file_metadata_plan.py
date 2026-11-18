"""The decisions and documents of an extraction run, without I/O (file metadata plan, section 5)."""

from __future__ import annotations

import pytest
from support.file_metadata import Tables, pdf_stub, registry, sqlite_stub
from support.fixtures import compiled_model

from graph_cli.extractors.format import FORMAT
from graph_cli.extractors.model import ExtractionResult, PartResult
from graph_cli.kernel.changeset import ValueRef
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.services.file_metadata_plan import (
    RecordState,
    decide,
    enabled_extractors,
    inactive_changeset,
    record_changeset,
)

FILE = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
MODEL = compiled_model("notes_and_sources", "1.3.0")


def state(version: int = 1, status: str = "ok") -> RecordState:
    entity = EntityId(parse_id("0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8d0"))
    return RecordState(entity, f"pdf/{FILE}", version, status, {})


@pytest.mark.parametrize(
    ("detected", "record", "retry", "expected"),
    [
        ("application/pdf", None, False, "run"),
        ("application/pdf", state(1), False, "current"),
        ("application/pdf", state(0), False, "run"),
        ("application/pdf", state(1, "failed"), False, "current"),
        ("application/pdf", state(1, "failed"), True, "run"),
        ("application/pdf", state(1, "not_applicable"), False, "run"),
        ("application/pdf", state(0, "not_applicable"), False, "run"),
        ("application/pdf", state(2), False, "newer_version"),
        ("application/pdf", state(2, "failed"), True, "newer_version"),
        ("application/pdf", state(2, "not_applicable"), False, "newer_version"),
        ("text/plain", state(2), False, "newer_version"),
        ("text/plain", None, False, "already_not_applicable"),
        ("text/plain", state(1), False, "not_applicable"),
        ("text/plain", state(1, "failed"), False, "not_applicable"),
        ("text/plain", state(1, "not_applicable"), False, "already_not_applicable"),
    ],
)
def test_decide(detected: str, record: RecordState | None, retry: bool, expected: str) -> None:
    assert decide(pdf_stub(1), detected, "application/pdf", record, retry_failed=retry) == expected


def test_a_failed_record_of_an_older_version_runs_again_without_retry() -> None:
    assert decide(pdf_stub(2), "application/pdf", "x/y", state(1, "failed")) == "run"


def test_format_applies_to_every_file() -> None:
    assert decide(FORMAT, "", "text/csv", None) == "run"


def test_the_csv_exception_dispatches_on_the_recorded_type_too() -> None:
    from graph_cli.extractors.model import Extractor

    table = pdf_stub(1)
    csv = Extractor(
        name="table",
        version=1,
        module=table.module,
        record_class=table.record_class,
        accepts=lambda detected, recorded: (
            detected == "text/plain" and recorded in ("text/csv", "text/tab-separated-values")
        ),
        run=table.run,
        summary="x",
    )
    assert decide(csv, "text/plain", "text/csv", None) == "run"
    assert decide(csv, "text/plain", "application/octet-stream", None) == "already_not_applicable"


def test_enabled_extractors_follow_the_imported_modules() -> None:
    enabled, outdated = enabled_extractors(
        MODEL, registry(pdf_stub(), sqlite_stub(Tables()), pdf_stub(1, module_min="9.0.0"))
    )
    assert [e.name for e in enabled] == ["format", "sqlite"]  # the later pdf replaced the first
    assert [(m.extractor, m.need) for m in outdated] == [("pdf", "9.0.0")]
    bare = compiled_model("inventory")
    assert enabled_extractors(bare, registry(pdf_stub())) == ([], [])


def test_a_record_document_is_one_complete_description_of_the_record() -> None:
    result = ExtractionResult(status="ok", values={"page_count": 3, "encrypted": False})
    changeset = record_changeset(MODEL, pdf_stub(2), FILE, result, run="run-1")
    assert changeset.ops == ()
    assert changeset.reason == "extract pdf@2"
    source = changeset.source
    assert source is not None
    assert (source.name, source.version, source.run, source.kind) == (
        "pdf",
        "2",
        "run-1",
        "extractor",
    )
    (scope,) = changeset.reconcile
    assert (scope.type, scope.key, scope.under, scope.complete) == (
        "files_pdf:PdfMetadata",
        "key",
        FILE,
        False,
    )
    # every attribute but the key is the extractor's: a value a version leaves out is unset
    assert set(scope.fields) == {
        "encrypted",
        "extractor",
        "extractor_version",
        "page_count",
        "pdf_version",
        "problem",
        "status",
    }
    assert [e for e in scope.edges] == ["files:metadata_of"]
    (item,) = scope.items
    assert item.key == FILE
    assert item.attrs == {
        "extractor": "pdf",
        "extractor_version": 2,
        "status": "ok",
        "page_count": 3,
        "encrypted": False,
    }


def test_a_failed_document_carries_the_problem_and_no_values() -> None:
    changeset = record_changeset(MODEL, pdf_stub(), FILE, ExtractionResult.failed("pdf.unreadable"))
    (item,) = changeset.reconcile[0].items
    assert item.attrs == {
        "extractor": "pdf",
        "extractor_version": 1,
        "status": "failed",
        "problem": "pdf.unreadable",
    }
    assert changeset.source is not None and changeset.source.run is None


def test_a_not_applicable_document_sets_only_the_status() -> None:
    changeset = record_changeset(MODEL, pdf_stub(), FILE, None)
    (item,) = changeset.reconcile[0].items
    assert item.attrs == {"extractor": "pdf", "extractor_version": 1, "status": "not_applicable"}


def test_parts_are_a_second_complete_scope_the_record_points_at_by_key() -> None:
    result = ExtractionResult(
        status="ok",
        values={"table_count": 1},
        parts=(PartResult("orders", {"kind": "table", "column_count": 2}),),
    )
    changeset = record_changeset(MODEL, sqlite_stub(Tables()), FILE, result)
    record, parts = changeset.reconcile
    assert [e.rel for e in record.items[0].edges] == [
        "files:metadata_of",
        "files_sqlite:has_object",
    ]
    target = record.items[0].edges[1].target
    assert target == ValueRef("files_sqlite:SqliteObject", "key", f"sqlite/{FILE}/orders")
    assert (parts.type, parts.under, parts.complete, parts.allow_empty) == (
        "files_sqlite:SqliteObject",
        f"{FILE}/",
        True,
        True,
    )
    assert parts.allow_retire is not None and parts.allow_retire >= 500
    assert [(i.key, dict(i.attrs)) for i in parts.items] == [
        (f"{FILE}/orders", {"name": "orders", "kind": "table", "column_count": 2})
    ]
    assert "key" not in parts.fields and "name" in parts.fields


def test_an_extractor_without_parts_has_no_part_scope() -> None:
    changeset = record_changeset(MODEL, pdf_stub(), FILE, ExtractionResult(status="ok"))
    assert len(changeset.reconcile) == 1


def test_the_inactive_document_empties_both_scopes() -> None:
    changeset = inactive_changeset(MODEL, sqlite_stub(Tables()), FILE)
    assert changeset.reason == "extract sqlite@1: file not active"
    record, parts = changeset.reconcile
    for scope in (record, parts):
        assert scope.items == () and scope.complete and scope.allow_empty
    assert record.under == FILE and parts.under == f"{FILE}/"


def test_an_extractor_may_only_produce_attributes_of_its_classes() -> None:
    result = ExtractionResult(status="ok", values={"page_count": 3, "pages_total": 3})
    with pytest.raises(GraphCliError) as info:
        record_changeset(MODEL, pdf_stub(), FILE, result)
    assert info.value.code == "internal.error"
    assert info.value.details["names"] == ["pages_total"]
    parts = ExtractionResult(
        status="ok", parts=(PartResult("orders", {"kind": "table", "rows": 3}),)
    )
    with pytest.raises(GraphCliError) as info:
        record_changeset(MODEL, sqlite_stub(Tables()), FILE, parts)
    assert info.value.details["names"] == ["rows"]
    stray = ExtractionResult(status="ok", parts=(PartResult("orders", {}),))
    with pytest.raises(GraphCliError):
        record_changeset(MODEL, pdf_stub(), FILE, stray)  # parts without a part class
    # a failed or absent result carries no values to check
    assert record_changeset(MODEL, pdf_stub(), FILE, ExtractionResult.failed("pdf.unreadable"))
    assert record_changeset(MODEL, pdf_stub(), FILE, None)
