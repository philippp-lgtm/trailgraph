"""Running graph-admin as a subprocess and reading its JSON envelope (S tests)."""

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import CliRunner


@dataclass(frozen=True)
class Outcome:
    """An envelope as printed; ``graph`` prints the short one, without null or empty keys."""

    code: int
    envelope: dict[str, Any]
    stderr: str

    @property
    def data(self) -> Any:
        return self.envelope.get("data")

    @property
    def error(self) -> dict[str, Any]:
        error = self.envelope.get("error")
        assert isinstance(error, dict), self.envelope
        return error

    @property
    def warnings(self) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = self.envelope.get("warnings", [])
        return warnings

    @property
    def hints(self) -> list[dict[str, Any]]:
        hints: list[dict[str, Any]] = self.envelope.get("hints", [])
        return hints

    @property
    def page(self) -> dict[str, Any] | None:
        return self.envelope.get("page")

    @property
    def warning_codes(self) -> list[str]:
        return [w["code"] for w in self.warnings]


def parse(result: subprocess.CompletedProcess[str]) -> Outcome:
    assert result.stdout.count("\n") == 1, (result.stdout, result.stderr)
    return Outcome(result.returncode, json.loads(result.stdout), result.stderr)


def admin(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    """``graph-admin <args> --json``; stderr must stay empty in JSON mode (B9.1)."""
    outcome = parse(run_cli("graph-admin", *args, "--json", env=env))
    assert outcome.stderr == "", outcome.stderr
    return outcome


def data_root(env: dict[str, str]) -> Path:
    """The default data root of the isolated env (B12.1 step 4)."""
    return (Path(env["XDG_DATA_HOME"]) / "graph_cli").resolve()


def init(run_cli: CliRunner, env: dict[str, str]) -> Path:
    assert admin(run_cli, "init").code == 0
    return data_root(env)


def write_random_txs(root: Path, graph_id: str, *, ontology: str, version: str, count: int) -> None:
    """Instance data written the way item 6's write path will: real intent, then commit."""
    from graph_cli.kernel.ids import GraphId, parse_id
    from graph_cli.storage.files.store import FileStore
    from support import audit_events
    from support.clock import StepClock
    from support.fixtures import compiled_model
    from support.ids import SeededIdProvider
    from support.stores import snapshot_state
    from support.txbuilder import TxBuilder

    gid = GraphId(parse_id(graph_id))
    clock = StepClock(start="2030-01-01T00:00:00Z")
    builder = TxBuilder(
        compiled_model(ontology, version),
        graph_id=gid,
        ids=SeededIdProvider(clock, 11),
        clock=clock,
    )
    store = FileStore(root / "graphs" / graph_id, gid)
    for _ in range(count):
        with store.session(timeout_s=5) as session:
            draft = builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            builder.random_tx(draft)
            tx = draft.build()
            before = audit_events.before_hashes(session.snapshot(), tx)
            receipt = session.append_audit(audit_events.intent(tx, before=before))
            session.commit(tx, receipt)


def holding_markings(entry: Any, markings: Iterable[str]) -> Any:
    """A policy source whose installation policy lets the local app caller write ``entry``'s
    graph and hold ``markings`` there (a test-data writer for graphs that use markings)."""
    from graph_cli.access_control.compiler import compile_policy
    from graph_cli.access_control.policy import policy_set

    text = (
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        f"  - {{entrypoint: app, graphs: [{entry.name}], level: write,"
        f" markings: [{', '.join(sorted(markings))}]}}\n"
        'no_project: {"*": write}\n'
    )
    compiled = compile_policy(
        text.encode(), file="installation.yaml", graphs={entry.name: entry.id}
    )
    assert compiled.document is not None, compiled
    return FixedPolicy(policy_set(compiled.document))


class FixedPolicy:
    """A ``PolicySource`` that always answers one policy set."""

    def __init__(self, policy: Any) -> None:
        self.policy = policy

    def current(self, graph: object) -> Any:
        return self.policy

    def refresh(self) -> None:
        pass


def write_documents(
    root: Path, graph: str, documents: list[dict[str, Any]], *, seed: int = 13
) -> list[int]:
    """Instance data through the real WriteService on the CLI's data root; returns the seqs.
    Ids come from a seeded provider: a second call with the same ``seed`` repeats the ids. The
    writer is the local app caller under the built-in policy; when the graph's pinned model uses
    markings it also holds every one of them (``holding_markings``), so the data may carry
    values of marked attributes, which a caller without the marking cannot write."""
    from graph_cli.access_control.policy import BuiltinPolicySource
    from graph_cli.catalog.file_catalog import FileCatalog
    from graph_cli.kernel.canonical import hash_value
    from graph_cli.kernel.records import EnvInfo, RequestMeta
    from graph_cli.services.models import ModelCache
    from graph_cli.services.write import WriteRequest, WriteService
    from graph_cli.storage.files.audit_log import FileAuditLog
    from support.clock import StepClock
    from support.ids import SeededIdProvider
    from support.services import app_actor

    catalog = FileCatalog(root)
    entry = catalog.resolve(graph)
    used = ModelCache(catalog).load(entry).used_markings()
    clock = StepClock(start="2030-01-01T00:00:00Z")
    service = WriteService(
        catalog=catalog,
        audit=FileAuditLog(root),
        policy=holding_markings(entry, used) if used else BuiltinPolicySource(),
        clock=clock,
        ids=SeededIdProvider(clock, seed),
        env=EnvInfo("<data root>", "default"),
    )
    requests = [
        WriteRequest(
            app_actor(), graph, service.parse(doc), False, RequestMeta("apply", hash_value(doc))
        )
        for doc in documents
    ]
    outcome = service.apply_batch(requests)
    assert outcome.error is None, outcome.error.details if outcome.error else None
    return [r.seq or 0 for r in outcome.results]
