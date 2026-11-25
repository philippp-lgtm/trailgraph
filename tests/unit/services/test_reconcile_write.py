"""A reconcile document through the write service (reconcile plan R-K6, R-K8 to R-K11).

One tx per run that changes something, none for a run that changes nothing, ``request.source``
in the intent, the tx record, history and the change feed, idempotency by digest and source,
audited guard refusals, declared needs decided before anything is planned, the callers that do
not see a type or an attribute, and the rules every write obeys (migration, target policy).
"""

import json
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from support.reconcile_toy import REQUEST, World, doc, row, rows, scope

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.changeset import parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TxId
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.records import RequestMeta, SourceRef
from graph_cli.services.read import ReadService
from graph_cli.services.write import WriteRequest, WriteResult, WriteService
from graph_cli.temporal_knowledge.changes import FeedPosition

GRAPH = "example_graph"
SOURCE = SourceRef("inventory", "collector 3", "run-1")
EDITOR = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
HOLDER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [ex:personal]}
no_project: {"*": write}
"""
# may do everything with installations except retract them
NO_RETRACT = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ex:Installation: {create: true, update: true, link: true, unlink: true, transition: true}
no_project: {"*": write}
"""
# the same, with the attributes spelled out: a type with term rules
NO_RETRACT_TERMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ex:Installation:
        create: [source_key, package, version]
        update: [source_key, package, version]
        link: true
        unlink: true
no_project: {"*": write}
"""
HIDE_GADGETS = """\
schema: graph_cli.policy/1
id: graph
types:
  "ex:Gadget": none
"""
HIDE_TOOLS = """\
schema: graph_cli.policy/1
id: graph
types:
  "ex:Tool": none
"""


class Fixed:
    def __init__(self, *texts: tuple[str, str]) -> None:
        self.texts = texts
        self.policy: Any = None

    def bind(self, world: World) -> "Fixed":
        graphs = {GRAPH: world.env.entry.id}
        documents = [
            compile_policy(
                text.encode(),
                file=f"{kind}.yaml",
                graphs=graphs,
                graph=world.env.entry.id if kind == "graph" else None,
                roles=frozenset(),
            ).document
            for kind, text in self.texts
        ]
        self.policy = policy_set(*documents)
        return self

    def current(self, graph: object) -> Any:
        return self.policy

    def refresh(self) -> None:
        pass


def caller(world: World, *texts: tuple[str, str]) -> WriteService:
    return world.env.make_service(Fixed(*texts).bind(world))  # type: ignore[arg-type]


def request(
    world: World, document: Mapping[str, Any], *, dry_run: bool = False, key: str | None = None
) -> WriteRequest:
    data = dict(document)
    if key is not None:
        data["idempotency_key"] = key
    cs = parse_changeset(data)
    assert cs.source is not None
    return WriteRequest(
        world.env.actor,
        world.env.name,
        cs,
        dry_run,
        RequestMeta.of("apply", {"document": json.dumps(data, sort_keys=True)}, source=cs.source),
    )


def run(
    world: World,
    document: Mapping[str, Any],
    service: WriteService | None = None,
    **kw: Any,
) -> WriteResult:
    return (service or world.env.service).apply(request(world, document, **kw))


def refused(
    world: World, document: Mapping[str, Any], service: WriteService | None = None, **kw: Any
) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        run(world, document, service, **kw)
    return info.value


def graph_events(world: World) -> list[Mapping[str, Any]]:
    return [dict(e) for e in world.env.graph_events()]


def block(result: WriteResult) -> dict[str, Any]:
    assert isinstance(result.reconcile, dict)
    (scope_block,) = result.reconcile["scopes"]
    return scope_block


def counts(result: WriteResult) -> dict[str, int]:
    return {
        k: v for k, v in block(result).items() if isinstance(v, int) and not isinstance(v, bool)
    }


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# ---- one tx, no tx -------------------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.source_is_a_claim")
def test_create_update_and_retire_are_one_tx_that_names_its_source(world: World) -> None:
    first = run(world, doc(scope(complete=True, items=rows("curl", "jq", "zlib"))))
    assert (first.seq, first.reconcile is not None) == (1, True)
    assert counts(first)["created"] == 3 and len(first.entities) == 3
    second = run(
        world,
        doc(scope(complete=True, items=[row("host_a/curl", "9"), *rows("jq", "bash")])),
    )
    assert second.seq == 2  # one tx for the update, the create and the retire together
    assert counts(second) == {
        "created": 1,
        "updated": 1,
        "unchanged": 1,
        "moved": 0,
        "adopted": 0,
        "kept": 0,
        "retired": 1,
        "revived": 0,
    }
    assert block(second)["retired_keys"] == ["host_a/zlib"]
    assert world.env.head_seq() == 2
    tx = world.env.txs()[1]
    assert tx.request.source == SOURCE
    assert tx.reason == "reconcile inventory collector 3 run run-1"  # the default reason
    assert "reconcile" not in tx.changeset and "source" not in tx.changeset
    assert [op["op"] for op in tx.changeset["ops"]] == ["create", "update", "retract"]  # type: ignore[union-attr,index]
    assert {e.ops for e in second.entities} == {("create",), ("update",), ("retract",)}
    intent = graph_events(world)[-1]
    assert intent["request"]["source"] == SOURCE.to_json()
    assert not [f for f in world.env.verify().findings if f.severity.value == "error"]


def test_a_document_reason_is_kept(world: World) -> None:
    run(world, doc(scope(items=rows("curl")), reason="Inventory run of host-a"))
    assert world.env.txs()[0].reason == "Inventory run of host-a"


def test_a_run_that_changes_nothing_writes_no_tx_and_no_event(world: World) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq")))
    run(world, document)
    events = len(graph_events(world))
    again = run(world, document)
    assert (again.seq, again.tx_id, again.event_id, again.entities) == (None, None, None, ())
    assert counts(again)["unchanged"] == 2 and counts(again)["created"] == 0
    assert world.env.head_seq() == 1 and len(graph_events(world)) == events


def test_a_dry_run_plans_and_writes_nothing(world: World) -> None:
    run(world, doc(scope(items=rows("curl", "jq"))))
    events = len(graph_events(world))
    result = run(
        world, doc(scope(complete=True, allow_retire=1, items=rows("curl", "bash"))), dry_run=True
    )
    assert result.dry_run and result.seq is None
    assert counts(result)["created"] == 1 and counts(result)["retired"] == 1
    assert world.env.head_seq() == 1 and len(graph_events(world)) == events
    assert world.find("inventory/host_a/bash") is None


def test_a_dry_run_lists_every_retired_key_and_a_write_at_most_twenty(world: World) -> None:
    names = [f"p{n:02d}" for n in range(30)]
    run(world, doc(scope(items=rows(*names))))
    document = doc(scope(complete=True, allow_retire=30, allow_empty=True))
    assert len(block(run(world, document, dry_run=True))["retired_keys"]) == 30
    written = run(world, document)
    assert block(written)["retired"] == 30 and len(block(written)["retired_keys"]) == 20


def test_the_answer_json_carries_the_block_only_for_a_reconcile_document(world: World) -> None:
    result = run(world, doc(scope(items=rows("curl"))))
    data = result.to_json()
    assert data["reconcile"]["source"] == SOURCE.to_json()  # type: ignore[index]
    plain = world.env.apply({"op": "create", "type": "ex:Host", "attrs": {"hostname": "h"}})
    assert "reconcile" not in plain.to_json()


def test_the_locals_of_matched_and_created_items_are_bound(world: World) -> None:
    items = [
        {"key": "a/old", "local": "old", "attrs": {"package": "o"}},
        {"key": "a/new", "local": "new", "attrs": {"package": "n"}},
    ]
    first = run(world, doc(scope(under="a/", items=items[:1])))
    second = run(world, doc(scope(under="a/", items=items)))
    stored = world.find("inventory/a/old")
    assert stored is not None
    assert set(second.bindings) == {"old", "new"} and second.bindings["old"] == stored.id
    assert first.bindings["old"] == stored.id
    more = [*items, {"key": "a/newer", "local": "newer", "attrs": {"package": "m"}}]
    dry = run(world, doc(scope(under="a/", items=more)), dry_run=True)
    assert set(dry.bindings) == {"old", "new"}  # a created entity has no id in a dry run


def test_a_batch_of_reconcile_documents_is_one_tx_each(world: World) -> None:
    documents = [
        doc(scope(under="a/", items=[row("a/x")])),
        doc(scope(under="b/", items=[row("b/x")])),
    ]
    requests = [request(world, d) for d in documents]
    batch = world.env.service.apply_batch(requests)
    assert batch.error is None and [r.seq for r in batch.results] == [1, 2]
    dry = world.env.service.apply_batch([replace(r, dry_run=True) for r in requests])
    assert dry.error is None and [counts(r)["unchanged"] for r in dry.results] == [1, 1]


def test_a_dry_run_batch_plans_a_document_on_the_one_before(world: World) -> None:
    first = doc(scope(under="a/", items=[row("a/x")]))
    second = doc(scope(under="a/", complete=True, items=[row("a/y")]))
    batch = world.env.service.apply_batch(
        [replace(request(world, d), dry_run=True) for d in (first, second)]
    )
    assert batch.error is None
    assert counts(batch.results[1])["retired"] == 1  # it sees the entity the first would create


# ---- idempotency ---------------------------------------------------------------------------------


def test_a_retry_with_the_same_key_replays_whatever_the_state_is_now(world: World) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq")))
    first = run(world, document, key="run-1")
    stored = world.find("inventory/host_a/curl")
    assert stored is not None
    # the state moved on: a plan against it would retire nothing and write nothing
    world.plain({"op": "retract", "id": str(stored.id)})
    again = run(world, document, key="run-1")
    assert again.idempotent_replay and again.tx_id == first.tx_id and again.seq == 1
    assert again.reconcile is None  # the tx holds the expanded ops only
    assert world.env.head_seq() == 2 and len(world.env.txs()) == 2


def test_a_retry_replays_even_when_the_guards_would_refuse_now(world: World) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    run(world, doc(scope(items=rows(*names))))
    document = doc(scope(complete=True, allow_retire=6, items=rows(*names[:14])))
    first = run(world, document, key="k")
    assert block(first)["retired"] == 6
    assert run(world, document, key="k").idempotent_replay


def test_a_key_used_for_other_content_or_another_source_is_a_conflict(world: World) -> None:
    run(world, doc(scope(items=rows("curl"))), key="k")
    other = refused(world, doc(scope(items=rows("curl", "jq"))), key="k")
    assert other.code == "conflict.idempotency_key_reuse"
    elsewhere = refused(world, doc(scope(items=rows("curl")), source={"name": "other"}), key="k")
    assert elsewhere.code == "conflict.idempotency_key_reuse"
    refusal = graph_events(world)[-1]
    assert (refusal["result"]["status"], refusal["result"]["code"]) == (
        "conflict",
        "conflict.idempotency_key_reuse",
    )
    assert refusal["request"]["source"]["name"] == "other"


def test_the_source_decides_a_plain_retry_too(world: World) -> None:
    ops = [{"op": "create", "type": "ex:Host", "attrs": {"hostname": "h"}}]
    first = run(world, {"source": {"name": "inventory"}, "ops": ops}, key="k")
    assert run(world, {"source": {"name": "inventory"}, "ops": ops}, key="k").idempotent_replay
    assert first.seq == 1
    other = refused(world, {"source": {"name": "other"}, "ops": ops}, key="k")
    assert other.code == "conflict.idempotency_key_reuse"
    assert world.env.txs()[0].request.source == SourceRef("inventory")


def test_a_run_that_changes_nothing_does_not_hold_its_key(world: World) -> None:
    document = doc(scope(items=rows("curl")))
    run(world, document)
    assert run(world, document, key="k").seq is None
    assert run(world, doc(scope(items=rows("curl", "jq"))), key="k").seq == 2


# ---- concurrency ---------------------------------------------------------------------------------


def test_two_concurrent_runs_the_second_sees_the_first(world: World) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq", "zlib")))
    barrier = threading.Barrier(2)
    answers: list[WriteResult | GraphCliError] = []

    def go() -> None:
        barrier.wait()
        try:
            answers.append(run(world, document))
        except GraphCliError as exc:  # pragma: no cover - failing the assertion below
            answers.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results = [a for a in answers if isinstance(a, WriteResult)]
    assert len(results) == 2, answers
    assert sorted(counts(r)["created"] for r in results) == [0, 3]  # one wrote, one found it done
    assert world.env.head_seq() == 1 and world.count("ex:Installation") == 3


# ---- guards are audited refusals -----------------------------------------------------------------


@pytest.mark.doc_claim("security_model.source_refusals_audited")
def test_a_guard_refusal_is_audited_with_its_decisions_and_source(world: World) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    run(world, doc(scope(items=rows(*names))))
    before = len(graph_events(world))
    error = refused(world, doc(scope(complete=True, items=rows(*names[:10]))))
    assert error.code == "reconcile.retire_limit" and error.details["count"] == 10
    events = graph_events(world)
    assert len(events) == before + 1
    refusal = events[-1]
    assert (refusal["result"]["status"], refusal["result"]["code"]) == (
        "invalid",
        "reconcile.retire_limit",
    )
    assert refusal["request"]["source"] == SOURCE.to_json()
    assert {d["action"] for d in refusal["authz"]["decisions"]} >= {"write.create", "write.retract"}
    assert world.env.head_seq() == 1  # nothing was written
    assert not [f for f in world.env.verify().findings if f.severity.value == "error"]


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (doc(scope(complete=True)), "reconcile.source_empty"),
        (doc(scope(expect={"min_items": 5}, items=rows("a"))), "reconcile.shape"),
        (doc(scope(under="x/"), scope(under="x/y/")), "reconcile.scope_overlap"),
        (
            doc({"type": "ex:Loose", "key": "source_key", "items": []}),
            "reconcile.key_attribute",
        ),
        (
            doc(scope(moves=[{"from": "a/b", "to": "a/c"}], items=[{"key": "a/c"}])),
            "reconcile.move_invalid",
        ),
        (
            doc(
                scope(
                    edges=["ex:installed_on"],
                    items=[
                        {
                            "key": "a/x",
                            "edges": [
                                {
                                    "rel": "ex:installed_on",
                                    "target": {"type": "ex:Host", "attr": "hostname", "value": "?"},
                                }
                            ],
                        }
                    ],
                )
            ),
            "reconcile.target_not_found",
        ),
    ],
)
@pytest.mark.doc_claim("security_model.source_refusals_audited")
def test_every_guard_is_an_audited_invalid_refusal(
    world: World, document: dict[str, Any], code: str
) -> None:
    run(world, doc(scope(items=rows("curl", "jq"))))
    before = len(graph_events(world))
    assert refused(world, document).code == code
    refusal = graph_events(world)[-1]
    assert len(graph_events(world)) == before + 1
    assert (refusal["result"]["status"], refusal["result"]["code"]) == ("invalid", code)
    # a dry run raises the same error without an event
    assert refused(world, document, dry_run=True).code == code
    assert len(graph_events(world)) == before + 1


def test_a_scope_conflict_is_an_audited_refusal(world: World) -> None:
    run(world, doc(scope(items=rows("curl"))))
    stored = world.find("inventory/host_a/curl")
    assert stored is not None
    conflict = doc(
        scope(items=rows("curl")),
        ops=[{"op": "update", "id": str(stored.id), "set": {"count": 1}}],
    )
    assert refused(world, conflict).code == "reconcile.scope_conflict"
    assert graph_events(world)[-1]["result"]["code"] == "reconcile.scope_conflict"


def test_a_type_mismatch_is_an_audited_refusal(world: World) -> None:
    item = {"type": "ex:Item", "key": "source_key"}
    run(world, doc({**item, "items": [{"key": "a/x", "type": "ex:Tool"}]}))
    other = doc({**item, "items": [{"key": "a/x", "type": "ex:Gadget"}]})
    assert refused(world, other).code == "reconcile.type_mismatch"
    assert graph_events(world)[-1]["result"]["code"] == "reconcile.type_mismatch"


def test_other_planner_errors_are_raised_as_plain_ops_raise_them(world: World) -> None:
    before = len(graph_events(world))
    unknown = refused(world, doc(scope(type="ex:Nothing")))
    assert (unknown.code, unknown.details["kind"]) == ("term.unknown", "type")
    clash = {"key": "a/x", "local": "dup"}
    own = {"op": "create", "type": "ex:Host", "local": "dup", "attrs": {"hostname": "h"}}
    duplicate = refused(world, doc(scope(under="a/", fields=[], items=[clash]), ops=[own]))
    assert duplicate.code == "changeset.local_ref_duplicate"
    retire = {"transition": "removed"}
    plain = {"type": "ex:Plain", "key": "source_key", "retire": retire, "items": []}
    assert refused(world, doc(plain)).code == "changeset.malformed"
    assert len(graph_events(world)) == before  # none of them is an audited refusal


def test_an_expansion_above_the_op_limit_is_refused_unaudited(world: World) -> None:
    items = [{"key": f"a/{n}"} for n in range(5001)]
    error = refused(world, doc(scope(under="a/", fields=[], items=items)))
    assert error.code == "changeset.too_large" and error.details["limit_ops"] == 5000


# ---- declared needs, before anything is planned --------------------------------------------------


@pytest.mark.doc_claim("security_model.source_is_a_claim")
def test_a_token_without_the_retract_right_is_denied_on_its_first_run(world: World) -> None:
    service = caller(world, ("installation", NO_RETRACT))
    partial = doc(scope(items=rows("curl")))
    assert run(world, partial, service).seq == 1  # create, update, link, unlink: allowed
    before = graph_events(world)
    complete = doc(scope(complete=True, items=rows("curl")))  # nothing would retire
    error = refused(world, complete, service)
    assert error.code == "access.denied"
    assert world.env.head_seq() == 1 and graph_events(world) == before  # nothing was planned
    denial = [e for e in world.env.installation_events() if e["result"]["status"] == "denied"][-1]  # type: ignore[index]
    deny = next(d for d in denial["authz"]["decisions"] if d["effect"] == "deny")  # type: ignore[index]
    assert deny["action"] == "write.retract" and deny["resource"].endswith("/type:ex:Installation")
    assert denial["request"]["source"] == SOURCE.to_json()  # type: ignore[index]
    # a dry run is denied too, unaudited
    count = len(world.env.installation_events())
    assert refused(world, complete, service, dry_run=True).code == "access.denied"
    assert len(world.env.installation_events()) == count


def test_a_lifecycle_retire_needs_the_transition_not_the_retract(world: World) -> None:
    retire = {"transition": "removed", "revive": "present"}
    service = caller(world, ("installation", NO_RETRACT))
    assert run(world, doc(scope(retire=retire, complete=True, items=rows("a"))), service).seq == 1


def test_declared_attributes_are_decided_on_the_terms_of_a_type_with_term_rules(
    world: World,
) -> None:
    service = caller(world, ("installation", NO_RETRACT_TERMS))
    allowed = doc(scope(under="a/", items=[row("a/x")]))
    assert run(world, allowed, service).seq == 1
    # ``count`` is not a listed attribute: denied before planning, whatever the items hold
    error = refused(world, doc(scope(fields=["package", "count"], items=[])), service)
    assert error.code == "access.denied"
    assert world.env.head_seq() == 1


# ---- type read limits ----------------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.source_declared_needs")
def test_a_scope_type_the_caller_cannot_see_is_an_unknown_type(world: World) -> None:
    service = caller(world, ("installation", EDITOR), ("graph", HIDE_TOOLS))
    hidden = doc({"type": "ex:Tool", "key": "source_key", "items": [{"key": "a/x"}]})
    error = refused(world, hidden, service)
    assert (error.code, error.details["kind"], error.details["term"]) == (
        "term.unknown",
        "type",
        "ex:Tool",
    )
    nowhere = refused(world, doc({"type": "ex:Nothing", "key": "source_key", "items": []}), service)
    assert (nowhere.code, nowhere.details["kind"]) == (error.code, error.details["kind"])
    assert world.env.head_seq() == 0
    # the same caller reconciles the type it sees
    visible = doc({"type": "ex:Gadget", "key": "source_key", "items": [{"key": "a/x"}]})
    assert run(world, visible, service).seq == 1


@pytest.mark.doc_claim("security_model.source_plan_as_seen")
def test_a_key_held_by_a_hidden_subtype_fails_the_unique_check_with_the_finding_concealed(
    world: World,
) -> None:
    both = {"type": "ex:Item", "key": "source_key"}
    run(world, doc({**both, "items": [{"key": "a/x", "type": "ex:Gadget"}]}))  # unlimited
    service = caller(world, ("installation", EDITOR), ("graph", HIDE_GADGETS))
    asked = doc({**both, "items": [{"key": "a/x", "type": "ex:Tool"}]})
    error = refused(world, asked, service)
    assert error.code == "write.invalid"
    findings = error.details["findings"]
    assert [f["code"] for f in findings] == ["data.semantic.unique_violation"]  # type: ignore[index,call-overload]
    hidden = world.find("inventory/a/x", type_curie="ex:Gadget")
    assert hidden is not None and str(hidden.id) not in str(error.details)
    # a plain create of the same key answers the same
    plain = {"op": "create", "type": "ex:Tool", "attrs": {"source_key": "inventory/a/x"}}
    cs = parse_changeset({"ops": [plain], "reason": "r"})
    with pytest.raises(GraphCliError) as info:
        service.apply(WriteRequest(world.env.actor, world.env.name, cs, False, REQUEST))
    assert [f["code"] for f in info.value.details["findings"]] == [  # type: ignore[index,union-attr]
        "data.semantic.unique_violation"
    ]
    assert world.env.head_seq() == 1


# ---- markings ------------------------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.source_declared_needs")
def test_a_marked_field_is_refused_like_an_unknown_attribute(world: World) -> None:
    editor = caller(world, ("installation", EDITOR))
    asked = doc(scope(fields=["package", "owner"], items=[]))  # no item names the field
    error = refused(world, asked, editor)
    assert error.code == "write.invalid"
    (finding,) = error.details["findings"]  # type: ignore[misc]
    assert finding["code"] == "data.structural.unknown_attribute"  # type: ignore[index]
    assert finding["target"] == {"type": "ex:Installation", "path": "attrs.owner"}  # type: ignore[index]
    twin = refused(world, doc(scope(fields=["package", "nothing"], items=[])), editor)
    assert twin.details["findings"][0]["code"] == finding["code"]  # type: ignore[index]
    assert refused(world, asked, editor, dry_run=True).code == "write.invalid"
    refusal = graph_events(world)[-1]
    assert refusal["result"]["status"] == "invalid"
    assert world.env.head_seq() == 0
    # the key attribute is declared too
    keyed = doc({"type": "ex:Installation", "key": "owner", "items": []})
    assert refused(world, keyed, editor).code == "write.invalid"
    # a holder of the marking writes it
    holder = caller(world, ("installation", HOLDER))
    items = [{"key": "a/x", "attrs": {"package": "x", "owner": "ada"}}]
    assert (
        run(world, doc(scope(under="a/", fields=["package", "owner"], items=items)), holder).seq
        == 1
    )


@pytest.mark.doc_claim("security_model.source_declared_needs")
def test_a_marked_attribute_outside_the_fields_is_untouched(world: World) -> None:
    holder = caller(world, ("installation", HOLDER))
    items = [{"key": "a/x", "attrs": {"package": "x", "owner": "ada"}}]
    run(world, doc(scope(under="a/", fields=["package", "owner"], items=items)), holder)
    editor = caller(world, ("installation", EDITOR))
    changed = [{"key": "a/x", "attrs": {"package": "y"}}]
    result = run(world, doc(scope(under="a/", fields=["package"], items=changed)), editor)
    assert counts(result)["updated"] == 1
    stored = world.find("inventory/a/x")
    assert stored is not None
    assert (stored.attrs["package"], stored.attrs["owner"]) == ("y", "ada")


# ---- the rules of every write --------------------------------------------------------------------


def test_a_migration_that_is_not_finished_refuses_the_write(world: World) -> None:
    marker = MigrationMarker(
        2, TxId(uuid4()), "1.0.0", "2.0.0", "sha256:" + "a" * 64, "sha256:" + "b" * 64
    )
    world.env.set_entry(migration=marker)
    for dry_run in (False, True):
        error = refused(world, doc(scope(items=rows("curl"))), dry_run=dry_run)
        assert error.code == "write.migration_incomplete"
    assert world.env.head_seq() == 0


def test_a_target_that_would_become_inactive_blocks_the_whole_document(world: World) -> None:
    packages = {"type": "ex:Package", "key": "source_key", "fields": ["name"], "complete": True}
    run(world, doc({**packages, "items": [{"key": "p/curl", "attrs": {"name": "curl"}}]}))
    stored = world.find("inventory/p/curl", type_curie="ex:Package")
    assert stored is not None
    world.plain(
        {
            "op": "create",
            "type": "ex:Installation",
            "attrs": {"package": "curl"},
            "edges": [{"rel": "ex:installation_of", "target": str(stored.id)}],
        }
    )
    both = doc(
        {**packages, "allow_empty": True, "items": []},
        scope(under="n/", items=[row("n/fresh")]),
    )
    error = refused(world, both)
    assert error.code == "write.invalid"
    assert [f["code"] for f in error.details["findings"]] == ["data.referential.target_referenced"]  # type: ignore[index,union-attr]
    assert world.find("inventory/n/fresh") is None  # the create of the other scope did not happen
    assert world.find("inventory/p/curl", type_curie="ex:Package") is not None


def test_a_policy_that_demands_a_reason_is_met_by_the_default_reason(world: World) -> None:
    require = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
    layer = """\
schema: graph_cli.policy/1
id: graph
obligations:
  - {action: [write.create], require_reason: true}
"""
    service = caller(world, ("installation", require), ("graph", layer))
    assert run(world, doc(scope(items=rows("curl"))), service).seq == 1


# ---- history and the change feed -----------------------------------------------------------------


def test_source_is_in_history_and_the_feed(world: World) -> None:
    run(world, doc(scope(items=rows("curl"))))
    world.plain({"op": "create", "type": "ex:Host", "attrs": {"hostname": "h"}})
    entity = world.find("inventory/host_a/curl")
    assert entity is not None
    reader: ReadService = world.env.make_reader()
    view = reader.open(
        world.env.actor,
        world.env.catalog.resolve(world.env.name),
        "read.history",
        RequestMeta("history", "sha256:" + "0" * 64),
    )
    data, _, _ = reader.history(view, world.env.actor, str(entity.id), limit=10, offset=0)
    (item,) = data["items"]  # type: ignore[misc,union-attr]
    assert item["source"] == SOURCE.to_json()  # type: ignore[index]
    assert list(item).index("source") > list(item).index("changed")  # additive, last of the tx part
    page = reader.changes(view, FeedPosition(0), limit=50)
    first, second = page["items"]  # type: ignore[misc,assignment]
    assert first["source"] == SOURCE.to_json() and "source" not in second  # type: ignore[index,operator]
    keys = list(first)  # type: ignore[call-overload]
    assert keys.index("source") < keys.index("entities")
    assert keys[-1] == "entities"


# ---- a bulk write reads the unique values in one pass ------------------------------------------


@pytest.fixture(params=["memory", "files"])
def stored(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> World:
    """The graph on each store: the query behind the unique check differs."""
    if request.param == "memory":
        return World(tmp_path)
    return World.on_files(tmp_path, monkeypatch)


def _installations(count: int, *, clash: dict[int, str] | None = None) -> list[dict[str, Any]]:
    keys = {n: f"inventory/bulk/{n:04d}" for n in range(count)} | (clash or {})
    return [
        {"op": "create", "type": "ex:Installation", "attrs": {"source_key": key}}
        for _, key in sorted(keys.items())
    ]


def test_a_bulk_write_finds_a_unique_value_used_twice_in_it(stored: World) -> None:
    ops = _installations(100, clash={99: "inventory/bulk/0003"})
    with pytest.raises(GraphCliError) as info:
        stored.plain(*ops)
    findings = info.value.details["findings"]
    assert [f["code"] for f in findings] == ["data.semantic.unique_violation"] * 2  # type: ignore[index,union-attr]
    assert stored.env.head_seq() == 0


def test_a_bulk_write_finds_a_unique_value_an_existing_entity_holds(stored: World) -> None:
    stored.plain(
        {"op": "create", "type": "ex:Installation", "attrs": {"source_key": "inventory/x"}}
    )
    with pytest.raises(GraphCliError) as info:
        stored.plain(*_installations(100, clash={50: "inventory/x"}))
    findings = info.value.details["findings"]
    assert [f["code"] for f in findings] == ["data.semantic.unique_violation"]  # type: ignore[index,union-attr]
    assert stored.env.head_seq() == 1
    stored.plain(*_installations(100))  # without the clash it is written
    assert stored.env.head_seq() == 2


def test_a_bulk_reconcile_run_is_as_valid_as_a_small_one(stored: World) -> None:
    items = [{"key": f"b/{n:04d}", "attrs": {"package": f"p{n}"}} for n in range(300)]
    document = doc(scope(under="b/", fields=["package"], items=items))
    assert counts(run(stored, document))["created"] == 300
    renamed = [{**i, "attrs": {"package": "same"}} for i in items]
    changed = run(stored, doc(scope(under="b/", fields=["package"], items=renamed)))
    assert counts(changed)["updated"] == 300
    # a person writing the same key by hand is refused as before
    taken = {"op": "create", "type": "ex:Installation", "attrs": {"source_key": "inventory/b/0007"}}
    with pytest.raises(GraphCliError) as info:
        stored.plain(taken)
    assert info.value.code == "write.invalid"
