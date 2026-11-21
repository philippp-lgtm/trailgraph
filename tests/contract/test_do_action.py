"""``ActionService.do`` on both backends over the inventory toy ontology (actions plan A5-A7):
the action is expanded and checked on the state under the graph lock, commits one tx and one
intent that name it, and equals the ``apply`` of the change set it expanded to."""

from pathlib import Path
from typing import Any

import pytest
from support.actions import action_service, do, publish_actions, read_dir
from support.fixtures import FIXTURES, compiled_model
from support.graphs import pin_of
from support.services import MemoryCatalog, ServiceEnv

from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.catalog import KineticSlot, sources_hash
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import ActionRef
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.services.actions import ActionService

SOURCES = FIXTURES / "actions" / "inventory" / "valid"
CLEAR_CATEGORIES = """\
schema: graph_cli.action/1
name: clear_categories
summary: Take an item out of all its categories.
use_when: An item should belong to no category any more.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item.
ops:
  - {op: unlink, id: $item, rel: inv:in_category, direction: out, all: true}
examples:
  - request: The crate no longer belongs to any category.
"""


@pytest.fixture(params=("inventory",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    publish_actions(env, {**read_dir(SOURCES), "clear_categories.yaml": CLEAR_CATEGORIES})
    return env


@pytest.fixture
def service(svc: ServiceEnv) -> ActionService:
    return action_service(svc)


def make_item(svc: ServiceEnv, local: str = "x") -> str:
    """An item with its location, as ids."""
    result = svc.create("inv:Item", local)
    return str(next(e.id for e in result.entities if e.local == local))


def make_location(svc: ServiceEnv, local: str = "loc") -> str:
    result = svc.create("inv:Location", local)
    return str(next(e.id for e in result.entities if e.local == local))


def make_categories(svc: ServiceEnv, item: str, count: int) -> list[str]:
    ids = []
    for n in range(count):
        local = f"cat{n}"
        created = svc.apply(
            {"op": "create", "type": "inv:Category", "local": local, "attrs": {"name": f"c{n}"}}
        )
        ids.append(str(next(e.id for e in created.entities if e.local == local)))
        svc.apply({"op": "link", "id": item, "rel": "inv:in_category", "target": ids[-1]})
    return ids


def locations_of(svc: ServiceEnv, item: str) -> list[str]:
    current = svc.snapshot().get(EntityId(parse_id(item)))
    assert current is not None
    return sorted(str(e.target) for e in current.edges if e.rel == "inv:located_in")


def action_ref(service: ActionService, svc: ServiceEnv, name: str) -> ActionRef:
    published = service._reader.published_actions(svc.entry)
    assert published is not None
    entry = next(e for e in published.entries if e.name == name)
    return ActionRef(name, entry.hash)


def test_move_item_commits_one_tx_and_one_intent_naming_the_action(
    svc: ServiceEnv, service: ActionService
) -> None:
    item, target = make_item(svc), make_location(svc)
    events = len(svc.graph_events())
    before = svc.head_seq()
    result = do(service, svc, "move_item", f"item={item}", f"location={target}")
    assert svc.head_seq() == before + 1 and len(svc.graph_events()) == events + 1
    ref = action_ref(service, svc, "move_item")
    assert result.action == ref and result.to_json()["action"] == ref.to_json()
    assert result.write.seq == before + 1 and not result.write.dry_run
    assert locations_of(svc, item) == [target]
    (tx,) = [t for t in svc.txs() if t.seq == before + 1]
    assert tx.request.command == "do" and tx.request.action == ref
    intent = svc.graph_events()[-1]
    assert intent["request"]["action"] == ref.to_json()  # type: ignore[index]
    assert intent["change"]["tx_seq"] == tx.seq  # type: ignore[index]
    ops = [op["op"] for op in tx.changeset["ops"]]  # type: ignore[index, union-attr]
    assert ops == ["unlink", "link"]  # the expansion is what the tx records
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]


def test_a_dry_run_shows_the_expansion_and_writes_nothing(
    svc: ServiceEnv, service: ActionService
) -> None:
    item, target = make_item(svc), make_location(svc)
    old = locations_of(svc, item)
    head, events = svc.head_seq(), len(svc.graph_events())
    result = do(service, svc, "move_item", f"item={item}", f"location={target}", dry_run=True)
    assert result.write.dry_run and result.write.seq is None and result.write.tx_id is None
    (entity,) = result.write.entities
    assert str(entity.id) == item and set(entity.ops) == {"unlink", "link"}
    assert svc.head_seq() == head and len(svc.graph_events()) == events
    assert locations_of(svc, item) == old


def test_do_writes_what_apply_of_the_expanded_change_set_writes(
    backend: str, ontology: str, tmp_path: Path
) -> None:
    """Two identical envs (seeded ids and clock): one runs the action, the other the change
    set it expanded to; the revisions are equal."""
    first = ServiceEnv(backend, ontology, tmp_path / "a")
    second = ServiceEnv(backend, ontology, tmp_path / "b")
    for env in (first, second):
        publish_actions(env, read_dir(SOURCES))
    ids = []
    for env in (first, second):
        item, target = make_item(env), make_location(env)
        ids.append((item, target))
    assert ids[0] == ids[1]
    item, target = ids[0]
    service = action_service(first)
    do(service, first, "move_item", f"item={item}", f"location={target}")
    expanded = first.txs()[-1].changeset
    ops = [dict(op) for op in expanded["ops"]]  # type: ignore[union-attr, arg-type]
    second.apply(*ops)
    mine, theirs = first.txs()[-1], second.txs()[-1]
    assert mine.changeset == theirs.changeset and mine.changeset_hash == theirs.changeset_hash
    assert [r.content_hash for r in mine.revisions] == [r.content_hash for r in theirs.revisions]
    assert [(r.id, r.rev) for r in mine.revisions] == [(r.id, r.rev) for r in theirs.revisions]
    assert mine.request.command == "do" and theirs.request.command == "apply"


def test_a_precondition_that_became_false_after_the_dry_run_refuses_the_write(
    svc: ServiceEnv, service: ActionService
) -> None:
    item = make_item(svc)
    do(service, svc, "retire_item", f"item={item}", dry_run=True, reason="scrapped")
    svc.apply({"op": "transition", "id": item, "to": "in_stock"})  # a state change in between
    svc.apply({"op": "transition", "id": item, "to": "retired"}, reason="by hand")
    head = svc.head_seq()
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "retire_item", f"item={item}", reason="scrapped")
    error = caught.value
    assert error.code == "action.precondition_failed"
    failed: Any = error.details["failed"]
    assert [f["precondition"] for f in failed] == ["not_retired"]
    assert svc.head_seq() == head
    refusal = svc.graph_events()[-1]
    assert refusal["result"]["status"] == "invalid"  # type: ignore[index]
    assert refusal["result"]["code"] == "action.precondition_failed"  # type: ignore[index]
    assert refusal["request"]["action"]["name"] == "retire_item"  # type: ignore[index]
    assert refusal["target"]["entities"] == [item]  # type: ignore[index]
    assert refusal["reason"] == "scrapped"
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]


def test_a_dry_run_refusal_is_not_audited(svc: ServiceEnv, service: ActionService) -> None:
    item = make_item(svc)
    svc.apply({"op": "transition", "id": item, "to": "retired"}, reason="by hand")
    events = len(svc.graph_events()) + len(svc.installation_events())
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "retire_item", f"item={item}", dry_run=True, reason="scrapped")
    assert caught.value.code == "action.precondition_failed"
    assert len(svc.graph_events()) + len(svc.installation_events()) == events


@pytest.mark.parametrize("count", [0, 1, 3])
def test_unlink_all_removes_every_edge_there_is(
    svc: ServiceEnv, service: ActionService, count: int
) -> None:
    item = make_item(svc)
    make_categories(svc, item, count)
    head = svc.head_seq()
    if count == 0:  # only an `all` unlink and no edge: nothing would change
        events = len(svc.graph_events())
        for dry_run in (True, False):
            with pytest.raises(GraphCliError) as caught:
                do(service, svc, "clear_categories", f"item={item}", dry_run=dry_run)
            assert caught.value.code == "action.nothing_to_do"
        assert svc.head_seq() == head and len(svc.graph_events()) == events  # not audited
        return
    dry = do(service, svc, "clear_categories", f"item={item}", dry_run=True)
    assert dry.write.seq is None and svc.head_seq() == head
    result = do(service, svc, "clear_categories", f"item={item}")
    assert result.write.seq == head + 1
    (tx,) = [t for t in svc.txs() if t.seq == head + 1]
    assert [op["op"] for op in tx.changeset["ops"]] == ["unlink"] * count  # type: ignore[index, union-attr]
    current = svc.snapshot().get(EntityId(parse_id(item)))
    assert current is not None and not [e for e in current.edges if e.rel == "inv:in_category"]


def test_a_replay_with_the_same_key_returns_the_recorded_result(
    svc: ServiceEnv, service: ActionService
) -> None:
    item, target, other = make_item(svc), make_location(svc, "a"), make_location(svc, "b")
    first = do(
        service, svc, "move_item", f"item={item}", f"location={target}", idempotency_key="k-1"
    )
    head, events = svc.head_seq(), len(svc.graph_events())
    # the state changed since: expanding again would unlink another edge
    svc.apply(
        {"op": "unlink", "id": item, "rel": "inv:located_in", "target": target},
        {"op": "link", "id": item, "rel": "inv:located_in", "target": other},
    )
    head, events = svc.head_seq(), len(svc.graph_events())
    again = do(
        service, svc, "move_item", f"item={item}", f"location={target}", idempotency_key="k-1"
    )
    assert again.write.idempotent_replay and again.write.tx_id == first.write.tx_id
    assert again.write.seq == first.write.seq and again.action == first.action
    assert svc.head_seq() == head and len(svc.graph_events()) == events
    assert locations_of(svc, item) == [other]  # nothing was done again
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={other}", idempotency_key="k-1")
    assert caught.value.code == "conflict.idempotency_key_reuse"
    assert svc.graph_events()[-1]["result"]["status"] == "conflict"  # type: ignore[index]
    assert svc.head_seq() == head


def test_a_key_of_a_change_set_is_not_matched_by_an_action_and_back(
    svc: ServiceEnv, service: ActionService
) -> None:
    item, target = make_item(svc), make_location(svc)
    svc.apply({"op": "update", "id": item, "set": {"name": "other"}}, idempotency_key="k-2")
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={target}", idempotency_key="k-2")
    assert caught.value.code == "conflict.idempotency_key_reuse"
    do(service, svc, "move_item", f"item={item}", f"location={target}", idempotency_key="k-3")
    error = svc.refuse({"op": "update", "id": item, "set": {"name": "x"}}, idempotency_key="k-3")
    assert error.code == "conflict.idempotency_key_reuse"


def test_the_reason_is_recorded_and_a_required_one_is_asked_for(
    svc: ServiceEnv, service: ActionService
) -> None:
    item = make_item(svc)
    head = svc.head_seq()
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "retire_item", f"item={item}")
    assert caught.value.code == "action.reason_required" and svc.head_seq() == head
    do(service, svc, "retire_item", f"item={item}", reason="scrapped")
    assert svc.txs()[-1].reason == "scrapped"


class Source:
    """A policy source with one fixed policy."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def policy_with(*rules: dict[str, JsonValue], obligations: list[JsonValue] | None = None) -> Source:
    document = {
        **BUILTIN_POLICY,
        "rules": [*BUILTIN_POLICY["rules"], *rules],  # type: ignore[misc]
        "obligations": obligations or [],
    }
    return Source(policy_set(document))


def test_an_action_the_actor_may_not_run_is_a_recorded_denial(svc: ServiceEnv) -> None:
    """The policy denies the unlink of items: ``move_item`` needs it. The caller sees what the
    action names, so the call is ``access.denied`` naming the right (recorded like any denial),
    while a name that is not an action is not found, with suggestions of allowed actions only."""
    item, target = make_item(svc), make_location(svc)
    deny = {
        "id": "no_unlink_item",
        "effect": "deny",
        "action": ["write.unlink"],
        "resource": f"graph:{svc.entry.id}/type:inv:Item",
    }
    service = action_service(svc, policy_with(deny))
    events = len(svc.installation_events())
    head = svc.head_seq()
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={target}")
    assert caught.value.code == "access.denied"
    assert caught.value.details["action"] == "write.unlink"
    assert caught.value.details["resource"] == f"graph:{svc.entry.id}/type:inv:Item"
    assert caught.value.details["rule"] == "no_unlink_item"
    assert len(svc.installation_events()) == events + 1
    assert svc.installation_events()[-1]["result"]["status"] == "denied"  # type: ignore[index]
    assert svc.head_seq() == head  # nothing was written
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "moove_item", f"item={item}", f"location={target}")
    assert caught.value.code == "action.not_found"
    assert "move_item" not in caught.value.details["did_you_mean"]  # type: ignore[operator]
    do(service, svc, "retire_item", f"item={item}", reason="scrapped")  # the allowed one runs


def test_a_graph_the_actor_cannot_see_does_not_exist(svc: ServiceEnv) -> None:
    deny = {"id": "no_graph", "effect": "deny", "action": ["read.list"], "resource": "graph:*"}
    service = action_service(svc, policy_with(deny))
    item, target = make_item(svc), make_location(svc)
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={target}")
    assert caught.value.code == "graph.not_found"
    assert svc.installation_events()[-1]["result"]["status"] == "denied"  # type: ignore[index]


def test_an_obligation_the_expansion_breaks_refuses_the_whole_action(svc: ServiceEnv) -> None:
    """``max_batch`` counts the ops of the expansion, which only the write knows."""
    item, target = make_item(svc), make_location(svc)
    limit: list[JsonValue] = [{"action": ["write.*"], "max_batch": 1}]
    service = action_service(svc, policy_with(obligations=limit))
    head = svc.head_seq()
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={target}")
    assert caught.value.code == "access.batch_too_large"
    assert svc.head_seq() == head and locations_of(svc, item) != [target]
    denial = svc.installation_events()[-1]
    assert denial["result"]["status"] == "denied"  # type: ignore[index]
    assert denial["request"]["action"]["name"] == "move_item"  # type: ignore[index]
    dry = None
    with pytest.raises(GraphCliError):  # a dry run is refused alike, and not audited
        count = len(svc.installation_events())
        dry = do(service, svc, "move_item", f"item={item}", f"location={target}", dry_run=True)
    assert dry is None and len(svc.installation_events()) == count


def test_a_reference_of_the_wrong_class_is_refused_with_the_parameter(
    svc: ServiceEnv, service: ActionService
) -> None:
    item, target = make_item(svc), make_location(svc)
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={target}", f"location={target}")
    error = caught.value
    assert error.code == "action.parameter_type"
    assert error.details == {
        "action": "move_item",
        "parameter": "item",
        "expected": "inv:Item",
        "found": "inv:Location",
    }
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", "location=0123456789abcdef")
    assert caught.value.code == "entity.not_found"
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}")
    assert caught.value.code == "action.parameter_missing"
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "move_item", f"item={item}", f"location={target}", "x=1")
    assert caught.value.code == "action.parameter_unknown"


RENAME_ITEM = """\
schema: graph_cli.action/1
name: rename_item
summary: Give an item another name.
use_when: An item is renamed.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item.
  new_name:
    type: string
    summary: The new name.
ops:
  - {op: update, id: $item, set: {name: $new_name}}
examples:
  - request: The label printer is now called the badge printer.
"""
DEPRECATED_MOVE = """\
schema: graph_cli.action/1
name: relocate_item
summary: Store an item in another location (old name).
use_when: An item is moved to another place; use move_item instead.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item that moves.
  location:
    type: ref
    class: inv:Location
    summary: The location the item moves to.
ops:
  - {op: unlink, id: $item, rel: inv:located_in, direction: out, all: true}
  - {op: link, id: $item, rel: inv:located_in, target: $location}
examples:
  - request: Put the label printer into the second storage room.
status: deprecated
replaced_by: move_item
"""


def test_a_deprecated_action_runs_with_a_warning_that_names_its_successor(
    svc: ServiceEnv,
) -> None:
    publish_actions(svc, {**read_dir(SOURCES), "relocate_item.yaml": DEPRECATED_MOVE})
    item, target = make_item(svc), make_location(svc)
    result = do(action_service(svc), svc, "relocate_item", f"item={item}", f"location={target}")
    (warning,) = result.warnings
    assert (warning.code, warning.details["replaced_by"]) == ("action.deprecated", "move_item")
    assert locations_of(svc, item) == [target]


def test_an_action_that_no_longer_fits_the_ontology_is_stale(tmp_path: Path) -> None:
    """Published against 1.0.0, run on a graph pinned at 2.0.0, which renames ``name``: the
    action that wrote it is refused with the cause, the one that still fits runs."""
    older = compiled_model("inventory", "1.0.0")
    env = ServiceEnv("memory", "inventory", tmp_path / "root", version="2.0.0")
    sources = {**read_dir(SOURCES), "rename_item.yaml": RENAME_ITEM}
    compiled, problems = compile_actions(sources, older)
    assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    assert isinstance(env.catalog, MemoryCatalog)
    env.catalog.actions[env.entry.id] = KineticSlot(
        sources_hash(files), hash_value(compiled), pin_of(older), compiled, {}, kind="actions"
    )
    service = action_service(env)
    item = make_item(env)
    with pytest.raises(GraphCliError) as caught:
        do(service, env, "rename_item", f"item={item}", "new_name=Other")
    error = caught.value
    assert error.code == "action.stale" and error.details["action"] == "rename_item"
    assert error.details["cause"] == "action.unknown_attribute"
    assert str(error.details["hash"]).startswith("sha256:")
    assert env.head_seq() == 1
    do(service, env, "retire_item", f"item={item}", reason="scrapped")  # still fits
    assert env.head_seq() == 2


def test_a_graph_without_actions_has_no_action(tmp_path: Path) -> None:
    env = ServiceEnv("memory", "inventory", tmp_path / "root")
    with pytest.raises(GraphCliError) as caught:
        do(action_service(env), env, "move_item")
    assert (caught.value.code, caught.value.details["did_you_mean"]) == ("action.not_found", [])
