"""The standard modules of derived file metadata (file metadata plan, M2, M5, M6, M8)."""

from __future__ import annotations

import pytest
from support.fixtures import compiled_model

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES

KINDS = {
    "files_pdf": "files_pdf:PdfMetadata",
    "files_image": "files_image:ImageMetadata",
    "files_table": "files_table:TableMetadata",
    "files_sqlite": "files_sqlite:SqliteMetadata",
}


def _model(module_id: str) -> CompiledModel:
    model = compile_bundle(module_id, LayeredSource()).model
    assert model is not None
    return model


def test_the_kind_modules_are_builtin() -> None:
    assert set(KINDS) < BUILTIN_MODULES


def test_files_holds_the_metadata_base() -> None:
    model = _model("files")
    assert next(m for m in model.modules if m.id == "files").version == "1.1.0"
    metadata = model.classes["files:Metadata"]
    part = model.classes["files:MetadataPart"]
    assert metadata.abstract and part.abstract
    for definition in (metadata.attributes["key"], part.attributes["key"]):
        assert definition.unique and definition.required and definition.mutability == "immutable"
    assert metadata.attributes["extractor"].mutability == "immutable"
    assert model.classes["files:FormatMetadata"].extends == "files:Metadata"
    link = model.relations["files:metadata_of"]
    assert link.domain == ("files:Metadata",)
    assert link.range == ("files:File",)
    assert (link.out.min, link.out.max) == (1, 1)
    assert link.inverse_label == "has_metadata"
    assert link.on_target_inactive == "allow"


@pytest.mark.parametrize(("module_id", "record"), sorted(KINDS.items()))
def test_a_kind_module_imports_files_and_subclasses_the_record(module_id: str, record: str) -> None:
    model = _model(module_id)
    info = next(m for m in model.modules if m.id == module_id)
    assert info.version == "1.0.0"
    assert model.is_a(record, "files:Metadata")
    assert model.classes[record].abstract is False
    for status in ("ok", "failed", "not_applicable"):
        assert status in model.enums["files:ExtractionStatus"].value_names()


def test_sqlite_parts_hang_off_the_record_through_has_object() -> None:
    model = _model("files_sqlite")
    assert model.is_a("files_sqlite:SqliteObject", "files:MetadataPart")
    link = model.relations["files_sqlite:has_object"]
    assert link.domain == ("files_sqlite:SqliteMetadata",)
    assert link.range == ("files_sqlite:SqliteObject",)
    assert link.on_target_inactive == "allow"
    assert link.inverse_label == "object_of"


def test_a_graph_importing_every_kind_compiles_without_findings() -> None:
    imports = "\n".join(f'    - {{module: {m}, version: "^1.0"}}' for m in sorted(KINDS))
    text = (
        "format: graph_cli.ir/1\nmodule:\n  id: files_all\n  prefix: fa\n  version: 1.0.0\n"
        f"  summary: Every kind of file metadata.\n  imports:\n{imports}\n"
    )
    source = LayeredSource(MappingSource({"files_all": text.encode()}))
    outcome = compile_bundle("files_all", source)
    assert outcome.findings == ()


def test_the_toy_fixture_imports_two_kinds() -> None:
    model = compiled_model("notes_and_sources", "1.3.0")
    imported = {m.id for m in model.modules}
    assert {"files", "files_pdf", "files_sqlite"} <= imported
    assert "files_image" not in imported
    assert "files_pdf:PdfMetadata" in model.classes
