"""An interrupted release that publishes the catalogs again (migration plan M3, M8).

The republish sits between the migration tx and the pin switch. A fault at its rename leaves
the old catalog and the old pin (with the migration committed and its marker, when data was
migrated, which is the incomplete state); the same release run again publishes the catalog
and pins.
"""

from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.admin_context import admin_context
from support.moved_supplier import BARE_FUNCTION, INVENTORY, moved_source, write_folder
from support.stores import FaultSwitch

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.services_admin import audit, functions, graphs
from graph_cli_admin.services_admin.release import release

pytestmark = pytest.mark.slow

SUPPLIER = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Example supplier"}}


class Scene:
    def __init__(
        self, run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, *, data: bool
    ) -> None:
        self.root = init(run_cli, isolated_env)
        self.faults = FaultSwitch()
        self.ctx = admin_context(self.root, self.faults)
        graphs.create(
            self.ctx,
            name="example_graph",
            source=INVENTORY / "1.1.0",
            module=None,
            title=None,
            language=None,
        )
        if data:
            write_documents(self.root, "example_graph", [{"ops": [SUPPLIER]}])
        self.graph_id = self.ctx.catalog.resolve("example_graph").id
        folder = write_folder(tmp_path, "functions", {"suppliers_by_name": BARE_FUNCTION})
        functions.publish(
            self.ctx, graph_ref="example_graph", source=folder, reason=None, dry_run=False
        )
        self.source = moved_source(tmp_path, migration=data)
        self.before = self.slot()

    def use(self, faults: FaultSwitch) -> None:
        self.faults = faults
        self.ctx = admin_context(self.root, faults)

    def slot(self) -> Any:
        return self.ctx.catalog.read_functions(self.graph_id)

    def pin(self) -> str:
        return self.ctx.catalog.resolve("example_graph").pin.version

    def marker(self) -> Any:
        return self.ctx.maintenance.store(self.graph_id).migration_marker()

    def run(self) -> Any:
        return release(
            self.ctx,
            graph_ref="example_graph",
            source=self.source,
            module="inventory",
            reason="example reason",
        )

    def interrupt(self) -> None:
        faults = FaultSwitch()
        faults.armed.add(CrashPoint.FUNCTIONS_BEFORE_RENAME)
        self.use(faults)
        with pytest.raises(GraphCliError) as caught:
            self.run()
        assert caught.value.code in ("backend.unavailable", "audit.unavailable")
        self.use(FaultSwitch())

    def errors(self) -> list[str]:
        verification = audit.verify(self.ctx)
        verification.raise_on_errors()
        return sorted(f.code for f in verification.findings)


@pytest.mark.parametrize("data", [True, False], ids=["with_data", "without_data"])
def test_a_fault_in_the_republish_is_finished_by_running_the_release_again(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, data: bool
) -> None:
    scene = Scene(run_cli, isolated_env, tmp_path, data=data)
    scene.interrupt()
    # the catalog and the pin are as they were; with data the migration is committed
    assert scene.pin() == "1.1.0"
    assert scene.slot().compiled_hash == scene.before.compiled_hash
    assert (scene.marker() is not None) is data
    scene.errors()  # an interrupted publish is no error finding
    outcome = scene.run()
    assert outcome.data["pinned"] is True
    assert outcome.data["republished"]["functions"]["names"] == ["suppliers_by_name"]
    assert scene.pin() == "2.0.0" and scene.marker() is None
    after = scene.slot()
    assert after.hash == scene.before.hash and after.compiled_hash != scene.before.compiled_hash
    assert after.published_against.version == "2.0.0"
    scene.errors()
    # nothing is left to do
    again = scene.run()
    assert (again.data["released"], again.data["pinned"]) == (False, False)
    assert "republished" not in again.data


def test_a_fault_after_the_republish_leaves_nothing_to_republish(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """A fault at the pin switch, after the catalog was published: the rerun pins and finds the
    catalog already compiled for the new model."""
    scene = Scene(run_cli, isolated_env, tmp_path, data=True)
    faults = FaultSwitch()
    faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    scene.use(faults)
    with pytest.raises(GraphCliError):
        scene.run()
    scene.use(FaultSwitch())
    assert scene.pin() == "1.1.0" and scene.marker() is not None
    assert scene.slot().published_against.version == "2.0.0"
    outcome = scene.run()
    assert outcome.data["pinned"] is True and "republished" not in outcome.data
    assert scene.pin() == "2.0.0" and scene.marker() is None
    scene.errors()
