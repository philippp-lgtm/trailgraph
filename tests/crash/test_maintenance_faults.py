"""Interrupted maintenance operations (contracts W.e, W.f, B5.6; plan M2, M4).

A fault before each effect's rename leaves what a kill -9 would leave. The
verifier then reports only ``intent_without_effect`` (info), and a retry
completes the operation.
"""

from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.services_admin import audit, graphs
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.release import release

INVENTORY = ONTOLOGIES / "inventory"
IWE = "integrity.audit.intent_without_effect"


@pytest.fixture
def faults() -> FaultSwitch:
    return FaultSwitch()


@pytest.fixture
def ctx(tmp_path: Path, faults: FaultSwitch) -> AdminContext:
    return admin_context(tmp_path / "root", faults)


def create(ctx: AdminContext) -> None:
    graphs.create(
        ctx,
        name="example_graph",
        source=INVENTORY / "1.0.0",
        module=None,
        title=None,
        language=None,
    )


def verify_codes(ctx: AdminContext) -> list[str]:
    verification = audit.verify(ctx)
    verification.raise_on_errors()
    return sorted(f.code for f in verification.findings)


def interrupted(faults: FaultSwitch, point: CrashPoint, action: object) -> None:
    faults.armed.add(point)
    with pytest.raises(GraphCliError) as caught:
        action()  # type: ignore[operator]
    assert caught.value.code == "backend.unavailable"
    faults.armed.clear()


def test_create_before_announce_leaves_nothing_visible(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    interrupted(faults, CrashPoint.CREATE_BEFORE_ANNOUNCE, lambda: create(ctx))
    assert ctx.catalog.list_graphs() == []
    assert verify_codes(ctx) == []
    create(ctx)
    assert [e.name for e in ctx.catalog.list_graphs()] == ["example_graph"]
    assert verify_codes(ctx) == []


def test_create_before_rename_leaves_an_announced_intent(
    ctx: AdminContext, faults: FaultSwitch
) -> None:
    interrupted(faults, CrashPoint.CREATE_BEFORE_RENAME, lambda: create(ctx))
    assert ctx.catalog.list_graphs() == []
    assert verify_codes(ctx) == [IWE]
    create(ctx)  # the name is free: the graph was never published
    assert verify_codes(ctx) == [IWE]


def test_release_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    create(ctx)
    run = lambda: release(ctx, graph_ref="example_graph", source=INVENTORY / "1.0.1", module=None)  # noqa: E731
    interrupted(faults, CrashPoint.RELEASE_BEFORE_RENAME, run)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.0"
    assert verify_codes(ctx) == [IWE]
    assert run().data["released"] is True
    # The retry installed the same artifact: the first intent's effect now exists.
    assert verify_codes(ctx) == []


def test_pin_before_rename(ctx: AdminContext, faults: FaultSwitch) -> None:
    create(ctx)
    run = lambda: release(ctx, graph_ref="example_graph", source=INVENTORY / "1.0.1", module=None)  # noqa: E731
    interrupted(faults, CrashPoint.MANIFEST_BEFORE_RENAME, run)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.0"
    assert verify_codes(ctx) == [IWE]
    outcome = run()
    assert (outcome.data["released"], outcome.data["pinned"]) == (False, True)
    assert ctx.catalog.resolve("example_graph").pin.version == "1.0.1"
    assert verify_codes(ctx) == [IWE]  # the first pin intent stays an orphan
