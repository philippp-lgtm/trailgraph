"""Interrupted releases with a data migration (migration plan M3, M5; contracts W.e).

A fault at each point of the release sequence (release installed, marker written, intent
appended, tx committed, manifest switched, marker removed) leaves what a kill -9 would leave.
The state is either the old one or the incomplete one; there, reads see the target version's
names, writes are refused, another release is refused, and the same release run again finishes
it. Writers queued behind the lock re-authorize under the new model.
"""

import dataclasses
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, init, write_documents
from support.admin_context import admin_context
from support.app import ENV, app
from support.fixtures import ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.actor import Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.migration_marker import marker_committed
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.services_admin import audit, graphs
from graph_cli_admin.services_admin.check import check
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.release import authorize, release

pytestmark = pytest.mark.slow

INVENTORY = ONTOLOGIES / "inventory"
IWE = "integrity.audit.intent_without_effect"
MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""
SEED: list[dict[str, Any]] = [
    {"op": "create", "type": "inv:Location", "local": "room", "attrs": {"name": "R", "code": "R1"}},
    {"op": "create", "type": "inv:Category", "local": "tools", "attrs": {"name": "Tools"}},
    {
        "op": "create",
        "type": "inv:Item",
        "local": "hammer",
        "attrs": {"name": "Hammer", "condition": "worn"},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
        ],
    },
]


class Arm(FaultSwitch):
    """``FaultSwitch`` that arms ``then`` once ``after`` has been seen: the intent of the
    migration is the first audit append after its marker is written."""

    def __init__(self, after: CrashPoint, then: CrashPoint) -> None:
        super().__init__()
        self.after, self.then = after, then

    def __call__(self, point: CrashPoint) -> None:
        if point is self.after:
            self.armed.add(self.then)
        super().__call__(point)


@dataclass(frozen=True)
class Row:
    """The durable state right after the fault."""

    marker: bool  # data/migration.json exists
    committed: bool  # the migration tx is in the log
    pinned: bool  # the manifest names 2.0.0


ROWS: dict[str, tuple[CrashPoint, Row]] = {
    "release": (CrashPoint.RELEASE_BEFORE_RENAME, Row(False, False, False)),
    "marker": (CrashPoint.MIGRATION_MARKER_BEFORE_RENAME, Row(False, False, False)),
    "intent": (CrashPoint.AUDIT_BEFORE_WRITE, Row(True, False, False)),
    "tx_before_write": (CrashPoint.TX_BEFORE_WRITE, Row(True, False, False)),
    "pin": (CrashPoint.MANIFEST_BEFORE_RENAME, Row(True, True, False)),
    "marker_removal": (CrashPoint.MIGRATION_MARKER_BEFORE_REMOVE, Row(True, True, True)),
}


class Scene:
    """A graph at 1.1.0 with one worn item, and the 2.0.0 directory that migrates it."""

    def __init__(self, run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> None:
        self.run_cli = run_cli
        self.root = init(run_cli, isolated_env)
        self.tmp_path = tmp_path
        self.source = tmp_path / "v2"
        shutil.copytree(INVENTORY / "2.0.0", self.source)
        (self.source / "migration.yaml").write_text(MIGRATION)
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
        write_documents(self.root, "example_graph", [{"ops": SEED}])
        self.graph_id = self.ctx.catalog.resolve("example_graph").id

    def use(self, faults: FaultSwitch) -> None:
        self.faults = faults
        self.ctx = admin_context(self.root, faults)

    def run(self, source: Path | None = None) -> Any:
        return release(
            self.ctx,
            graph_ref="example_graph",
            source=source or self.source,
            module=None,
            reason="example reason",
        )

    def interrupt(self, name: str) -> Row:
        point, row = ROWS[name]
        if name == "intent":
            faults: FaultSwitch = Arm(CrashPoint.MIGRATION_MARKER_BEFORE_RENAME, point)
        else:
            faults = FaultSwitch()
            faults.armed.add(point)
        self.use(faults)
        with pytest.raises(GraphCliError) as caught:
            self.run()
        assert caught.value.code in ("backend.unavailable", "audit.unavailable")
        self.use(FaultSwitch())
        return row

    # ---- what a reader of the data root sees ----

    def marker(self) -> Any:
        return self.ctx.maintenance.store(self.graph_id).migration_marker()

    def committed(self) -> bool:
        marker = self.marker()
        store = self.ctx.catalog.file_store(self.graph_id)
        return marker is not None and marker_committed(store.snapshot(), marker)

    def pin(self) -> str:
        return self.ctx.catalog.resolve("example_graph").pin.version

    def app_pin(self) -> str:
        return FileCatalog(self.root, follow_migration=True).resolve("example_graph").pin.version

    def errors(self) -> list[str]:
        verification = audit.verify(self.ctx)
        verification.raise_on_errors()
        return sorted(f.code for f in verification.findings)

    def items(self) -> Outcome:
        return app(self.run_cli, "find", "--type", "inv:Item", env=ENV)

    def create_location(self, name: str, code: str) -> Outcome:
        return app(
            self.run_cli,
            "create",
            "inv:Location",
            "--set-json",
            f'name="{name}"',
            "--set-json",
            f'code="{code}"',
            env=ENV,
        )


@pytest.fixture
def scene(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Scene:
    return Scene(run_cli, isolated_env, tmp_path)


@pytest.mark.parametrize("name", list(ROWS))
def test_a_fault_leaves_the_old_state_or_the_incomplete_one(scene: Scene, name: str) -> None:
    row = scene.interrupt(name)
    assert scene.marker() is not None if row.marker else scene.marker() is None
    assert scene.committed() is row.committed
    assert scene.pin() == ("2.0.0" if row.pinned else "1.1.0")
    # the app catalog reads the data under the names it has
    assert scene.app_pin() == ("2.0.0" if row.committed else "1.1.0")
    found = scene.items()
    assert found.code == 0, found.envelope
    attrs = found.data["items"][0]["attrs"]
    assert ("label" in attrs) is row.committed and ("name" in attrs) is not row.committed
    # the crash case warns; so does a finished pin with a marker left over (the marker is
    # all that is left, and the next release clears it)
    assert ("graph.migration_incomplete" in found.warning_codes) is row.committed
    scene.errors()  # no error finding, whatever the state
    # writes are refused while the migration is committed and the marker is there
    written = scene.create_location("Annex", "R2")
    if row.committed:
        assert written.error["code"] == "write.migration_incomplete"
        assert written.error["category"] == "unavailable" and written.error["retryable"]
        assert written.code != 0
    else:
        assert written.code == 0, written.envelope
    # the same release finishes it
    outcome = scene.run()
    assert outcome.data["pinned"] is not row.pinned
    assert scene.pin() == "2.0.0" and scene.marker() is None
    assert scene.app_pin() == "2.0.0"
    after = scene.items().data["items"]
    assert [i["attrs"]["label"] for i in after] == ["Hammer"]
    assert after[0]["attrs"]["condition"] == "good"
    assert after[0]["rev"] == 2  # migrated once, whichever way it ran
    scene.errors()
    assert scene.create_location("Annex", "R3").code == 0  # writes work again


def test_a_fault_after_the_commit_is_deferred_not_fatal(scene: Scene) -> None:
    """Past the commit point the derived files are rebuilt lazily: the release goes on and
    the next writer recovers (``storage.materialization_deferred``), as for any write."""
    scene.use(FaultSwitch())
    scene.faults.armed.add(CrashPoint.HEAD_BEFORE_RENAME)  # type: ignore[attr-defined]
    outcome = scene.run()
    assert outcome.data["pinned"] is True
    assert scene.pin() == "2.0.0" and scene.marker() is None
    scene.use(FaultSwitch())
    assert scene.create_location("Annex", "R2").code == 0
    assert [i["attrs"]["label"] for i in scene.items().data["items"]] == ["Hammer"]
    scene.errors()


def test_the_rerun_of_a_committed_migration_writes_no_second_tx(scene: Scene) -> None:
    scene.interrupt("pin")
    head = scene.ctx.catalog.file_store(scene.graph_id).head().seq
    outcome = scene.run()
    block = outcome.data["migration"]
    assert block["completing"] is True and "tx_seq" not in block
    assert outcome.data["released"] is False and outcome.data["pinned"] is True
    assert scene.ctx.catalog.file_store(scene.graph_id).head().seq == head


def test_a_stale_marker_is_ignored_even_when_a_write_takes_its_seq(scene: Scene) -> None:
    """The marker names the seq and the tx id: a write that took the seq of the migration
    that never committed does not make the marker look committed."""
    scene.interrupt("tx_before_write")
    marker = scene.marker()
    assert scene.create_location("Annex", "R2").code == 0
    store = scene.ctx.catalog.file_store(scene.graph_id)
    tx = store.snapshot().tx(marker.seq)
    assert tx is not None and tx.tx_id != marker.tx_id
    assert scene.committed() is False
    assert scene.items().warning_codes == []
    outcome = scene.run()
    assert outcome.data["migration"]["tx_seq"] == marker.seq + 1
    assert scene.pin() == "2.0.0" and scene.marker() is None
    scene.errors()


def test_another_release_is_refused_in_the_incomplete_state(scene: Scene) -> None:
    scene.interrupt("pin")
    other = scene.tmp_path / "v2_1"
    shutil.copytree(INVENTORY / "2.1.0", other)
    with pytest.raises(GraphCliError) as caught:
        scene.run(other)
    assert caught.value.code == "admin.migration_incomplete"
    assert caught.value.details["to"] == "2.0.0"
    # nor does the same version with another file complete it
    changed = scene.tmp_path / "changed"
    shutil.copytree(scene.source, changed)
    (changed / "migration.yaml").write_text(MIGRATION.replace("worn: good", "worn: new"))
    with pytest.raises(GraphCliError) as caught:
        scene.run(changed)
    assert caught.value.code == "admin.release_exists"
    assert scene.marker() is not None and scene.pin() == "1.1.0"
    # a minor release of the old version is refused too: it would pin over migrated data
    patch = scene.tmp_path / "patch"
    patch.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (patch / "inventory.yaml").write_text(text.replace("version: 1.1.0", "version: 1.1.1", 1))
    with pytest.raises(GraphCliError) as caught:
        scene.run(patch)
    assert caught.value.code == "admin.migration_incomplete"


def test_a_stale_marker_is_cleared_by_a_release_without_migration(scene: Scene) -> None:
    scene.interrupt("tx_before_write")
    assert scene.marker() is not None
    patch = scene.tmp_path / "patch"
    patch.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (patch / "inventory.yaml").write_text(text.replace("version: 1.1.0", "version: 1.1.1", 1))
    outcome = scene.run(patch)
    assert outcome.data["pinned"] is True and scene.marker() is None
    assert scene.pin() == "1.1.1"


def test_a_finished_pin_with_a_leftover_marker_is_cleared_by_rerunning(scene: Scene) -> None:
    scene.interrupt("marker_removal")
    assert scene.pin() == "2.0.0" and scene.marker() is not None
    outcome = scene.run()
    assert (outcome.data["released"], outcome.data["pinned"]) == (False, False)
    assert scene.marker() is None
    scene.errors()


def test_writers_behind_the_lock_re_authorize_under_the_new_model(scene: Scene) -> None:
    """A write that waits for the lock of a migrating release finds the new pin when it gets
    the lock and is judged by the new model: it names ``label``, a term only 2.0.0 has."""
    reached, proceed = threading.Event(), threading.Event()

    class Gate(FaultSwitch):
        def __call__(self, point: CrashPoint) -> None:
            if point is CrashPoint.MIGRATION_MARKER_BEFORE_RENAME:
                reached.set()
                assert proceed.wait(20)
            super().__call__(point)

    scene.use(Gate())
    failures: list[BaseException] = []

    def migrate() -> None:
        try:
            scene.run()
        except BaseException as exc:
            failures.append(exc)

    def write() -> None:
        item = {
            "op": "create",
            "type": "inv:Item",
            "attrs": {"label": "Saw", "condition": "good"},
            "edges": [
                {"rel": "inv:located_in", "target": "LOC-0001"},
                {"rel": "inv:in_category", "target": "CAT-0001"},
            ],
        }
        try:
            write_documents(scene.root, "example_graph", [{"ops": [item]}], seed=14)
        except BaseException as exc:
            failures.append(exc)

    releasing = threading.Thread(target=migrate)
    releasing.start()
    assert reached.wait(20)
    writing = threading.Thread(target=write)
    writing.start()
    time.sleep(1)  # the writer has passed its checks and waits for the lock the release holds
    proceed.set()
    releasing.join(30)
    writing.join(30)
    assert not failures, failures
    assert scene.pin() == "2.0.0"
    found = scene.items().data["items"]
    assert sorted(i["attrs"]["label"] for i in found) == ["Hammer", "Saw"]
    scene.errors()


def test_a_project_admin_may_not_migrate(scene: Scene) -> None:
    """``maint.migrate`` stays out of the project admin's maintain level (plan M4): a release
    that migrates is refused before the lock with the ceiling."""
    policy = compile_policy(
        b"schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        b'  - {principal: {name: tester}, graphs: "*", level: write}\n'
        b"scopes:\n  example_project: {example_graph: maintain}\n"
        b'no_project: {"*": read}\n',
        file="i.yaml",
        graphs={"example_graph": GraphId(scene.graph_id)},
    )

    class Source:
        def current(self, graph: Any) -> Any:
            return policy_set(policy.document)

        def refresh(self) -> None:
            return None

    actor = dataclasses.replace(
        scene.ctx.actor,
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=Facet(Assurance.VERIFIED, id="example_project", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.MAINT),
    )
    ctx: AdminContext = dataclasses.replace(scene.ctx, actor=actor, policy=Source())  # type: ignore[arg-type]
    entry = ctx.catalog.resolve("example_graph")
    meta = RequestMeta.of("ontology release", {})
    plain = authorize(ctx, entry, meta)  # a release without a migration is the project's own
    assert plain.migrate == ()
    with pytest.raises(GraphCliError) as caught:
        authorize(ctx, entry, meta, migrate=True)
    assert caught.value.code == "access.ceiling"


def test_the_events_of_a_migrating_release(scene: Scene) -> None:
    """The sequence the audit verifier expects (step 4's scene): release with the file's hash,
    the migration intent under the tx's event id, then the pin with the prefixes."""
    scene.run()
    graph_id = str(scene.graph_id)
    from graph_cli.audit.verify import verify_stream
    from graph_cli.kernel.ports.audit import graph_stream

    stream = verify_stream(
        scene.ctx.audit.read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert stream.findings == ()
    sequence = ("maint.ontology_release", "maint.migrate", "maint.ontology_pin")
    events = {e["actions"][0]: e for e in stream.events if e["actions"][0] in sequence}
    assert list(events) == ["maint.ontology_release", "maint.migrate", "maint.ontology_pin"]
    release_change = events["maint.ontology_release"]["change"]
    assert set(release_change) == {"release", "migration_hash"}
    assert set(release_change["release"]) == {"version", "artifact_hash", "release_id"}
    pin_change = events["maint.ontology_pin"]["change"]
    assert set(pin_change) == {"pin"}  # nothing moved: no sibling key
    migrate = events["maint.migrate"]
    assert migrate["reason"] == "example reason"
    record = migrate["change"]["migration"]
    assert record["counts"] == {"entities": 1, "edges": 0}
    tx = scene.ctx.catalog.file_store(scene.graph_id).snapshot().tx(record["tx_seq"])
    assert tx is not None and str(tx.event_id) == migrate["event_id"]
    assert tx.reason == "example reason" and tx.actor.principal.name is not None
    assert tx.changeset["schema"] == "graph_cli.migration/1"
    assert tx.changeset["migration"]["migration_hash"] == release_change["migration_hash"]


def test_a_reader_during_the_release_sees_the_new_names_and_no_warning(scene: Scene) -> None:
    """Between the commit and the pin switch the release holds the lock: reads already use the
    target version, without the crash warning, and writes are refused at once."""
    reached, proceed = threading.Event(), threading.Event()

    class Gate(FaultSwitch):
        def __call__(self, point: CrashPoint) -> None:
            if point is CrashPoint.MANIFEST_BEFORE_RENAME:
                reached.set()
                assert proceed.wait(20)
            super().__call__(point)

    scene.use(Gate())
    failures: list[BaseException] = []

    def migrate() -> None:
        try:
            scene.run()
        except BaseException as exc:
            failures.append(exc)

    releasing = threading.Thread(target=migrate)
    releasing.start()
    try:
        assert reached.wait(20)
        found = scene.items()
        assert found.code == 0, found.envelope
        assert "label" in found.data["items"][0]["attrs"]
        assert found.warning_codes == []  # a writer holds the lock: not the crash case
        written = scene.create_location("Annex", "R2")
        assert written.error["code"] == "write.migration_incomplete"
    finally:
        proceed.set()
        releasing.join(30)
    assert not failures, failures
    assert scene.pin() == "2.0.0"


def test_graph_check_in_the_incomplete_state_uses_the_target_version(scene: Scene) -> None:
    scene.interrupt("pin")
    outcome = check(scene.ctx, graph_ref="example_graph", source=None, module=None)
    assert outcome.data["counts"]["error"] == 0
    assert outcome.data["ontology"]["version"] == "2.0.0"
    assert [w.code for w in outcome.warnings] == ["graph.migration_incomplete"]
    again = check(scene.ctx, graph_ref="example_graph", source=scene.source, module=None)
    assert again.data["counts"]["error"] == 0 and "migration" not in again.data
    scene.run()
    done = check(scene.ctx, graph_ref="example_graph", source=None, module=None)
    assert done.warnings == () and done.data["ontology"]["version"] == "2.0.0"
