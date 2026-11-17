"""S: ``graph apply`` with reconcile documents on a throwaway data root, on the toy ontology of
``support.reconcile_toy`` (reconcile plan section 9, step 4): a complete run, a run that writes no
tx, a dry run, a JSONL batch, a guard refusal with its exit code and envelope, the audit, history
and the change feed naming the source, and the text form."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app
from support.reconcile_toy import _MODULE, doc, row, rows, scope

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
SOURCE = {"name": "inventory", "version": "collector 3", "run": "run-1"}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    ontology = tmp_path / "ontology"
    ontology.mkdir()
    (ontology / "example_module.yaml").write_text(_MODULE)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(ontology), "--language", "en"
    )
    assert created.code == 0, created.envelope
    return root


def apply(run_cli: CliRunner, *documents: dict[str, Any], args: tuple[str, ...] = ()) -> Outcome:
    text = "".join(json.dumps(d) + "\n" for d in documents)
    return app(run_cli, "apply", "-", *args, env=ENV, input=text)


def block(outcome: Outcome, index: int = 0) -> dict[str, Any]:
    (scope_block,) = outcome.data["results"][index]["result"]["reconcile"]["scopes"]
    return scope_block


def test_a_complete_run_then_a_run_that_writes_nothing(run_cli: CliRunner, root: Path) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq", "zlib")), reason="Run 1")
    first = apply(run_cli, document)
    assert first.code == 0, first.envelope
    result = first.data["results"][0]["result"]
    assert result["seq"] == 1 and result["entity_count"] == 3
    assert result["reconcile"]["source"] == SOURCE
    assert block(first) == {
        "type": "ex:Installation",
        "under": "",
        "complete": True,
        "created": 3,
        "updated": 0,
        "unchanged": 0,
        "moved": 0,
        "adopted": 0,
        "kept": 0,
        "retired": 0,
        "revived": 0,
        "retired_keys": [],
    }
    assert first.envelope["graph"]["seq"] == 1
    again = apply(run_cli, document)
    assert again.code == 0, again.envelope
    assert again.data["results"][0]["result"]["seq"] is None
    assert again.data["results"][0]["result"]["entity_count"] == 0
    assert block(again)["unchanged"] == 3 and block(again)["created"] == 0
    assert again.envelope["graph"]["seq"] is None  # no tx
    changed = apply(
        run_cli, doc(scope(complete=True, items=[row("host_a/curl", "9"), *rows("jq")]))
    )
    assert block(changed)["updated"] == 1 and block(changed)["retired"] == 1
    assert block(changed)["retired_keys"] == ["host_a/zlib"]
    assert changed.data["results"][0]["result"]["seq"] == 2


def test_a_dry_run_lists_every_retired_key_and_writes_nothing(
    run_cli: CliRunner, root: Path
) -> None:
    names = [f"p{n:02d}" for n in range(30)]
    assert apply(run_cli, doc(scope(items=rows(*names)))).code == 0
    wipe = doc(scope(complete=True, allow_retire=30, allow_empty=True))
    dry = apply(run_cli, wipe, args=("--dry-run",))
    assert dry.code == 0, dry.envelope
    assert dry.data["dry_run"] is True and dry.data["results"][0]["result"]["seq"] is None
    assert block(dry)["retired"] == 30 and len(block(dry)["retired_keys"]) == 30
    assert dry.envelope["graph"]["seq"] is None
    written = apply(run_cli, wipe)
    assert block(written)["retired"] == 30 and len(block(written)["retired_keys"]) == 20
    summary = apply(run_cli, doc(scope(items=rows("again"))), args=("--summary",))
    assert summary.data["results"][0]["result"]["entities"] is None
    assert block(summary)["created"] == 1  # the block stays with ``--summary``


def test_a_jsonl_batch_is_one_tx_per_document(run_cli: CliRunner, root: Path) -> None:
    batch = apply(
        run_cli,
        doc(scope(under="a/", items=[row("a/x")])),
        doc(scope(under="b/", items=[row("b/x")])),
        doc(scope(under="a/", complete=True, items=[row("a/x")])),
    )
    assert batch.code == 0, batch.envelope
    assert batch.data["documents"] == batch.data["applied"] == 3
    assert [r["result"]["seq"] for r in batch.data["results"]] == [1, 2, None]
    assert [block(batch, i)["created"] for i in range(3)] == [1, 1, 0]
    assert batch.envelope["graph"]["seq"] is None  # the last document wrote nothing


def test_a_guard_refusal_has_the_exit_code_envelope_and_audit_of_an_invalid_write(
    run_cli: CliRunner, root: Path
) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    assert apply(run_cli, doc(scope(items=rows(*names)))).code == 0
    refused = apply(run_cli, doc(scope(complete=True, items=rows(*names[:10]))))
    assert refused.code == 1
    assert (
        refused.error["code"] == "reconcile.retire_limit" and refused.error["category"] == "invalid"
    )
    assert refused.error["details"]["count"] == 10 and refused.error["details"]["limit"] == 5
    assert refused.error["details"]["field"] == "allow_retire"
    assert "allow_retire" in refused.error["message"] and "document 0" in refused.error["message"]
    assert refused.error["hint"] == (
        "graph find --type ex:Installation --where source_key:prefix=inventory/"
    )
    (failed,) = [r for r in refused.data["results"] if not r["ok"]]
    assert failed["error"]["code"] == "reconcile.retire_limit"
    assert refused.data["applied"] == 0
    found = app(run_cli, "find", "--type", "ex:Installation", "--limit", "100", env=ENV)
    assert found.page is not None and found.page["total"] == 20  # nothing was retired
    # the same code from the hint's own command: the scope is what it was
    again = apply(run_cli, doc(scope(complete=True, allow_retire=10, items=rows(*names[:10]))))
    assert again.code == 0 and block(again)["retired"] == 10
    assert admin(run_cli, "audit", "verify").code == 0
    graphs = list((root / "graphs").iterdir())
    (events,) = (graphs[0] / "audit").glob("events-*.jsonl")
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    refusal = next(e for e in lines if e["result"].get("code") == "reconcile.retire_limit")
    assert refusal["result"]["status"] == "invalid"
    assert refusal["request"]["source"] == SOURCE
    assert refusal["request"]["command"] == "apply"


def test_other_errors_of_a_reconcile_document(run_cli: CliRunner, root: Path) -> None:
    unknown = apply(run_cli, doc(scope(type="ex:Nothing")))
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    key = apply(run_cli, doc({"type": "ex:Loose", "key": "source_key", "items": []}))
    assert (key.code, key.error["code"]) == (1, "reconcile.key_attribute")
    assert key.error["hint"] == "graph describe type ex:Loose"
    malformed = apply(run_cli, {"reconcile": [scope()]})  # no source
    assert (malformed.code, malformed.error["code"]) == (2, "changeset.malformed")
    assert malformed.error["details"]["path"] == "source"
    assert apply(run_cli, doc(scope(items=[{"key": "a", "x": 1}]))).code == 2
    empty = apply(run_cli, doc(scope(complete=True)))
    assert (
        empty.code == 0 and empty.data["results"][0]["result"]["seq"] is None
    )  # nothing to retire
    assert find_total(run_cli) == 0


def find_total(run_cli: CliRunner) -> int:
    found = app(run_cli, "find", "--type", "ex:Installation", env=ENV)
    assert found.page is not None
    total: int = found.page["total"]
    return total


def test_an_idempotent_retry_replays_without_the_block(run_cli: CliRunner, root: Path) -> None:
    document = doc(scope(items=rows("curl")))
    first = apply(run_cli, document, args=("--idempotency-key", "run-1"))
    assert first.data["results"][0]["result"]["seq"] == 1
    retry = apply(run_cli, document, args=("--idempotency-key", "run-1"))
    result = retry.data["results"][0]["result"]
    assert result["idempotent_replay"] is True and result["seq"] == 1
    assert "reconcile" not in result
    other = apply(run_cli, doc(scope(items=rows("jq"))), args=("--idempotency-key", "run-1"))
    assert (other.code, other.error["code"]) == (4, "conflict.idempotency_key_reuse")


def test_history_and_the_feed_name_the_source(run_cli: CliRunner, root: Path) -> None:
    assert apply(run_cli, doc(scope(items=rows("curl")))).code == 0
    entity = app(run_cli, "find", "--type", "ex:Installation", env=ENV).data["items"][0]["id"]
    history = app(run_cli, "history", entity, env=ENV)
    assert history.data["items"][0]["source"] == SOURCE
    assert history.data["items"][0]["command"] == "apply"
    changes = app(run_cli, "changes", "--since", "tx:0", env=ENV)
    assert changes.data["items"][0]["source"] == SOURCE
    plain = app(
        run_cli, "create", "ex:Host", "--set-json", 'hostname="h"', "--reason", "r", env=ENV
    )
    assert plain.code == 0, plain.envelope
    page = app(run_cli, "changes", "--since", "tx:0", env=ENV).data["items"]
    assert "source" in page[0] and "source" not in page[1]


def test_the_text_form_of_the_answer(run_cli: CliRunner, root: Path) -> None:
    text = json.dumps(doc(scope(complete=True, items=rows("curl", "jq"))))
    done = run_cli("graph", "apply", "-", env=ENV, input=text)
    assert done.returncode == 0, done.stderr
    assert "reconcile" in done.stdout and "created" in done.stdout
    refused = run_cli("graph", "apply", "-", env=ENV, input=json.dumps(doc(scope(complete=True))))
    assert refused.returncode == 1
    assert "reconcile.source_empty" in refused.stderr + refused.stdout
