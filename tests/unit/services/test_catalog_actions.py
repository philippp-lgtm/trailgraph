"""A catalog caller runs an action through its grant (catalog callers plan CA4, step 3): the
entry path of ``WriteService._authorize`` (hand rights first, then ``write.action`` on the action's
entry resource and every op deny-only), decided again under the lock after a policy refresh and in
a dry run, the deny rules that still refuse the whole action, the obligations counted once, what
the audit records, and the listing and shown obligations of such a caller.

Over the inventory toy ontology with four actions. The roles are server grants: ``closer`` is
catalog-only (``reads`` of the three classes), ``stocker`` a reader with ``catalog.actions``,
``writer`` writes by hand, ``plain`` reads, ``blind`` is catalog-only with ``reads`` that leave out
the items.
"""

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from support.actions import action_service, publish_actions
from support.catalog_policies import Fixed
from support.company_server import CATALOG_READS
from support.services import ServiceEnv
from support.txbuilder import actor

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.effective import entry_write_obligations
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.actions import ActionService
from graph_cli.services.catalog_access import allowed_entries
from graph_cli.services.read import ReadService

GRAPH = "example_graph"

MARK_DAMAGED = """\
schema: graph_cli.action/1
name: mark_damaged
summary: Record that an item is damaged.
use_when: An inspection finds an item damaged.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item.
ops:
  - {op: update, id: $item, set: {condition: damaged}}
examples:
  - request: The chair came out of the inspection damaged.
"""
MOVE_ITEM = """\
schema: graph_cli.action/1
name: move_item
summary: Store an item in another location.
use_when: An item is moved, relocated or put away somewhere else.
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
"""
RETIRE_ITEM = """\
schema: graph_cli.action/1
name: retire_item
summary: Take an item out of service for good.
use_when: An item is scrapped, written off or taken out of service.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item to retire.
ops:
  - {op: transition, id: $item, to: retired}
reason: required
examples:
  - request: The old label printer is scrapped.
"""
STOCK_ITEMS = """\
schema: graph_cli.action/1
name: stock_items
summary: Add two items to a location.
use_when: Two items of one kind arrive and are put in a location.
params:
  location:
    type: ref
    class: inv:Location
    summary: The location the items go to.
ops:
  - op: create
    type: inv:Item
    local: first
    attrs: {name: Crate}
    edges:
      - {rel: inv:located_in, target: $location}
  - op: create
    type: inv:Item
    local: second
    attrs: {name: Crate}
    edges:
      - {rel: inv:located_in, target: $location}
examples:
  - request: Two crates arrive at the dock.
"""
# a compiled policy asks for a reason when something is superseded or retracted
DEFAULT_REASONS = {"write.retract", "write.supersede"}
GRANT = "grant.1.actions"  # the closer grant is the first of the installation (1-based rule ids)
DETACH_PARTS = """\
schema: graph_cli.action/1
name: detach_parts
summary: Take the parts off an item.
use_when: An item is taken apart and its parts become items of their own.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item whose parts are detached.
ops:
  - {op: unlink, id: $item, rel: inv:part_of, direction: in, all: true}
examples:
  - request: The crate is taken apart.
"""
ARCHIVE_ITEM = """\
schema: graph_cli.action/1
name: archive_item
summary: Mark an item that has no parts as damaged.
use_when: A whole item is found damaged and nothing is attached to it.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item.
preconditions:
  - id: no_parts
    on: item
    summary: The item has no parts.
    require:
      - count: {rel: part_of, direction: in}
        equals_number: 0
ops:
  - {op: update, id: $item, set: {condition: damaged}}
examples:
  - request: The empty crate is damaged.
"""
SOURCES = {
    "archive_item.yaml": ARCHIVE_ITEM,
    "detach_parts.yaml": DETACH_PARTS,
    "mark_damaged.yaml": MARK_DAMAGED,
    "move_item.yaml": MOVE_ITEM,
    "retire_item.yaml": RETIRE_ITEM,
    "stock_items.yaml": STOCK_ITEMS,
}
ACTIONS = "[archive_item, detach_parts, mark_damaged, move_item, retire_item, stock_items]"
ITEMS = "[inv:Item, inv:Location, inv:Category]"

INSTALLATION = f"""\
schema: graph_cli.policy/1
id: installation
roles:
  closer: {{description: "Catalog only: four actions"}}
  stocker: {{description: "A reader that also runs actions through its grant"}}
  writer: {{description: "Writes by hand"}}
  plain: {{description: "Reads"}}
  blind: {{description: "Catalog only, no item type among its reads"}}
  locator: {{description: "Catalog only, stock_items, no item type among its reads"}}
grants:
  - entrypoint: server
    principal: {{role: closer}}
    graphs: [{GRAPH}]
    level: catalog
    catalog: {{actions: {ACTIONS}}}
    reads: {ITEMS}
  - entrypoint: server
    principal: {{role: stocker}}
    graphs: [{GRAPH}]
    level: read
    catalog: {{actions: {ACTIONS}}}
  - {{entrypoint: server, principal: {{role: writer}}, graphs: [{GRAPH}], level: write}}
  - {{entrypoint: server, principal: {{role: plain}}, graphs: [{GRAPH}], level: read}}
  - entrypoint: server
    principal: {{role: blind}}
    graphs: [{GRAPH}]
    level: catalog
    catalog: {{actions: [move_item]}}
    reads: [inv:Location]
  - entrypoint: server
    principal: {{role: locator}}
    graphs: [{GRAPH}]
    level: catalog
    catalog: {{actions: [stock_items]}}
    reads: [inv:Location]
scopes:
  frozen: {{{GRAPH}: read}}
  open: {{{GRAPH}: write}}
no_project: {{"*": write}}
"""
SCOPES_FROZEN = "frozen"


def server_actor(
    *roles: str, project: str | None = None, agent_type: str | None = None
) -> ActorContext:
    base = actor()
    return dataclasses.replace(
        base,
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
        project=Facet(Assurance.VERIFIED, id=project, name=project, source="token")
        if project
        else base.project,
        agent=Facet(Assurance.ASSERTED, id="agent-1", type=agent_type, source="test")
        if agent_type
        else base.agent,
    )


CLOSER = server_actor("closer")
STOCKER = server_actor("stocker")
WRITER = server_actor("writer")
PLAIN = server_actor("plain")
BLIND = server_actor("blind")
LOCATOR = server_actor("locator")


def graph_layer(env: ServiceEnv, text: str) -> Mapping[str, JsonValue]:
    return compile_policy(
        text.encode(),
        file="g.yaml",
        graphs={GRAPH: env.entry.id},
        graph=env.entry.id,
        roles=frozenset(),
    ).document


def installation_document(env: ServiceEnv, text: str = INSTALLATION) -> Mapping[str, JsonValue]:
    return compile_policy(text.encode(), file="i.yaml", graphs={GRAPH: env.entry.id}).document


class Swapping:
    """A policy source that answers ``before`` until the write service refreshes it (it does so
    after taking the graph lock), then ``after``: a policy applied while a write waited."""

    def __init__(self, before: PolicySet, after: PolicySet) -> None:
        self.sets = (before, after)
        self.refreshed = False

    def current(self, graph: object) -> PolicySet:
        return self.sets[1 if self.refreshed else 0]

    def refresh(self) -> None:
        self.refreshed = True


class Scene:
    """The inventory with a location, an item and the four actions, served under a policy."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        graph: str | None = None,
        installation: str = INSTALLATION,
        source: Any = None,
    ) -> None:
        self.env = ServiceEnv("memory", "inventory", tmp_path / "root")
        publish_actions(self.env, SOURCES)
        made = self.env.apply(
            *self.env.factory.create("inv:Location", "shelf"),
            *self.env.factory.create("inv:Item", "item"),
        )
        self.ids = {e.local: str(e.id) for e in made.entities if e.local}
        self.install(graph=graph, installation=installation, source=source)

    def install(
        self, *, graph: str | None = None, installation: str = INSTALLATION, source: Any = None
    ) -> None:
        document = installation_document(self.env, installation)
        layer = None if graph is None else graph_layer(self.env, graph)
        self.policy = Fixed(policy_set(document, layer))  # type: ignore[arg-type]
        self.source = source or self.policy
        self.service: ActionService = action_service(self.env, self.source)
        self.reader: ReadService = self.service._reader  # type: ignore[attr-defined]

    def do(
        self,
        who: ActorContext,
        name: str,
        *params: str,
        dry_run: bool = False,
        reason: str | None = "test",
        key: str | None = None,
    ) -> Any:
        pairs = [tuple(p.split("=", 1)) for p in params]
        return self.service.do(
            who,
            self.env.entry,
            name,
            pairs,  # type: ignore[arg-type]
            dry_run=dry_run,
            reason=reason,
            idempotency_key=key,
        )

    def refused(self, who: ActorContext, name: str, *params: str, **kw: Any) -> GraphCliError:
        with pytest.raises(GraphCliError) as caught:
            self.do(who, name, *params, **kw)
        return caught.value

    @property
    def writer(self) -> Any:
        """The write service under the scene's policy (``scene.env.service`` has another)."""
        return self.service._writer  # type: ignore[attr-defined]

    def item(self) -> str:
        return f"item={self.ids['item']}"

    def location(self) -> str:
        return f"location={self.ids['shelf']}"

    def events(self) -> list[Mapping[str, JsonValue]]:
        return [*self.env.graph_events(), *self.env.installation_events()]

    def newest(self, since: int = 0) -> Mapping[str, Any]:
        found = self.events()[since:]
        assert found, "no event was recorded"
        return found[-1]

    def count(self) -> int:
        return len(self.events())

    def last_ok(self) -> Mapping[str, Any]:
        """The newest event of a committed write."""
        return [e for e in self.env.graph_events() if e["result"]["status"] == "ok"][-1]

    def condition(self) -> object:
        revision = self.env.snapshot().get(EntityId(parse_id(self.ids["item"])))
        assert revision is not None
        return revision.attrs.get("condition")


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


def decisions(event: Mapping[str, Any]) -> set[tuple[str, str, str, str]]:
    return {
        (d["action"], d["resource"].split("/", 1)[-1], d["effect"], d["rule"])
        for d in event["authz"]["decisions"]
    }


# ---- the entry path ---------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_an_action_writes_through_its_grant_what_the_caller_could_not_write_by_hand(
    scene: Scene,
) -> None:
    assert scene.condition() is None
    # by hand the token writes nothing: the primitives are refused
    update = scene.env.request(
        {"op": "update", "id": scene.ids["item"], "set": {"condition": "damaged"}},
        actor=CLOSER,
        reason="test",
    )
    with pytest.raises(GraphCliError) as by_hand:
        scene.writer.apply(update)
    assert by_hand.value.code == "access.denied"
    assert scene.condition() is None
    done = scene.do(CLOSER, "mark_damaged", scene.item())
    assert done.write.tx_id is not None
    assert scene.condition() == "damaged"


def test_the_event_of_an_allowed_action_names_the_grant_of_every_decision(scene: Scene) -> None:
    done = scene.do(CLOSER, "mark_damaged", scene.item())
    event = scene.last_ok()
    got = decisions(event)
    graph = "action:mark_damaged"
    # the entry decision and the op decisions, all naming the rule of the catalog grant, and no
    # decision of a by-hand probe that failed (there is none that denies)
    assert got == {
        ("write.action", graph, "allow", GRANT),
        ("write.update", "type:inv:Item", "allow", GRANT),
    }, got
    assert all(d["effect"] == "allow" for d in event["authz"]["decisions"])
    assert event["request"]["action"]["name"] == "mark_damaged"
    assert set(event["request"]["action"]) == {"name", "hash"}
    assert event["actions"] == ["write.action", "write.update"]
    assert done.action.name == "mark_damaged"
    assert not scene.env.verify().findings  # the chain verifies with the new decisions


def test_an_action_with_nothing_to_do_is_authorized_by_its_needs_through_the_grant(
    scene: Scene,
) -> None:
    """An expansion without an op is authorized by the needs the action declares: the caller of a
    grant is told there is nothing to do, not that it may not write."""
    since = scene.count()
    error = scene.refused(CLOSER, "detach_parts", scene.item())
    assert error.code == "action.nothing_to_do"
    assert scene.count() == since  # nothing to do: no event
    error = scene.refused(PLAIN, "detach_parts", scene.item())
    assert error.code == "access.denied"


@pytest.mark.doc_claim("security_model.catalog_action_residual")
def test_a_failed_precondition_shows_what_it_counted_to_a_caller_of_a_grant(scene: Scene) -> None:
    """The refusal of a precondition quotes the values of its aggregates, as it does for a caller
    with write rights: a residual leak, accepted and documented, for a token that could not read
    the item by hand."""
    part = scene.env.factory.create("inv:Item", "part")
    part[-1].setdefault("edges", []).append({"rel": "inv:part_of", "target": scene.ids["item"]})
    scene.env.apply(*part)
    error = scene.refused(CLOSER, "archive_item", scene.item())
    assert error.code == "action.precondition_failed"
    (failed,) = error.details["failed"]  # type: ignore[misc]
    assert failed["precondition"] == "no_parts" and failed["entity"] == scene.ids["item"]  # type: ignore[index]
    assert failed["actual"] not in (None, {}, [])  # type: ignore[index]
    assert scene.condition() is None


def test_a_multi_op_action_decides_each_op_through_the_grant(scene: Scene) -> None:
    scene.do(CLOSER, "move_item", scene.item(), scene.location())
    event = scene.last_ok()
    assert {d[:2] for d in decisions(event) if d[3] == GRANT} == {
        ("write.action", "action:move_item"),
        ("write.link", "type:inv:Item"),
        ("write.unlink", "type:inv:Item"),
    }
    done = scene.do(CLOSER, "stock_items", scene.location())
    assert len(done.write.entities) == 2


def test_the_hand_rights_come_first_and_existing_callers_keep_their_events(scene: Scene) -> None:
    scene.do(WRITER, "mark_damaged", scene.item())
    event = scene.last_ok()
    got = decisions(event)
    assert not any(d[0] == "write.action" for d in got)  # the by-hand path allowed: no entry path
    assert {d[0] for d in got} == {"write.update"}
    # a reader without a grant of the action is refused as before: one denial, no entry decision
    since = scene.count()
    refused = scene.refused(PLAIN, "mark_damaged", scene.item())
    assert refused.code == "access.denied"
    assert refused.details["action"] == "write.update"
    event = scene.newest(since)
    assert event["result"]["status"] == "denied"
    assert not any(d[0] == "write.action" for d in decisions(event))


def test_a_caller_with_hand_rights_that_a_rule_denies_is_answered_as_before(tmp_path: Path) -> None:
    """The entry path only lifts a missing allow: a writer by hand whom a rule refuses gets the
    refusal of that rule, with the decisions of the by-hand path and no entry decision."""
    scene = Scene(tmp_path, graph=graph_deny('types:\n  "inv:Item": read\n'))
    since = scene.count()
    refused = scene.refused(WRITER, "mark_damaged", scene.item())
    assert refused.code == "access.denied"
    assert (refused.details["action"], refused.details["resource"].split("/", 1)[1]) == (
        "write.update",
        "type:inv:Item",
    )
    event = scene.newest(since)
    assert not any(d[0] == "write.action" for d in decisions(event))
    assert {d[3] for d in decisions(event) if d[2] == "deny"} == {"type.inv:Item"}


def test_an_app_caller_that_a_rule_denies_is_answered_as_before(tmp_path: Path) -> None:
    """The built-in policy lets the app write (``write.*`` on every graph resource, the entry of an
    action included): a graph rule that refuses a type is answered by the by-hand decisions alone,
    never by an entry decision next to them (asked of the write service itself: the listing would
    already call the action not allowed)."""
    from graph_cli.access_control.policy import BUILTIN_POLICY

    scene = Scene(tmp_path)
    layer = graph_layer(scene.env, graph_deny('types:\n  "inv:Item": read\n'))
    scene.install(source=Fixed(policy_set(BUILTIN_POLICY, layer)))  # type: ignore[arg-type]
    since = scene.count()
    who = actor()
    with pytest.raises(GraphCliError) as refused:
        scene.writer.apply(plan_request(scene, who, "mark_damaged", item=scene.ids["item"]))
    assert refused.value.code == "access.denied"
    event = scene.newest(since)
    assert not any(d[0] == "write.action" for d in decisions(event))
    assert {d[3] for d in decisions(event) if d[2] == "deny"} == {"type.inv:Item"}


def test_a_need_without_options_is_decided_deny_only_through_the_grant(scene: Scene) -> None:
    """``need_decisions`` of a need that names no option (the declared class alone): through the
    entry decision it asks ``authorize_entry``, so a rule that refuses the type still refuses it,
    and where none does the grant has no allow rule to lend."""
    from graph_cli.access_control.authorize import authorize
    from graph_cli.kinetic.action_needs import NeedOptions
    from graph_cli.services.action_rights import need_decisions

    layer = graph_layer(scene.env, graph_deny('types:\n  "inv:Item": read\n'))
    ctx = AuthzContext(op_count=1, has_reason=True, dry_run=False)
    need = NeedOptions("write.update", "inv:Item", ())
    for policy, allowed in (
        (scene.policy.current(scene.env.entry.id), True),
        (policy_set(installation_document(scene.env), layer), False),  # type: ignore[arg-type]
    ):
        entered = authorize(
            CLOSER,
            "write.action",
            f"graph:{scene.env.entry.id}/action:mark_damaged",
            ctx,
            policy,
        )
        by_hand = need_decisions(CLOSER, scene.env.entry.id, need, ctx, policy, frozenset())
        through = need_decisions(
            CLOSER, scene.env.entry.id, need, ctx, policy, frozenset(), through=entered
        )
        assert not by_hand.met  # the token has no write right of its own
        assert through.met is allowed
        if not allowed:
            assert through.decisions[0].rule_id == "type.inv:Item"


def test_a_reader_with_catalog_actions_writes_through_these_actions_only(scene: Scene) -> None:
    scene.do(STOCKER, "mark_damaged", scene.item())
    assert scene.condition() == "damaged"
    # the primitives and the other things it reads stay as they were
    update = scene.env.request(
        {"op": "update", "id": scene.ids["item"], "set": {"condition": "good"}},
        actor=STOCKER,
        reason="test",
    )
    with pytest.raises(GraphCliError) as by_hand:
        scene.writer.apply(update)
    assert by_hand.value.code == "access.denied"
    assert scene.condition() == "damaged"


def test_a_grant_of_one_action_carries_no_other_action(tmp_path: Path) -> None:
    own = INSTALLATION.replace(
        f"catalog: {{actions: {ACTIONS}}}", "catalog: {actions: [move_item]}"
    )
    scene = Scene(tmp_path, installation=own)
    scene.do(STOCKER, "move_item", scene.item(), scene.location())
    # the other action is listed as before for a reader (not allowed) and is refused
    refused = scene.refused(STOCKER, "mark_damaged", scene.item())
    assert refused.code == "access.denied" and refused.details["action"] == "write.update"
    # for a catalog-only caller an unnamed action does not exist
    assert scene.refused(CLOSER, "retire_item", scene.item()).code == "action.not_found"


def test_a_rule_that_allows_the_entry_without_naming_the_action_is_no_grant(tmp_path: Path) -> None:
    """The write service takes the entry path only for an action the caller's grants name, as the
    listing does: a caller whose rules allow ``write.action`` by a glob (here a hand-made rule on
    ``graph:<id>*``) and whose grants name no action is decided by its own rights alone."""
    scene = Scene(tmp_path)
    document = dict(installation_document(scene.env))
    document["rules"] = [
        *document["rules"],  # type: ignore[misc]
        {
            "id": "odd.entry_glob",
            "effect": "allow",
            "principal": {"entrypoint": "server", "role": "odd"},
            "action": ["write.action", "read.list", "read.describe", "read.get"],
            "resource": f"graph:{scene.env.entry.id}*",
        },
    ]
    scene.install(source=Fixed(policy_set(document)))  # type: ignore[arg-type]
    odd = server_actor("odd")
    policy = scene.source.current(scene.env.entry.id)
    entry = f"graph:{scene.env.entry.id}/action:mark_damaged"
    from graph_cli.access_control.authorize import authorize

    ctx = AuthzContext(op_count=1, has_reason=True, dry_run=False)
    assert (
        authorize(odd, "write.action", entry, ctx, policy).effect == "allow"
    )  # the glob allows it
    from graph_cli.access_control.effective import catalog_grants

    assert catalog_grants(odd, scene.env.entry.id, policy).actions == ()  # and names nothing
    since = scene.count()
    with pytest.raises(GraphCliError) as refused:
        scene.writer.apply(plan_request(scene, odd, "mark_damaged", item=scene.ids["item"]))
    assert refused.value.code == "access.denied"
    assert scene.condition() is None
    got = decisions(scene.newest(since))
    assert not any(d[0] == "write.action" for d in got)  # no entry decision: the path was not taken
    assert ("write.update", "type:inv:Item", "deny", "default.deny") in got


# ---- deny rules win ---------------------------------------------------------------------


def graph_deny(rule: str) -> str:
    return f"schema: graph_cli.policy/1\nid: graph\n{rule}"


DENY_KINDS: dict[str, tuple[str, str | None, dict[str, str]]] = {
    # name -> (graph layer text, installation replacement or None, actor kwargs)
    "type_limit_read": (graph_deny('types:\n  "inv:Item": read\n'), None, {}),
    "term_deny": (
        graph_deny(
            "deny:\n  - {id: no_condition, action: [write.update], type: [inv:Item],"
            " attribute: [condition]}\n"
        ),
        None,
        {},
    ),
    "installation_deny": (
        "",
        "deny:\n  - {id: no_updates, action: [write.update]}\n",
        {},
    ),
    "deny_write_action": (
        "",
        "deny:\n  - {id: no_actions, action: [write.action]}\n",
        {},
    ),
    "scope_read": ("", None, {"project": SCOPES_FROZEN}),
    "agent_type": (
        "",
        "deny:\n  - {id: no_explore_writes, agent_type: [explore], action: [write.*]}\n",
        {"agent_type": "explore"},
    ),
}


@pytest.mark.parametrize("kind", sorted(DENY_KINDS))
@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_deny_rule_refuses_the_whole_action_through_a_grant(tmp_path: Path, kind: str) -> None:
    graph, extra, who = DENY_KINDS[kind]
    scene = Scene(tmp_path, graph=graph or None, installation=INSTALLATION + (extra or ""))
    caller = server_actor("closer", **who)
    # through ``do``: the listing already says the action is not allowed (a recorded denial)
    since = scene.count()
    refused = scene.refused(caller, "mark_damaged", scene.item())
    assert refused.code == "access.denied", kind
    assert scene.condition() is None  # nothing was written
    got = decisions(scene.newest(since))
    assert any(d[2] == "deny" and d[3] != "default.deny" for d in got), got
    assert not any(d[3] == "default.deny" for d in got), got
    # and in the write service alone, whatever the listing said (the policy may have changed
    # meanwhile): the rule refuses the op, the whole action is refused and no op decision of the
    # grant is among the decisions that allowed anything
    since = scene.count()
    with pytest.raises(GraphCliError) as direct:
        scene.writer.apply(plan_request(scene, caller, "mark_damaged", item=scene.ids["item"]))
    assert direct.value.code == "access.denied", kind
    assert scene.condition() is None
    event = scene.newest(since)
    assert event["result"]["status"] == "denied"
    got = decisions(event)
    assert any(d[2] == "deny" and d[3] != "default.deny" for d in got), got
    assert not any(d[3] == "default.deny" for d in got), got
    if kind == "term_deny":  # the type itself is allowed by default-deny only: the entry path ran
        assert ("write.action", "action:mark_damaged", "allow", GRANT) in got
        assert ("write.update", "type:inv:Item", "allow", GRANT) in got
    # a dry run is refused the same way, unaudited
    before = scene.count()
    request = dataclasses.replace(
        plan_request(scene, caller, "mark_damaged", item=scene.ids["item"]), dry_run=True
    )
    with pytest.raises(GraphCliError) as dry:
        scene.writer.apply(request)
    assert dry.value.code == "access.denied" and scene.count() == before


def test_the_listing_of_a_denied_action_is_not_allowed_naming_the_denied_write(
    tmp_path: Path,
) -> None:
    deny = graph_deny(
        "deny:\n  - {id: no_condition, action: [write.update], type: [inv:Item],"
        " attribute: [condition]}\n"
    )
    scene = Scene(tmp_path, graph=deny)
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    found = {
        c.entry.name: c for c in scene.reader.classified_actions(CLOSER, scene.env.entry, published)
    }
    assert found["mark_damaged"].allowed is False
    assert [str(m) for m in found["mark_damaged"].missing] == [
        "write.update on type:inv:Item/attr:condition"
    ]
    # the other granted actions do not touch the denied attribute
    assert found["move_item"].allowed and found["stock_items"].allowed
    # a refusal of the named action: a recorded denial naming the action and its hash
    since = scene.count()
    refused = scene.refused(CLOSER, "mark_damaged", scene.item())
    assert refused.code == "access.denied"
    event = scene.newest(since)
    assert event["request"]["action"]["name"] == "mark_damaged"
    assert event["request"]["action"]["hash"] == found["mark_damaged"].entry.hash


def test_an_action_never_writes_a_type_the_caller_does_not_see(scene: Scene) -> None:
    # `blind` reads locations only: an action that names an item is hidden from it, as it is from
    # every caller with that read view, granted or not
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    assert scene.reader.classified_actions(BLIND, scene.env.entry, published) == ()
    since = scene.count()
    error = scene.refused(BLIND, "move_item", scene.item(), scene.location())
    assert error.code == "action.not_found"
    assert scene.condition() is None and scene.count() == since  # unknown: nothing is recorded


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_type_the_caller_does_not_see_keeps_its_default_denial(scene: Scene) -> None:
    """``stock_items`` creates items, which ``locator`` does not see: the listing hides the action
    from it and the write service, asked anyway, answers the create as a denial on a resource no
    rule names; the grant of the action never makes the type writable."""
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    assert scene.reader.classified_actions(LOCATOR, scene.env.entry, published) == ()
    since = scene.count()
    with pytest.raises(GraphCliError) as refused:
        scene.writer.apply(plan_request(scene, LOCATOR, "stock_items", location=scene.ids["shelf"]))
    assert refused.value.code == "access.denied"
    event = scene.newest(since)
    assert event["result"]["status"] == "denied"
    got = decisions(event)
    assert ("write.create", "type:inv:Item", "deny", "default.deny") in got, got
    assert not any(d[2] == "allow" and d[0] == "write.create" for d in got)


# ---- the entry path in the write service ---------------------------------------------------


def plan_request(scene: Scene, who: ActorContext, name: str, **bound: str) -> Any:
    """A request for the action's plan built the way ``ActionService`` hands it over, so a test
    can ask the write service alone (a hidden action never gets that far through the service)."""
    from graph_cli.kernel.changeset import ChangeSet
    from graph_cli.kernel.records import ActionRef, RequestMeta
    from graph_cli.services.write import ActionPlan, WriteRequest

    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    entry = next(e for e in published.entries if e.name == name)
    return WriteRequest(
        actor=who,
        graph=str(scene.env.entry.id),
        changeset=ChangeSet(ops=(), reason="test", idempotency_key=None),
        dry_run=False,
        request=RequestMeta.of("do", {"action": name}, ActionRef(name, entry.hash)),
        plan=ActionPlan(entry, bound),  # type: ignore[arg-type]
    )


def test_the_entry_decision_is_made_with_the_context_of_the_request_and_counted_once(
    tmp_path: Path,
) -> None:
    limit = graph_deny("obligations:\n  - {action: [write.*], max_batch: 2}\n")
    scene = Scene(tmp_path, graph=limit)
    # two ops, a limit of two: allowed (the entry decision and each op count the ops once)
    done = scene.do(CLOSER, "stock_items", scene.location())
    assert len(done.write.entities) == 2
    # two ops, a limit of one: refused with the one code of the obligation, on the entry and on
    # the ops alike
    over = Scene(tmp_path / "over", graph=limit.replace("max_batch: 2", "max_batch: 1"))
    since = over.count()
    refused = over.refused(CLOSER, "stock_items", over.location())
    assert refused.code == "access.batch_too_large"
    event = over.newest(since)
    assert {d[3] for d in decisions(event) if d[2] == "deny"} == {"obligation.max_batch"}
    assert ("write.action", "action:stock_items", "deny", "obligation.max_batch") in decisions(
        event
    )
    # a dry run gets the same answer (it predicts the real outcome) and records nothing
    before = over.count()
    dry = over.refused(CLOSER, "stock_items", over.location(), dry_run=True)
    assert dry.code == "access.batch_too_large" and over.count() == before


def test_a_reason_obligation_on_the_writes_is_asked_of_the_action_without_a_reason(
    tmp_path: Path,
) -> None:
    need = graph_deny("obligations:\n  - {action: [write.*], require_reason: true}\n")
    scene = Scene(tmp_path, graph=need)
    since = scene.count()
    refused = scene.refused(CLOSER, "mark_damaged", scene.item(), reason=None)
    assert refused.code == "access.reason_required"  # not the default denial of the first op
    event = scene.newest(since)
    assert event["result"]["status"] == "denied"
    assert (
        "write.action",
        "action:mark_damaged",
        "deny",
        "obligation.require_reason",
    ) in decisions(event)
    done = scene.do(CLOSER, "mark_damaged", scene.item(), reason="inspection")
    assert done.write.tx_id is not None
    # the reason a caller must give is shown: the manifest's input demands it
    obligations = scene.reader.reason_obligations(CLOSER, scene.env.entry)
    assert obligations == {
        "write.action",
        "write.alias",
        "write.create",
        "write.update",
        "write.link",
        "write.unlink",
        "write.transition",
        *DEFAULT_REASONS,
    }
    assert scene.reader.reason_obligations(PLAIN, scene.env.entry) == frozenset()


def test_an_obligation_on_the_entry_action_makes_the_manifest_ask_for_a_reason(
    tmp_path: Path,
) -> None:
    from graph_cli.services.describe_tools import reason_required

    scene = Scene(
        tmp_path,
        graph=graph_deny("obligations:\n  - {action: [write.action], require_reason: true}\n"),
    )
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    entry = next(e for e in published.entries if e.name == "mark_damaged")
    shown = scene.reader.reason_obligations(CLOSER, scene.env.entry)
    assert reason_required(entry, shown)
    # a caller with no grant of an action is shown none of it
    assert not reason_required(entry, scene.reader.reason_obligations(PLAIN, scene.env.entry))


def test_the_shown_obligations_come_from_the_entry_path_for_a_granted_caller(
    tmp_path: Path,
) -> None:
    need = graph_deny("obligations:\n  - {action: [write.update], require_reason: true}\n")
    scene = Scene(tmp_path, graph=need)
    assert (
        scene.reader.reason_obligations(CLOSER, scene.env.entry)
        == {"write.update"} | DEFAULT_REASONS
    )
    # a reader that is granted no action shows nothing: it writes nothing by hand
    assert scene.reader.reason_obligations(PLAIN, scene.env.entry) == frozenset()
    # a writer by hand: as before, from the ladder
    assert (
        scene.reader.reason_obligations(WRITER, scene.env.entry)
        == {"write.update"} | DEFAULT_REASONS
    )
    # a rule that denies a write takes it out of what is shown
    denied = Scene(
        tmp_path / "denied",
        graph=need + "deny:\n  - {id: no_updates, action: [write.update]}\n",
    )
    assert denied.reader.reason_obligations(CLOSER, denied.env.entry) == DEFAULT_REASONS


def test_entry_write_obligations_name_the_entry_action_itself(tmp_path: Path) -> None:
    need = graph_deny("obligations:\n  - {action: [write.action], require_reason: true}\n")
    scene = Scene(tmp_path, graph=need)
    policy = scene.policy.current(scene.env.entry.id)
    from graph_cli.access_control.authorize import authorize

    entered = authorize(
        CLOSER,
        "write.action",
        f"graph:{scene.env.entry.id}/action:mark_damaged",
        AuthzContext(op_count=1, has_reason=True, dry_run=False),
        policy,
    )
    assert entered.effect == "allow"
    found = entry_write_obligations(CLOSER, scene.env.entry.id, entered, policy)
    assert found == {
        "write.action": {"require_reason": True},
        **{a: {"require_reason": True} for a in DEFAULT_REASONS},
    }


MARKED_ACTIONS = {
    "set_email.yaml": """\
schema: graph_cli.action/1
name: set_email
summary: Set the e-mail.
use_when: Set the e-mail of a person.
params:
  who: {type: ref, class: ex:Person, summary: The person.}
  value: {type: string, summary: The address.}
ops:
  - {op: update, id: $who, set: {email: $value}}
examples:
  - request: Set the e-mail.
""",
    "set_name.yaml": """\
schema: graph_cli.action/1
name: set_name
summary: Set the name.
use_when: Rename a person.
params:
  who: {type: ref, class: ex:Person, summary: The person.}
  value: {type: string, summary: The name.}
ops:
  - {op: update, id: $who, set: {name: $value}}
examples:
  - request: Rename the person.
""",
}
MARKED_INSTALLATION = f"""\
schema: graph_cli.policy/1
id: installation
roles:
  closer: {{description: "Catalog only, no marking"}}
grants:
  - entrypoint: server
    principal: {{role: closer}}
    graphs: [{GRAPH}]
    level: catalog
    catalog: {{actions: [set_email, set_name]}}
    reads: [ex:Person, ex:Team]
no_project: {{"*": write}}
"""
# a term rule on the type makes its attributes decided one by one (and so the hidden one a
# decision on a resource no rule names)
MARKED_GRAPH = graph_deny(
    "deny:\n  - {id: no_phone, action: [write.update], type: [ex:Person], attribute: [phone]}\n"
)


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_an_attribute_the_caller_does_not_see_keeps_its_default_denial(tmp_path: Path) -> None:
    """A marked attribute the caller lacks the marking for is decided on a resource no rule names
    (default deny), never asked deny-only: the grant of the action does not make it writable. The
    listing hides such an action; should one reach the write service, it is refused."""
    from support.marked_writes import MODULE, POLICIES, compiled

    env = ServiceEnv("memory", "inventory", tmp_path / "root", model_override=compiled(MODULE))
    holder = compile_policy(
        POLICIES["holder"].encode(), file="h.yaml", graphs={GRAPH: env.entry.id}
    ).document
    seeding = env.make_service(Fixed(policy_set(holder)))  # type: ignore[arg-type]
    made = seeding.apply(
        env.request(
            {"op": "create", "type": "ex:Team", "local": "t", "attrs": {"name": "Core"}},
            {
                "op": "create",
                "type": "ex:Person",
                "local": "p",
                "attrs": {
                    "name": "Ada",
                    "phone": "+1",
                    "email": "ada@private.example",
                    "nicknames": ["Al"],
                    "grade": "mid",
                    "code": "C-1",
                    "hours": 30,
                },
                "edges": [{"rel": "ex:member_of", "target": "@t"}],
            },
            reason="seed",
        )
    )
    person = str(next(e.id for e in made.entities if e.local == "p"))
    publish_actions(env, MARKED_ACTIONS)
    graphs = {GRAPH: env.entry.id}
    document = compile_policy(MARKED_INSTALLATION.encode(), file="i.yaml", graphs=graphs).document
    layer = graph_layer(env, MARKED_GRAPH)
    source = Fixed(policy_set(document, layer))  # type: ignore[arg-type]
    service = action_service(env, source)
    reader = service._reader  # type: ignore[attr-defined]
    writer = service._writer  # type: ignore[attr-defined]
    published = reader.published_actions(env.entry)
    assert published is not None
    seen = {
        c.entry.name: c.allowed for c in reader.classified_actions(CLOSER, env.entry, published)
    }
    assert seen == {"set_name": True}  # set_email names a hidden attribute: it does not exist
    with pytest.raises(GraphCliError) as hidden:
        service.do(
            CLOSER,
            env.entry,
            "set_email",
            [("who", person), ("value", "x@y")],  # type: ignore[arg-type]
            dry_run=False,
            reason="t",
            idempotency_key=None,
        )
    assert hidden.value.code == "action.not_found"

    def plan(name: str, value: str, *, dry_run: bool = False) -> Any:
        from graph_cli.kernel.changeset import ChangeSet
        from graph_cli.kernel.records import ActionRef, RequestMeta
        from graph_cli.services.write import ActionPlan, WriteRequest

        entry = next(e for e in published.entries if e.name == name)
        return WriteRequest(
            actor=CLOSER,
            graph=str(env.entry.id),
            changeset=ChangeSet(ops=(), reason="test", idempotency_key=None),
            dry_run=dry_run,
            request=RequestMeta.of("do", {"action": name}, ActionRef(name, entry.hash)),
            plan=ActionPlan(entry, {"who": person, "value": value}),
        )

    for dry_run in (False, True):
        with pytest.raises(GraphCliError) as refused:
            writer.apply(plan("set_email", "x@y", dry_run=dry_run))
        assert refused.value.code == "access.denied", dry_run  # not write.invalid: not allowed
    event = env.installation_events()[-1]
    got = decisions(event)
    assert ("write.update", "type:ex:Person/attr:email", "deny", "default.deny") in got, got
    # the visible attribute goes through the grant, attribute by attribute
    done = writer.apply(plan("set_name", "Ada L"))
    assert done.tx_id is not None
    event = env.graph_events()[-1]
    assert {d[:3] for d in decisions(event)} >= {
        ("write.action", "action:set_name", "allow"),
        ("write.update", "type:ex:Person", "allow"),
        ("write.update", "type:ex:Person/attr:name", "allow"),
    }
    # every write decision names the grant of the action; what the caller sees is the grant's reads
    assert {d[3] for d in decisions(event) if d[0].startswith("write.")} == {"grant.1.actions"}
    assert {d[3] for d in decisions(event) if d[0] == "read.get"} == {"grant.1.reads"}


@pytest.mark.parametrize("kind", sorted(DENY_KINDS))
def test_the_listing_marks_the_action_not_allowed_for_every_kind_of_deny(
    tmp_path: Path, kind: str
) -> None:
    graph, extra, who = DENY_KINDS[kind]
    scene = Scene(tmp_path, graph=graph or None, installation=INSTALLATION + (extra or ""))
    caller = server_actor("closer", **who)
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    found = {
        c.entry.name: c for c in scene.reader.classified_actions(caller, scene.env.entry, published)
    }
    assert not found["mark_damaged"].allowed, kind
    (missing,) = found["mark_damaged"].missing
    assert str(missing) in {
        "write.update on type:inv:Item",  # a type limit, a rule on the action
        "write.update on type:inv:Item/attr:condition",  # a rule on the term
        "write.action on action:mark_damaged",  # a scope or a rule on the entry takes it itself
    }, (kind, missing)
    assert found["mark_damaged"].denials[0].rule_id != "default.deny"


DETACH_UNIT = """\
schema: graph_cli.action/1
name: detach_unit
module: organization
summary: Take a unit out of the unit it is part of.
use_when: A unit leaves its parent unit.
params:
  unit:
    type: ref
    class: org:OrgUnit
    summary: The unit.
ops:
  - {op: unlink, id: $unit, rel: org:part_of, direction: out, all: true}
examples:
  - request: The platform department leaves the group.
"""


def test_a_need_is_met_through_the_first_type_the_deny_only_check_lets_through(
    tmp_path: Path,
) -> None:
    """The need of ``detach_unit`` is any-of over the concrete units (a department, a team): the
    first, a department, is read-only for the caller, so the action is met through a team, and an
    expansion without an op is authorized on that option, not on the one a rule refuses."""
    from support.actions import valid_sources
    from support.catalog_policies import company_env

    env = company_env(tmp_path / "root")
    publish_actions(env, {**valid_sources(), "detach_unit.yaml": DETACH_UNIT})
    made = env.create("org:Department", "d")
    unit = str(made.entities[0].id)
    installation = INSTALLATION.replace(
        f"catalog: {{actions: {ACTIONS}}}\n    reads: {ITEMS}",
        f"catalog: {{actions: [detach_unit]}}\n    reads: [{', '.join(CATALOG_READS)}]",
    ).replace(f"graphs: [{GRAPH}]\n    level: catalog", f"graphs: [{GRAPH}]\n    level: catalog", 1)
    document = compile_policy(
        installation.encode(), file="i.yaml", graphs={GRAPH: env.entry.id}
    ).document
    layer = graph_layer(env, graph_deny('types:\n  "org:Department": read\n'))
    service = action_service(env, Fixed(policy_set(document, layer)))  # type: ignore[arg-type]
    reader = service._reader  # type: ignore[attr-defined]
    published = reader.published_actions(env.entry)
    assert published is not None
    (found,) = reader.classified_actions(CLOSER, env.entry, published)
    assert found.allowed  # met through a team
    with pytest.raises(GraphCliError) as nothing:
        service.do(
            CLOSER,
            env.entry,
            "detach_unit",
            [("unit", unit)],  # type: ignore[list-item]
            dry_run=False,
            reason="t",
            idempotency_key=None,
        )
    assert nothing.value.code == "action.nothing_to_do"  # not a denial on the department


# ---- decided again under the lock, and in a dry run ----------------------------------------


def test_a_policy_applied_while_the_write_waited_decides_the_action(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    allowing = scene.policy.current(scene.env.entry.id)
    denying = policy_set(
        installation_document(scene.env),  # type: ignore[arg-type]
        graph_layer(scene.env, graph_deny('types:\n  "inv:Item": read\n')),  # type: ignore[arg-type]
    )
    swapping = Swapping(allowing, denying)
    scene.install(source=swapping)
    since = scene.count()
    refused = scene.refused(CLOSER, "mark_damaged", scene.item())
    assert swapping.refreshed
    assert refused.code == "access.denied"  # decided again under the lock, after the refresh
    assert scene.condition() is None
    event = scene.newest(since)
    assert event["result"]["status"] == "denied"
    assert any(d[2] == "deny" and d[3] != "default.deny" for d in decisions(event))
    # a rule that takes only the entry (``write.action``), applied meanwhile: the by-hand answer
    # is a default deny, so the under-lock decision goes through the grant and the entry itself
    # is the denial
    scene = Scene(tmp_path / "entry")
    taking = policy_set(
        installation_document(
            scene.env, INSTALLATION + "deny:\n  - {id: no_actions, action: [write.action]}\n"
        )  # type: ignore[arg-type]
    )
    swapping = Swapping(scene.policy.current(scene.env.entry.id), taking)
    scene.install(source=swapping)
    since = scene.count()
    refused = scene.refused(CLOSER, "mark_damaged", scene.item())
    assert swapping.refreshed and refused.code == "access.denied"
    assert refused.details["action"] == "write.action"
    assert ("write.action", "action:mark_damaged", "deny", "deny.no_actions") in decisions(
        scene.newest(since)
    )
    assert scene.condition() is None
    # the other way round: a policy that grants nothing while the write waited: the item is no
    # entity of a caller who sees no type any more (its read view is decided under the new policy)
    scene = Scene(tmp_path / "revoked")
    nothing = policy_set(
        installation_document(  # type: ignore[arg-type]
            scene.env,
            INSTALLATION.replace("principal: {role: closer}", "principal: {role: blind}", 1),
        )
    )
    swapping = Swapping(scene.policy.current(scene.env.entry.id), nothing)
    scene.install(source=swapping)
    refused = scene.refused(CLOSER, "mark_damaged", scene.item())
    assert swapping.refreshed and refused.code == "entity.not_found"
    assert scene.condition() is None


def test_a_dry_run_is_decided_through_the_grant_and_records_nothing(scene: Scene) -> None:
    before = scene.count()
    result = scene.do(CLOSER, "mark_damaged", scene.item(), dry_run=True)
    assert result.write.dry_run is True and result.write.tx_id is None
    assert scene.count() == before
    assert scene.condition() is None


def test_an_idempotent_replay_of_an_action_through_a_grant_is_unchanged(scene: Scene) -> None:
    first = scene.do(CLOSER, "mark_damaged", scene.item(), key="k-1")
    since = scene.count()
    again = scene.do(CLOSER, "mark_damaged", scene.item(), key="k-1")
    assert again.write.idempotent_replay is True
    assert again.write.tx_id == first.write.tx_id
    assert scene.count() == since  # a replay records no new event
    other = scene.refused(CLOSER, "move_item", scene.item(), scene.location(), key="k-1")
    assert other.code == "conflict.idempotency_key_reuse"


# ---- listing ----------------------------------------------------------------------------


def test_the_listing_allows_a_named_action_when_each_need_passes_deny_only(scene: Scene) -> None:
    published = scene.reader.published_actions(scene.env.entry)
    assert published is not None
    closer = scene.reader.classified_actions(CLOSER, scene.env.entry, published)
    assert {c.entry.name for c in closer} == {
        "archive_item",
        "detach_parts",
        "mark_damaged",
        "move_item",
        "retire_item",
        "stock_items",
    }
    assert all(c.allowed for c in closer)
    assert {a.name for a in allowed_entries(closer)} == {c.entry.name for c in closer}
    # a reader of everything sees every action and may run none by hand but the named ones
    plain = scene.reader.classified_actions(PLAIN, scene.env.entry, published)
    assert not any(c.allowed for c in plain)
    stocker = scene.reader.classified_actions(STOCKER, scene.env.entry, published)
    assert all(c.allowed for c in stocker)  # every action of the fixture is named
