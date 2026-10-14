"""FileCatalog: registry scan, resolution, release files, store_for (B12.4, B12.8; plan M6)."""

from pathlib import Path

import pytest
from support.fixtures import compiled_model
from support.graphs import Installation, pin_of
from support.trees import tree_hash

from graph_cli.catalog.file_catalog import FileCatalog, StorageSettings
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def edit_manifest(inst: Installation, graph: GraphId, old: str, new: str) -> None:
    path = inst.root / "graphs" / str(graph) / "manifest.toml"
    path.write_text(path.read_text().replace(old, new, 1))


def test_lists_graphs_by_name(inst: Installation) -> None:
    zeta = inst.create("zeta")
    alpha = inst.create("alpha", ontology="notes_and_sources")
    (inst.root / "graphs" / f".tmp-{inst.ids.new_id()}").mkdir()
    catalog = FileCatalog(inst.root)
    assert [e.name for e in catalog.list_graphs()] == ["alpha", "zeta"]
    assert [e.id for e in catalog.list_graphs()] == [alpha, zeta]
    assert catalog.list_graphs()[0].pin == pin_of(compiled_model("notes_and_sources"))


def test_archived_graphs_are_listed_on_request(inst: Installation) -> None:
    graph = inst.create("old")
    edit_manifest(inst, graph, 'state = "active"', 'state = "archived"')
    catalog = FileCatalog(inst.root)
    assert catalog.list_graphs() == []
    assert [e.name for e in catalog.list_graphs(include_archived=True)] == ["old"]
    assert catalog.resolve("old").state == "archived"


def test_resolve_by_name_or_id(inst: Installation) -> None:
    graph = inst.create("example_graph")
    catalog = FileCatalog(inst.root)
    assert catalog.resolve("example_graph").id == graph
    assert catalog.resolve(str(graph)).name == "example_graph"
    assert catalog.resolve(str(graph).upper()).id == graph
    for ref in ("other", str(inst.ids.new_id())):
        with pytest.raises(GraphCliError) as caught:
            catalog.resolve(ref)
        assert caught.value.code == "graph.not_found"
        assert caught.value.details == {"ref": ref}


def test_release_files(inst: Installation) -> None:
    graph = inst.create()
    catalog = FileCatalog(inst.root)
    version = catalog.resolve(str(graph)).pin.version
    assert catalog.read_release_file(graph, version, "compiled.json").startswith(b"{")
    with pytest.raises(GraphCliError) as caught:
        catalog.read_release_file(graph, version, "diff.json")
    assert caught.value.code == "ontology.artifact_missing"


def test_store_for(inst: Installation) -> None:
    graph = inst.create()
    catalog = FileCatalog(inst.root, settings=StorageSettings(1.5, "fsync"))
    read, write = catalog.store_for(graph)
    assert read is write
    assert read.head().seq == 0
    edit_manifest(inst, graph, 'backend = "files"', 'backend = "sqlite"')
    with pytest.raises(GraphCliError) as caught:
        catalog.store_for(graph)
    assert caught.value.code == "format.unsupported_version"


def test_unknown_manifest_keys_are_collected_once(inst: Installation) -> None:
    graph = inst.create()
    edit_manifest(inst, graph, "[graph]\n", '[graph]\ncolour = "x"\n')
    catalog = FileCatalog(inst.root)
    catalog.list_graphs()
    catalog.resolve("example_graph")
    assert [w.details["key"] for w in catalog.warnings] == ["graph.colour"]


def test_a_broken_manifest_is_config_invalid(inst: Installation) -> None:
    graph = inst.create()
    edit_manifest(inst, graph, 'state = "active"', 'state = "lost"')
    with pytest.raises(GraphCliError) as caught:
        FileCatalog(inst.root).list_graphs()
    assert caught.value.code == "config.invalid"


def test_missing_root_is_unavailable_and_creates_nothing(tmp_path: Path) -> None:
    catalog = FileCatalog(tmp_path / "missing")
    before = tree_hash(tmp_path)
    with pytest.raises(GraphCliError) as caught:
        catalog.list_graphs()
    assert caught.value.code == "backend.unavailable"
    assert tree_hash(tmp_path) == before


def test_empty_root_has_no_graphs(tmp_path: Path) -> None:
    assert FileCatalog(tmp_path).list_graphs() == []
