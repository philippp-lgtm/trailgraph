"""Which graphs a FileStore opens: one check for the catalog and the maintenance port.

``FileCatalog.file_store`` (app, server) and ``FileStoreMaintenance.store`` (graph-admin,
``files verify|gc``, ``audit verify``) refuse the same manifests: another backend or
layout, an unknown manifest format or storage feature. A graph whose stored files are in
an object store opens without object store settings; its blob access is refused instead
(``unusable_object_store``), naming what is missing.
"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from support.graphs import Installation

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.catalog import StoreInfo
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

STORE_ID = "4b0c9f3e-2d1a-4c5b-9e8f-7a6b5c4d3e2f"
SETTINGS = ObjectStoreSettings(
    bucket="example-bucket",
    region="example-region",
    credentials=ObjectStoreCredentials("EXAMPLEKEYID", "example-secret-value"),
)
PROBLEM = GraphCliError(
    "config.invalid",
    "config.toml: files.s3.bucket is missing; set it or GRAPH_CLI_S3_BUCKET",
    details={"file": "config.toml", "key": "files.s3.bucket"},
)

Opener = Callable[[Installation, GraphId], object]


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def manifest_path(inst: Installation, graph: GraphId) -> Path:
    return inst.root / "graphs" / str(graph) / "manifest.toml"


def edit(inst: Installation, graph: GraphId, old: str, new: str) -> None:
    path = manifest_path(inst, graph)
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def move_to_object_store(inst: Installation, graph: GraphId) -> None:
    layout = GraphLayout(inst.root / "graphs" / str(graph))
    current = read_manifest(layout)[0]
    moved = replace(current, store=StoreInfo("files", 1, "s3", STORE_ID))
    layout.manifest.write_bytes(format_manifest(moved))


def by_catalog(
    settings: ObjectStoreSettings | None = None, problem: GraphCliError | None = None
) -> Opener:
    def open_(inst: Installation, graph: GraphId) -> object:
        catalog = FileCatalog(inst.root, object_store=settings, object_store_problem=problem)
        return catalog.file_store(graph)

    return open_


def by_maintenance(
    settings: ObjectStoreSettings | None = None, problem: GraphCliError | None = None
) -> Opener:
    def open_(inst: Installation, graph: GraphId) -> object:
        maintenance = FileStoreMaintenance(
            inst.root, object_store=settings, object_store_problem=problem
        )
        return maintenance.store(graph)

    return open_


def by_session(
    settings: ObjectStoreSettings | None = None, problem: GraphCliError | None = None
) -> Opener:
    def open_(inst: Installation, graph: GraphId) -> object:
        maintenance = FileStoreMaintenance(
            inst.root, object_store=settings, object_store_problem=problem
        )
        with maintenance.maintenance_session(graph, timeout_s=5) as session:
            return session.verify()

    return open_


OPENERS = {"catalog": by_catalog, "maintenance": by_maintenance, "session": by_session}


@pytest.fixture(params=sorted(OPENERS))
def opener(request: pytest.FixtureRequest) -> Callable[..., Opener]:
    return OPENERS[request.param]


def refusal(inst: Installation, graph: GraphId, open_: Opener) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        open_(inst, graph)
    return caught.value


def test_a_local_graph_opens(inst: Installation, opener: Callable[..., Opener]) -> None:
    graph = inst.create()
    opener()(inst, graph)
    opener(SETTINGS)(inst, graph)  # settings change nothing for a local graph
    opener(None, PROBLEM)(inst, graph)  # nor does a broken [files.s3]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('backend = "files"', 'backend = "sqlite"'),
        ("format_version = 1\n\n[alias", "format_version = 2\n\n[alias"),
        ("format_version = 1\n\n[graph]", "format_version = 3\n\n[graph]"),
        (
            "format_version = 1\n\n[graph]",
            'format_version = 2\nrequires = ["sealed"]\n\n[graph]',
        ),
    ],
    ids=["backend", "layout", "manifest_format", "feature"],
)
def test_unsupported_manifests_are_refused_by_every_opener(
    inst: Installation, opener: Callable[..., Opener], old: str, new: str
) -> None:
    graph = inst.create()
    edit(inst, graph, old, new)
    assert refusal(inst, graph, opener(SETTINGS)).code == "format.unsupported_version"


def test_an_object_store_graph_opens_without_settings(
    inst: Installation, opener: Callable[..., Opener]
) -> None:
    """Reads that touch no file never need the object store (O4)."""
    graph = inst.create()
    move_to_object_store(inst, graph)
    opener()(inst, graph)
    opener(None, PROBLEM)(inst, graph)
    opener(SETTINGS)(inst, graph)


def blob_refusal(store: object) -> GraphCliError:
    assert isinstance(store, FileStore)
    with pytest.raises(GraphCliError) as caught:
        store.blob("sha256:" + "0" * 64)
    return caught.value


@pytest.mark.parametrize("opener_name", ["catalog", "maintenance"])
def test_blob_access_without_settings_names_what_is_missing(
    inst: Installation, opener_name: str
) -> None:
    graph = inst.create()
    move_to_object_store(inst, graph)
    opener = OPENERS[opener_name]
    error = blob_refusal(opener(None, PROBLEM)(inst, graph))
    assert error.code == "backend.unavailable"
    assert str(graph) in error.message
    assert "files.s3.bucket is missing" in error.message
    assert error.details == {
        "graph_id": str(graph),
        "blob_location": "s3",
        "store_id": STORE_ID,
        "cause": "config.invalid",
        "key": "files.s3.bucket",
    }
    plain = blob_refusal(opener()(inst, graph))
    assert (plain.code, plain.details.get("cause")) == ("backend.unavailable", None)
    assert "no object store is configured" in plain.message


def test_disagreeing_requires_is_refused_by_the_catalog(inst: Installation) -> None:
    graph = inst.create()
    move_to_object_store(inst, graph)
    edit(inst, graph, 'requires = ["blobs.s3"]\n', "")
    with pytest.raises(GraphCliError) as caught:
        by_catalog(SETTINGS)(inst, graph)
    assert (caught.value.code, caught.value.details["key"]) == ("config.invalid", "requires")


def test_maintenance_leaves_an_unreadable_manifest_to_verify(inst: Installation) -> None:
    """``audit verify`` reports a broken or missing manifest instead of stopping."""
    graph = inst.create()
    manifest_path(inst, graph).write_text("broken = ")
    assert [f.code for f in by_session()(inst, graph)] == ["integrity.storage.pin_mismatch"]
    by_maintenance()(inst, graph)
    with pytest.raises(GraphCliError) as caught:
        by_catalog()(inst, graph)
    assert caught.value.code == "config.invalid"
