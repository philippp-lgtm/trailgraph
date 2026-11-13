"""A version directory with a ``migration.yaml`` next to its modules: module detection, the
reserved module id, workspace entries and the remote upload (contracts B7.1)."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.fixtures import ONTOLOGIES

from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.maintenance_cli.remote import upload_tree
from graph_cli_admin.services_admin import error_codes, sources
from graph_cli_admin.services_admin.ontology import compile_source, main_module, read_migration

INVENTORY = (ONTOLOGIES / "inventory" / "1.1.0" / "inventory.yaml").read_bytes()
MIGRATION = b"format: graph_cli.migration/1\nfrom: 1.1.0\nsummary: Test.\n"


def version_dir(tmp_path: Path, *, migration: bool = True) -> Path:
    directory = tmp_path / "2.0.0"
    directory.mkdir(parents=True)
    (directory / "inventory.yaml").write_bytes(INVENTORY)
    if migration:
        (directory / "migration.yaml").write_bytes(MIGRATION)
    return directory


def test_the_main_module_ignores_the_migration_file(tmp_path: Path) -> None:
    assert main_module(version_dir(tmp_path), None) == "inventory"


def test_a_directory_with_only_a_migration_has_no_module(tmp_path: Path) -> None:
    directory = tmp_path / "2.0.0"
    directory.mkdir(parents=True)
    (directory / "migration.yaml").write_bytes(MIGRATION)
    with pytest.raises(GraphCliError) as caught:
        main_module(directory, None)
    assert caught.value.code == "usage.missing_argument"


def test_migration_cannot_be_named_as_the_main_module(tmp_path: Path) -> None:
    with pytest.raises(GraphCliError) as caught:
        main_module(version_dir(tmp_path), "migration")
    assert caught.value.code == "usage.invalid_argument"


def test_the_migration_file_is_no_module_of_the_release(tmp_path: Path) -> None:
    with_file = compile_source(version_dir(tmp_path))
    without = compile_source(version_dir(tmp_path / "plain", migration=False))
    assert with_file.model.artifact_hash == without.model.artifact_hash
    assert list(with_file.sources) == ["inventory"]


def test_a_module_cannot_import_migration(tmp_path: Path) -> None:
    directory = version_dir(tmp_path)
    text = INVENTORY.decode().replace(
        "  summary: Physical items",
        '  imports:\n    - {module: migration, version: "^1.0"}\n  summary: Physical items',
        1,
    )
    (directory / "inventory.yaml").write_text(text)
    error_codes.register()
    with pytest.raises(GraphCliError) as caught:
        compile_source(directory)
    assert caught.value.code == "admin.model_invalid"
    findings = caught.value.details["findings"]
    assert [f["code"] for f in findings] == ["model.referential.unknown_import"]


def test_read_migration(tmp_path: Path) -> None:
    assert read_migration(version_dir(tmp_path)) == MIGRATION
    assert read_migration(version_dir(tmp_path / "plain", migration=False)) is None


def test_the_migration_file_belongs_to_the_version_directory_entry() -> None:
    assert sources.workspace_entry("ontology/2.0.0/migration.yaml") == "ontology/2.0.0"
    assert sources.workspace_entry("ontology/2.0.0/inventory.yaml") == "ontology/2.0.0"
    assert sources.workspace_entry("ontology/2.0.0/migration.txt") is None


def test_a_workspace_write_carries_the_migration_file(tmp_path: Path) -> None:
    files = {
        "ontology/2.0.0/inventory.yaml": INVENTORY,
        "ontology/2.0.0/migration.yaml": MIGRATION,
        sources.SOURCE_FILE: b"{}",
    }
    sources.write_workspace(tmp_path / "workspace", files, graph_ref="example_graph", force=False)
    written = tmp_path / "workspace" / "ontology" / "2.0.0"
    assert sorted(p.name for p in written.iterdir()) == ["inventory.yaml", "migration.yaml"]


class FakeClient:
    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    def call(
        self, method: str, path: str, *, body: Any, length: int = 0, headers: Any = None
    ) -> Any:
        data = body.read() if hasattr(body, "read") else body
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.uploads[digest] = data
        return SimpleNamespace(status=200, json=lambda: {"upload": digest, "size": len(data)})


def test_the_remote_upload_of_a_version_directory_carries_the_migration_file(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    reference = upload_tree(SimpleNamespace(client=client), str(version_dir(tmp_path)), "source")
    assert sorted(reference["files"]) == ["inventory.yaml", "migration.yaml"]
    digest = reference["files"]["migration.yaml"]
    assert client.uploads[digest] == MIGRATION
