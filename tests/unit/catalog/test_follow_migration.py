"""The app's catalog reads a graph under its migration's target version (migration plan M3).

With ``follow_migration`` a graph whose migration tx is committed but whose pin is not switched
resolves to the target pin; graph-admin's catalog keeps the manifest's pin. A marker whose tx
is not in the log, and a marker the pin has reached, change nothing.
"""

from pathlib import Path

import pytest
from support.audit_scene import AuditedScene
from support.fixtures import compiled_model

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId


def scene(tmp_path: Path) -> tuple[AuditedScene, GraphId]:
    audited = AuditedScene(tmp_path / "root")
    graph_id = audited.create_audited("1.0.0")
    audited.write_entities(graph_id, 2)
    return audited, graph_id


def versions(audited: AuditedScene) -> tuple[str, str]:
    """The pin the app's catalog resolves and the one graph-admin's catalog resolves."""
    app = FileCatalog(audited.root, follow_migration=True).resolve("example_graph")
    admin = FileCatalog(audited.root).resolve("example_graph")
    listed = FileCatalog(audited.root, follow_migration=True).list_graphs()[0]
    assert listed == app
    return app.pin.version, admin.pin.version


def test_a_committed_migration_is_read_under_its_target_version(tmp_path: Path) -> None:
    audited, graph_id = scene(tmp_path)
    assert versions(audited) == ("1.0.0", "1.0.0")
    audited.migrate(graph_id, "1.1.0", pin=False)
    assert versions(audited) == ("1.1.0", "1.0.0")
    target = FileCatalog(audited.root, follow_migration=True).resolve(str(graph_id)).pin
    assert target.artifact_hash == compiled_model("inventory", "1.1.0").artifact_hash


def test_a_migration_that_is_pinned_changes_nothing(tmp_path: Path) -> None:
    audited, graph_id = scene(tmp_path)
    audited.migrate(graph_id, "1.1.0", remove_marker=False)  # pinned, marker left over
    assert versions(audited) == ("1.1.0", "1.1.0")
    audited.migrate(graph_id, "2.0.0")
    assert versions(audited) == ("2.0.0", "2.0.0")


def test_a_marker_without_its_tx_is_ignored(tmp_path: Path) -> None:
    audited, graph_id = scene(tmp_path)
    audited.migrate(graph_id, "1.1.0", pin=False)
    # the marker names another tx than the one at its seq (as when a write took the seq)
    marker = audited.maintenance.store(graph_id).migration_marker()
    assert marker is not None
    layout = audited.root / "graphs" / str(graph_id) / "data" / "migration.json"
    layout.write_bytes(
        layout.read_bytes().replace(
            str(marker.tx_id).encode(), b"0" * 8 + str(marker.tx_id).encode()[8:]
        )
    )
    assert versions(audited) == ("1.0.0", "1.0.0")


def test_an_unreadable_marker_hides_only_its_graph_from_a_tolerant_listing(tmp_path: Path) -> None:
    audited, graph_id = scene(tmp_path)
    other = audited.create("other_graph")
    (audited.root / "graphs" / str(graph_id) / "data" / "migration.json").write_bytes(b"{")
    catalog = FileCatalog(audited.root, follow_migration=True)
    with pytest.raises(GraphCliError) as caught:
        catalog.list_graphs()
    assert caught.value.code == "backend.corrupt"
    listed = catalog.list_graphs(skip_unreadable=True)
    assert [e.id for e in listed] == [other]
    assert [w.code for w in catalog.warnings] == ["graph.unreadable"]
