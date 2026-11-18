"""``files verify`` names records older than their extractor and modules imported too old
(file metadata plan M6, M12): the maintainer's service in process on a files graph."""

from __future__ import annotations

from pathlib import Path

from support.admin_context import admin_context
from support.file_metadata import Graph, pdf_stub, registry
from support.services import ServiceEnv

from graph_cli_admin.services_admin.file_extract import records_behind


def test_records_behind_their_extractor_are_counted(tmp_path: Path) -> None:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.3.0")
    graph = Graph(env)
    graph.add("one_page.pdf")
    graph.add("three_pages.pdf")
    graph.service(registry(pdf_stub(1))).extract(graph.request())
    ctx = admin_context(env.root)
    entry = ctx.catalog.resolve(env.name)
    assert records_behind(ctx, entry, registry(pdf_stub(1)))["behind"] == []
    found = records_behind(ctx, entry, registry(pdf_stub(2)))
    assert found["behind"] == [
        {"extractor": "pdf", "version": 2, "records": 2, "behind": 2, "newer": 0}
    ]


def test_a_module_imported_too_old_is_named(tmp_path: Path) -> None:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.3.0")
    ctx = admin_context(env.root)
    entry = ctx.catalog.resolve(env.name)
    found = records_behind(ctx, entry, registry(pdf_stub(1, module_min="9.0.0")))
    assert found["module_outdated"] == [
        {"extractor": "pdf", "module": "files_pdf", "have": "1.0.0", "need": "9.0.0"}
    ]
