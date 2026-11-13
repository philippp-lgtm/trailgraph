"""Write and read timings of FileStore on a generated graph (not run in CI).

Builds a graph of the inventory toy ontology (default: 300 txs with 15
creates each plus random edits, about 4,500 entities) in a temporary
directory, then times what one `graph` process pays: a fresh `FileStore`
per operation, so no replay cache carries over.

    uv run python scripts/bench_store.py [--txs N] [--creates K] [--runs R]

``--migration`` runs the migration scenario instead: a graph of about 6,300 entities and
16,000 edges is loaded in a few large txs and in many small ones, then every class and
relation moves to a new module with one ``session.migrate`` (real fsyncs). Loading and
migrating run in separate processes, so the peak memory reported is the migration's own.

    uv run python scripts/bench_store.py --migration [--entities N] [--shapes few,many]
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.clock import StepClock
from support.drafts import intent
from support.ids import SeededIdProvider
from support.migrations import allow_migrate
from support.modules import compile_modules, header
from support.stores import ALL_STATUSES, RecordingSyncer, StoreEnv, snapshot_state
from support.txbuilder import TxBuilder, TxDraft, actor

from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TermRef, TxId, parse_id
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import migration_hash, parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.migration import build_migration_tx, migration_record
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.durable import OsSyncer
from graph_cli.storage.files.layout import init_graph_dir
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli.temporal_knowledge.migrate import MigrationStamp, plan_migration


def _median_ms(run: Callable[[], object], runs: int) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        run()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--txs", type=int, default=300)
    parser.add_argument("--creates", type=int, default=15)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--migration", action="store_true", help="run the migration scenario")
    parser.add_argument(
        "--entities", type=int, default=6300, help="entities of the migration graph"
    )
    parser.add_argument(
        "--shapes",
        default="few,many,many_due",
        help="few, many and many_due: how the graph is loaded",
    )
    parser.add_argument("--child", choices=("load", "run"), help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--shape", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child is not None:
        _child(args)
        return
    if args.migration:
        _migration(args)
        return
    with tempfile.TemporaryDirectory() as tmp:
        env = StoreEnv("files", "inventory", Path(tmp))

        def bulk(draft: TxDraft) -> None:
            for _ in range(args.creates):
                draft.create()
            env.builder.random_tx(draft)

        for _ in range(args.txs):
            env.write(bulk)
        assert isinstance(env.store, FileStore)
        root, graph = env.store.layout.root, env.graph_id
        log_bytes = sum(p.stat().st_size for p in env.store.layout.txlog.iterdir())

        def fresh(**options: object) -> FileStore:
            return FileStore(root, graph, syncer=env.syncer, **options)  # type: ignore[arg-type]

        def write(**options: object) -> Callable[[], object]:
            def run() -> object:
                env.store = fresh(**options)
                return env.write(lambda draft: draft.create())

            return run

        def read() -> object:
            snapshot = fresh().snapshot()
            kind = sorted(env.builder.model.classes)[0]
            return snapshot.query(Query(types=(kind,), record_status=ALL_STATUSES, limit=50))

        rows = [("read (snapshot + find 50)", _median_ms(read, args.runs))]
        rows.append(("write", _median_ms(write(), args.runs)))
        rows.append(("write, full replay", _median_ms(write(full_check_every=1), args.runs)))
        everything = Query(record_status=ALL_STATUSES, limit=10**9)
        entities = len(fresh().snapshot().query(everything).items)
        print(f"txs {env.store.head().seq}, log {log_bytes / 1e6:.1f} MB, entities {entities}")
        for name, ms in rows:
            print(f"{name:28} {ms:8.1f} ms (median of {args.runs})")


# ---- the migration scenario -----------------------------------------------------------

# Entities per class in creation order; edges only point at entities created before.
MIX = (
    ("Topic", 0.095),
    ("Reference", 0.071),
    ("Task", 0.349),
    ("Solution", 0.254),
    ("KnowledgeItem", 0.231),
)
# Edges of a new entity: (relation, class of the target, how many).
EDGES = {
    "Task": (("ex:about", "Topic", 2), ("ex:cites", "Reference", 1)),
    "Solution": (("ex:about", "Topic", 1), ("ex:solves", "Task", 1)),
    "KnowledgeItem": (("ex:about", "Topic", 2), ("ex:cites", "Reference", 2)),
    "Topic": (("ex:broader", "Topic", 1),),
}
FEW_TXS = 4  # the "few large txs" shape: the graph in this many txs
SMALL_TX = 10  # the "many small txs" shape: entities per tx
CONFIRM_SAMPLE = 200
TARGET_SECONDS = 30.0
TARGET_MIB = 750.0
STAMP_TX = "0192f000-0000-7000-8000-0000000000e1"
STAMP_EVENT = "0192f000-0000-7000-8000-0000000000e2"

ATTRIBUTES = """    attributes:
      title: {summary: Title., type: string, required: true, role: label}
      body: {summary: Body., type: text}
"""
OLD_BODY = "classes:\n" + "".join(
    f"  {name}:\n    summary: {name}.\n    display_prefix: {name[:4].upper()}\n" + ATTRIBUTES
    for name, _ in MIX
)
OLD_BODY += """relations:
  about: {summary: About., domain: [Task, Solution, KnowledgeItem], range: Topic}
  cites:
    summary: Cites.
    domain: [Task, KnowledgeItem]
    range: Reference
    qualifiers:
      page: {summary: Page., type: string}
  solves: {summary: Solves., domain: Solution, range: Task}
  broader: {summary: Broader., domain: Topic, range: Topic}
"""
TASK_AREA = """classes:
{task}{solution}relations:
  solves: {{summary: Solves., domain: Solution, range: Task, renamed_from: ex:solves}}
"""
KNOWLEDGE_AREA = """classes:
{knowledge}{reference}relations:
  cites:
    summary: Cites.
    domain: [ta:Task, KnowledgeItem]
    range: Reference
    renamed_from: ex:cites
    qualifiers:
      page: {{summary: Page., type: string}}
"""
TOPIC_AREA = """classes:
{topic}relations:
  about:
    summary: About.
    domain: [ta:Task, ta:Solution, ka:KnowledgeItem]
    range: Topic
    renamed_from: ex:about
  broader: {{summary: Broader., domain: Topic, range: Topic, renamed_from: ex:broader}}
"""


def _moved(name: str) -> str:
    return (
        f"  {name}:\n    summary: {name}.\n    display_prefix: {name[:4].upper()}\n"
        f"    renamed_from: ex:{name}\n" + ATTRIBUTES
    )


def _models() -> tuple[CompiledModel, CompiledModel]:
    """One module of five classes and four relations, and the model that moves every class
    and relation to one of three area modules by CURIE ``renamed_from``."""
    old = compile_modules("example", {"example": header("example", "ex", "1.0.0") + OLD_BODY})
    areas = {
        "task_area": header("task_area", "ta", "1.0.0")
        + TASK_AREA.format(task=_moved("Task"), solution=_moved("Solution")),
        "knowledge_area": header("knowledge_area", "ka", "1.0.0", ("task_area",))
        + KNOWLEDGE_AREA.format(knowledge=_moved("KnowledgeItem"), reference=_moved("Reference")),
        "topic_area": header("topic_area", "tp", "1.0.0", ("task_area", "knowledge_area"))
        + TOPIC_AREA.format(topic=_moved("Topic")),
    }
    main = header("example", "ex", "2.0.0", tuple(areas))
    return old, compile_modules("example", {"example": main, **areas})


def _mib() -> float:
    """Peak resident set size of this process so far."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _counts(entities: int) -> list[tuple[str, int]]:
    counts = [(name, round(entities * share)) for name, share in MIX]
    return [*counts[:-1], (counts[-1][0], entities - sum(n for _, n in counts[:-1]))]


def _load(root: Path, shape: str, entities: int) -> dict[str, Any]:
    """Create the graph under ``root`` with a no-op syncer (loading is not measured)."""
    old, _ = _models()
    clock = StepClock()
    ids = SeededIdProvider(clock, 7)
    graph = GraphId(ids.new_id())
    syncer = RecordingSyncer()
    graph_dir = root / "graphs" / str(graph)
    init_graph_dir(graph_dir, syncer)
    store = FileStore(graph_dir, graph, syncer=syncer)
    builder = TxBuilder(old, graph_id=graph, ids=ids, clock=clock, seed=7)
    rng = random.Random(7)
    stream = [name for name, count in _counts(entities) for _ in range(count)]
    per_tx = -(-len(stream) // FEW_TXS) if shape == "few" else SMALL_TX  # many, many_due
    made: dict[str, list[EntityId]] = {name: [] for name, _ in MIX}
    edges = txs = 0
    start = time.perf_counter()
    with store.session(timeout_s=60) as session:
        for first in range(0, len(stream), per_tx):
            state = snapshot_state(session.snapshot())
            draft = builder.begin(state, session.head(), session.alias_counters())
            for cls in stream[first : first + per_tx]:
                number = len(made[cls]) + 1
                attrs = {"title": f"{cls} {number}", "body": f"{cls} body {number} " * 6}
                entity = draft.create(f"ex:{cls}", attrs=attrs)
                edges += _link(draft, rng, made, cls, entity)
                made[cls].append(entity)
            tx = draft.build()
            session.commit(tx, session.append_audit(intent(tx)))
            txs += 1
    if shape == "many":
        # With 200 txs or more since the last full check the next writer replays the log
        # and rebuilds the index (``many_due`` keeps that state). One more write does it,
        # so the migration meets a usable index, as it does shortly after a full check.
        with store.session(timeout_s=60) as session:
            draft = builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            draft.update(made["Task"][0])
            tx = draft.build()
            session.commit(tx, session.append_audit(intent(tx)))
            txs += 1
    log_bytes = sum(p.stat().st_size for p in store.layout.txlog.iterdir())
    return {
        "entities": len(stream),
        "edges": edges,
        "txs": txs,
        "log_mb": log_bytes / 1e6,
        "load_s": time.perf_counter() - start,
    }


def _link(
    draft: TxDraft, rng: random.Random, made: dict[str, list[EntityId]], cls: str, entity: EntityId
) -> int:
    """The outgoing edges of a new entity, to entities created before it."""
    count = 0
    for rel, target_class, number in EDGES.get(cls, ()):
        pool = made[target_class]
        for _ in range(number if pool else 0):
            qualifiers = {"page": str(rng.randint(1, 400))} if rel == "ex:cites" else None
            draft.link(entity, rel, rng.choice(pool), qualifiers=qualifiers)
            count += 1
    return count


class _PointClock:
    """Records when the commit path first reaches each fault point."""

    def __init__(self) -> None:
        self.at: dict[CrashPoint, float] = {}

    def __call__(self, point: CrashPoint) -> None:
        self.at.setdefault(point, time.perf_counter())


def _run(root: Path) -> dict[str, Any]:
    """Plan and commit the migration with full durability and time every phase, then time
    the readers."""
    old, new = _models()
    (graph_dir,) = (root / "graphs").iterdir()
    graph = GraphId(parse_id(graph_dir.name))
    parsed = parse_migration(
        "format: graph_cli.migration/1\nfrom: 1.0.0\nsummary: Move to areas.\nsteps: []\n"
    )
    assert parsed.doc is not None
    migration = parsed.doc
    # the state before the migration, to put back for the crash-shaped reader
    saved = Path(tempfile.mkdtemp(prefix="bench_saved_", dir=root))
    shutil.copy(graph_dir / "data" / "head.json", saved / "head.json")
    shutil.copytree(graph_dir / "data" / "index", saved / "index")
    points = _PointClock()
    maintenance = FileStoreMaintenance(root, syncer=OsSyncer("full"), faults=points)
    out: dict[str, Any] = {"confirm": _confirm(graph_dir, graph), "rss_start_mib": _mib()}
    clock = time.perf_counter()

    def lap() -> float:
        nonlocal clock
        now = time.perf_counter()
        elapsed, clock = now - clock, now
        return elapsed

    with maintenance.maintenance_session(graph, timeout_s=60) as session:
        snapshot = session.snapshot()
        head = session.head()
        assert head.recorded_at is not None
        out["session_open_s"], out["head_seq"] = lap(), head.seq
        renames = derive_renames(old, new)
        out["derive_renames_s"] = lap()
        stamp = MigrationStamp(
            TxId(parse_id(STAMP_TX)), head.seq + 1, head.recorded_at + timedelta(seconds=1), "2.0.0"
        )
        plan = plan_migration(old, new, renames, migration, snapshot, stamp=stamp)
        out["plan_s"], out["migrated"] = lap(), len(plan.revisions)
        findings = check_graph(new, plan.overlay(snapshot))
        out["check_graph_s"], out["findings"] = lap(), len(findings)
        assert not [f for f in findings if f.severity is Severity.ERROR], findings[:3]
        tx = build_migration_tx(
            plan,
            new,
            graph_id=graph,
            tx_id=stamp.tx_id,
            event_id=EventId(parse_id(STAMP_EVENT)),
            seq=stamp.tx_seq,
            recorded_at=stamp.recorded_at,
            actor=actor("maint:1"),
            decisions=allow_migrate(graph),
            ontology=OntologyPin(new.bundle_id, new.bundle_version, new.artifact_hash),
            reason="benchmark",
            request=RequestMeta("ontology release", hash_value({"version": "2.0.0"})),
            record=migration_record(
                from_version="1.0.0", to_version="2.0.0", renames=renames, doc=migration
            ),
        )
        out["build_tx_s"] = lap()
        marker = MigrationMarker(
            tx.seq, tx.tx_id, "1.0.0", "2.0.0", new.artifact_hash, migration_hash(migration)
        )
        session.write_migration_marker(marker)
        out["marker_write_s"] = lap()
        receipt = session.append_audit(intent(tx))
        out["intent_s"] = lap()
        started = time.perf_counter()
        session.migrate(tx, receipt)
        finished = time.perf_counter()
        clock = finished
        out.update(_commit_phases(points, started, finished))
        session.remove_migration_marker()
        out["marker_remove_s"] = lap()
    out["rss_peak_mib"] = _mib()
    parts = ("session_open", "derive_renames", "plan", "check_graph", "build_tx", "marker_write")
    out["total_s"] = sum(out[f"{k}_s"] for k in parts) + sum(
        out[f"{k}_s"] for k in ("intent", "commit", "marker_remove")
    )
    log = graph_dir / "data" / "txlog"
    out["tx_line_mb"] = max(p.stat().st_size for p in log.iterdir()) / 1e6
    out.update(_readers(graph, graph_dir, saved, tx.revisions[0].id))
    return out


def _commit_phases(points: _PointClock, started: float, finished: float) -> dict[str, float]:
    at, p = points.at, CrashPoint
    return {
        "commit_s": finished - started,
        "commit_checks_s": at[p.TX_BEFORE_WRITE] - started,
        "commit_append_s": at[p.TX_AFTER_SYNC] - at[p.TX_BEFORE_WRITE],
        "commit_files_s": at[p.MATERIALIZE_BEFORE_DIR_SYNC] - at[p.TX_AFTER_SYNC],
        "commit_dirs_s": at[p.HEAD_BEFORE_RENAME] - at[p.MATERIALIZE_BEFORE_DIR_SYNC],
        "commit_head_s": at[p.HEAD_AFTER_RENAME] - at[p.HEAD_BEFORE_RENAME],
        "commit_index_s": at[p.INDEX_AFTER_RENAME] - at[p.HEAD_AFTER_RENAME],
        "commit_rest_s": finished - at[p.INDEX_AFTER_RENAME],
    }


def _readers(graph: GraphId, graph_dir: Path, saved: Path, entity: EntityId) -> dict[str, float]:
    """A fresh reader's first open and a read, after the migration and, as after a crash
    between the commit and the checkpoint, with the old head and index (the migration tx is
    then the tail of the log)."""

    def first_open() -> tuple[float, float]:
        start = time.perf_counter()
        snapshot = FileStore(graph_dir, graph).snapshot()
        opened = time.perf_counter()
        snapshot.get(entity)
        snapshot.query(Query(types=(TermRef("tp:Topic"),), record_status=ALL_STATUSES, limit=50))
        return opened - start, time.perf_counter() - opened

    found: dict[str, float] = {}
    found["reader_open_s"], found["reader_read_s"] = first_open()
    shutil.copy(saved / "head.json", graph_dir / "data" / "head.json")
    shutil.rmtree(graph_dir / "data" / "index")
    shutil.copytree(saved / "index", graph_dir / "data" / "index")
    found["reader_tail_open_s"], found["reader_tail_read_s"] = first_open()
    return found


def _confirm(graph_dir: Path, graph: GraphId) -> dict[str, float]:
    """``confirm_all`` of every entity against one tx read per entity (what ``confirm`` did
    before), on the state a migration starts from. The old way is timed on a sample of
    ``CONFIRM_SAMPLE`` entities and extrapolated: on a few large txs it takes minutes."""
    store = FileStore(graph_dir, graph)
    index, _ = store.writer_index(store.read_state())
    if index is None:
        return {}
    view = index.view
    entities = [r.id for r in view.current_revisions()]
    start = time.perf_counter()
    view.confirm_all(entities)
    grouped = time.perf_counter() - start
    sample = entities[:: max(1, len(entities) // CONFIRM_SAMPLE)]
    start = time.perf_counter()
    for entity in sample:
        revision = view.current(entity)
        if revision is not None:
            view._reader.tx(revision.tx_seq, view._txlog)
    per_entity = (time.perf_counter() - start) * len(entities) / len(sample)
    index.reader.close()
    return {"grouped_s": grouped, "per_entity_s": per_entity, "sample": len(sample)}


def _child(args: argparse.Namespace) -> None:
    if args.child == "load":
        result = _load(args.root, args.shape, args.entities)
    else:
        result = _run(args.root)
    print(json.dumps(result))


def _spawn(*flags: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *flags],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        sys.exit(f"child failed ({done.returncode}):\n{done.stderr[-4000:]}")
    result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def _migration(args: argparse.Namespace) -> None:
    labels = {
        "few": "a few large txs",
        "many": "many small txs, index fresh",
        "many_due": "many small txs, a full check of the log is due",
    }
    for shape in args.shapes.split(","):
        with tempfile.TemporaryDirectory(prefix="bench_migration_") as tmp:
            flags = ("--root", tmp, "--shape", shape, "--entities", str(args.entities))
            loaded = _spawn("--child", "load", *flags)
            ran = _spawn("--child", "run", *flags)
        print(
            f"\n== loaded in {labels[shape]}: {loaded['txs']} txs, {loaded['entities']} entities,"
            f" {loaded['edges']} edges, log {loaded['log_mb']:.1f} MB"
            f" (load {loaded['load_s']:.1f} s)"
        )
        _report(ran)


def _report(r: dict[str, Any]) -> None:
    rows = [
        ("session open (lock, recovery, index check)", "session_open_s"),
        ("derive_renames", "derive_renames_s"),
        ("plan_migration", "plan_s"),
        ("check_graph(new, overlay)", "check_graph_s"),
        ("build_migration_tx", "build_tx_s"),
        ("marker write", "marker_write_s"),
        ("audit intent append", "intent_s"),
        ("migrate (commit), total", "commit_s"),
        ("  checks + confirm + seal", "commit_checks_s"),
        ("  append + fsync (commit point)", "commit_append_s"),
        ("  materialize: revision files", "commit_files_s"),
        ("  materialize: directory syncs", "commit_dirs_s"),
        ("  head (rename)", "commit_head_s"),
        ("  index (extend + publish)", "commit_index_s"),
        ("  after the index", "commit_rest_s"),
        ("marker remove", "marker_remove_s"),
    ]
    for name, key in rows:
        print(f"{name:46} {r[key]:8.2f} s")
    within = "within" if r["total_s"] < TARGET_SECONDS else "OVER"
    print(f"{'total':46} {r['total_s']:8.2f} s  ({within} the {TARGET_SECONDS:.0f} s target)")
    within = "within" if r["rss_peak_mib"] < TARGET_MIB else "OVER"
    print(
        f"{'peak RSS (at start ' + format(r['rss_start_mib'], '.0f') + ' MiB)':46}"
        f" {r['rss_peak_mib']:8.0f} MiB ({within} the {TARGET_MIB:.0f} MiB target)"
    )
    print(
        f"migrated {r['migrated']} revisions on top of {r['head_seq']} txs, findings"
        f" {r['findings']}, the tx line is {r['tx_line_mb']:.1f} MB"
    )
    for name, key in (
        ("after the migration", "reader"),
        ("migration tx as log tail", "reader_tail"),
    ):
        opened, read = r[f"{key}_open_s"] * 1000, r[f"{key}_read_s"] * 1000
        print(f"reader, first open / read, {name:26} {opened:6.0f} ms / {read:.0f} ms")
    c = r["confirm"]
    if c:
        print(
            f"confirm of every entity: grouped {c['grouped_s'] * 1000:.0f} ms, one tx read per"
            f" entity {c['per_entity_s'] * 1000:.0f} ms (extrapolated from {c['sample']})"
        )
    else:
        print("the writer replays the log (no usable index): no confirm")


if __name__ == "__main__":
    main()
