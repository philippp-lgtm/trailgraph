"""The writes of ``graph push``: their order.

One push publishes a release, a skill and all three catalogs. An interrupted push is
repeatable from any point only because the writes come in a fixed order (release, skill,
functions, actions, views), so the order is pinned here by the fault points the writes pass.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import FIXTURES, ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.maintenance_cli.commands.graph import app_inventory
from graph_cli_admin.services_admin import graphs, sources, sources_push
from graph_cli_admin.services_admin.context import AdminContext

INVENTORY = ONTOLOGIES / "inventory"
SKILL = FIXTURES / "skills" / "template" / "graph-example-graph"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"

WRITES = (
    CrashPoint.RELEASE_BEFORE_RENAME,
    CrashPoint.MANIFEST_BEFORE_RENAME,
    CrashPoint.SKILL_BEFORE_RENAME,
    CrashPoint.FUNCTIONS_BEFORE_RENAME,
    CrashPoint.ACTIONS_BEFORE_RENAME,
    CrashPoint.VIEWS_BEFORE_RENAME,
)


@dataclass
class Workspace:
    ctx: AdminContext
    faults: FaultSwitch
    source: Path

    def push(self) -> sources_push.Pushed:
        return sources_push.push(
            self.ctx,
            graph_ref="example_graph",
            source=self.source,
            reason=None,
            dry_run=False,
            inventory=app_inventory(),
        )


def pulled(tmp_path: Path, version: str) -> Workspace:
    """A graph at ``version`` (fixture ontology) and a workspace pulled from it."""
    faults = FaultSwitch()
    ctx = admin_context(tmp_path / "root", faults)
    graphs.create(
        ctx,
        name="example_graph",
        source=INVENTORY / version,
        module=None,
        title=None,
        language=None,
    )
    source = tmp_path / "work"
    sources.pull(ctx, graph_ref="example_graph", target=source, version=None, force=False)
    return Workspace(ctx, faults, source)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A graph at 1.0.0 and a workspace that pushes 1.1.0 with a skill and every catalog."""
    found = pulled(tmp_path, "1.0.0")
    shutil.copytree(INVENTORY / "1.1.0", found.source / "ontology" / "1.1.0")
    shutil.copytree(SKILL, found.source / "skill" / "graph-example-graph")
    for folder, fixture in (("functions", FUNCTIONS), ("actions", ACTIONS), ("views", VIEWS)):
        shutil.copytree(fixture, found.source / folder)
    return found


def test_a_push_writes_the_release_the_skill_and_the_catalogs_in_that_order(
    workspace: Workspace,
) -> None:
    workspace.push()
    assert [p for p in workspace.faults.seen if p in WRITES] == list(WRITES)
