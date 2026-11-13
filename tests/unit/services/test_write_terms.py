"""The write service and the action listing under term rules (write allow lists plan W4, W7, W9),
in process over the company example with generated policies.

The property test checks W7 at the level of decisions: the outcome of a whole write (every
decision allowed or one denied) is the same when the attribute and relation decisions are made
for every type and when they are made only for the types some rule names terms of
(``fine_types``). Seeded ``random``, no extra dependency.
"""

import json
import random
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.actions import action_service, company_model, do, publish_actions, valid_sources
from support.services import ServiceEnv, app_actor
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.documents import fine_types
from graph_cli.access_control.policy import policy_set
from graph_cli.access_control.terms import term_resource
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.changeset import ChangeSet, EdgeSpec, LinkOp, UpdateOp, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.kinetic.action_needs import action_needs
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.action_rights import FineTypes, need_decisions
from graph_cli.services.refs import resolve_refs
from graph_cli.services.term_model import term_model
from graph_cli.services.write import WriteRequest, _wanted

GRAPH_NAME = "example_graph"


class Fixed:
    """A policy source with one fixed policy set."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def compiled(env: ServiceEnv, installation: str, graph: str | None = None) -> PolicySet:
    graphs = {GRAPH_NAME: env.entry.id}
    layers = [compile_policy(installation.encode(), file="i.yaml", graphs=graphs).document]
    if graph is not None:
        layers.append(
            compile_policy(
                graph.encode(),
                file="g.yaml",
                graphs=graphs,
                graph=env.entry.id,
                roles=frozenset(),
            ).document
        )
    return policy_set(*layers)  # type: ignore[arg-type]


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    return ServiceEnv("memory", "company", tmp_path / "root", example_version="1.1.0")


def made(env: ServiceEnv, cls: str, local: str) -> EntityId:
    """The entity of ``cls`` that ``ServiceEnv.create`` made (it makes what it requires too)."""
    entities = env.create(cls, local).entities
    found = next(e.id for e in entities if e.local == local)
    assert found is not None
    return found


def request(env: ServiceEnv, changeset: ChangeSet, *, dry_run: bool = True) -> WriteRequest:
    return WriteRequest(
        actor=app_actor(),
        graph=GRAPH_NAME,
        changeset=changeset,
        dry_run=dry_run,
        request=RequestMeta("apply", "test"),
    )


# ---- an unknown term of a limited caller -------------------------------------------------

LIMITED = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ppl:Employee: {update: [name]}
no_project: {"*": write}
"""


def test_an_unknown_attribute_is_an_access_denial_and_a_malformed_name_is_no_bug(
    env: ServiceEnv,
) -> None:
    employee = made(env, "ppl:Employee", "e")
    service = env.make_service(Fixed(compiled(env, LIMITED)))

    def outcome(changeset: ChangeSet) -> GraphCliError:
        with pytest.raises(GraphCliError) as caught:
            service.apply(request(env, changeset))
        return caught.value

    unknown = outcome(
        parse_changeset({"ops": [{"op": "update", "id": str(employee), "set": {"x": 1}}]})
    )
    assert unknown.code == "access.denied"
    assert unknown.details["resource"] == f"graph:{env.entry.id}/type:ppl:Employee/attr:x"
    # a name outside the grammar cannot be written as a resource: no term is decided (no
    # internal.error), the listed attribute is allowed and validation reports the rest
    bad = ChangeSet(ops=(UpdateOp(employee, {"Bad Name": 1, "name": "N"}),))
    assert outcome(bad).code == "write.invalid"
    link = ChangeSet(ops=(LinkOp(employee, EdgeSpec(TermRef("not a curie"), employee)),))
    assert outcome(link).code != "internal.error"


def test_a_resource_for_a_term_outside_the_grammar_is_none() -> None:
    graph = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
    assert term_resource(graph, "ppl:Employee", "attr", "name") == (
        f"graph:{graph}/type:ppl:Employee/attr:name"
    )
    assert term_resource(graph, "ppl:Employee", "rel", "org:holds") == (
        f"graph:{graph}/type:ppl:Employee/rel:org:holds"
    )
    for kind, term in (("attr", "Bad"), ("attr", ""), ("rel", "holds"), ("state", "x")):
        assert term_resource(graph, "ppl:Employee", kind, term) is None


# ---- an action whose expansion is empty --------------------------------------------------

RELEASE = """\
schema: graph_cli.action/1
name: release_position
module: organization
summary: Take the holder of a position away.
use_when: A position should have no holder any more.
params:
  position:
    type: ref
    class: org:Position
    summary: The position.
ops:
  - {op: unlink, id: $position, rel: org:holds, direction: in, all: true}
examples:
  - request: Nobody holds the lead position any more.
"""
EXTERNALS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ppl:External: {unlink: [org:holds]}
no_project: {"*": write}
"""


def run_release(env: ServiceEnv, policy: PolicySet) -> GraphCliError:
    publish_actions(env, {**valid_sources(), "release_position.yaml": RELEASE})
    position = made(env, "org:Position", "p")
    service = action_service(env, Fixed(policy))
    with pytest.raises(GraphCliError) as caught:
        do(service, env, "release_position", f"position={position}")
    return caught.value


def test_an_empty_expansion_is_authorized_on_the_first_concrete_type_that_passes(
    env: ServiceEnv,
) -> None:
    """Nobody holds the position, so the action has nothing to do. The unlink need is on the
    abstract ``ppl:Person``; the External grant meets it, so the answer is the empty
    expansion, not a denial of the abstract class."""
    assert run_release(env, compiled(env, EXTERNALS)).code == "action.nothing_to_do"


def test_an_empty_expansion_no_type_passes_names_the_first_concrete_type(env: ServiceEnv) -> None:
    """The action is listed (``has_reason`` holds for a listing) but the empty expansion has no
    reason, so no option passes the obligation: the denial names a real resource, the first
    concrete type, not the abstract class."""
    with_reason = compiled(env, EXTERNALS)
    document = {
        **with_reason.documents[0],
        "obligations": [{"action": ["write.unlink"], "require_reason": True}],
    }
    error = run_release(env, policy_set(document))
    assert error.code == "access.denied"
    assert error.details["resource"] == f"graph:{env.entry.id}/type:ppl:Employee"
    assert error.details["rule"] == "default.deny"


def test_an_empty_expansion_with_a_leaf_need_is_decided_as_before(env: ServiceEnv) -> None:
    """A need on a concrete class without subclasses has that class as its only option."""
    only = """\
schema: graph_cli.action/1
name: unlink_all_tasks
module: operations
summary: Remove the assignee of a task.
use_when: A task has no assignee any more.
params:
  task:
    type: ref
    class: ops:Task
    summary: The task.
ops:
  - {op: unlink, id: $task, rel: ops:assigned_to, direction: out, all: true}
examples:
  - request: Nobody does the task any more.
"""
    publish_actions(env, {**valid_sources(), "unlink_all_tasks.yaml": only})
    task = made(env, "ops:Task", "t")
    allowed = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ops:Task: {unlink: [ops:assigned_to]}
no_project: {"*": write}
"""
    service = action_service(env, Fixed(compiled(env, allowed)))
    with pytest.raises(GraphCliError) as caught:
        do(service, env, "unlink_all_tasks", f"task={task}")
    assert caught.value.code == "action.nothing_to_do"


# ---- W7 as a property of decisions -------------------------------------------------------

WRITE_ACTIONS = (
    "write.create",
    "write.update",
    "write.link",
    "write.unlink",
    "write.transition",
    "write.supersede",
    "write.retract",
    "write.alias",
)
ATTRIBUTE_ACTIONS = ("write.create", "write.update", "write.transition")
RELATION_ACTIONS = ("write.create", "write.link", "write.unlink")
TYPES = ("ppl:Employee", "ppl:External", "ops:Task", "org:Position")


class Generator:
    """Policies and change sets over the concrete types of the company model."""

    def __init__(self, model: CompiledModel, rng: random.Random) -> None:
        self.rng = rng
        terms = term_model(model).types
        self.attributes = {
            t: sorted(n for n, a in terms[t].attributes.items() if not a.lifecycle) for t in TYPES
        }
        self.lifecycles = {
            t: sorted(n for n, a in terms[t].attributes.items() if a.lifecycle) for t in TYPES
        }
        self.relations = {t: sorted(r for r in terms[t].relations if r != "") for t in TYPES}

    def some(self, items: Sequence[str], low: int = 1) -> list[str]:
        count = self.rng.randint(min(low, len(items)), len(items))
        return sorted(self.rng.sample(list(items), count))

    def writes(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for t in self.some(TYPES):
            verbs: dict[str, Any] = {}
            for verb in self.rng.sample(("create", "update", "link", "unlink", "transition"), 4):
                names = {
                    "create": self.attributes[t],
                    "update": self.attributes[t],
                    "link": self.relations[t],
                    "unlink": self.relations[t],
                    "transition": self.lifecycles[t],
                }[verb]
                if not names or self.rng.random() < 0.5:
                    verbs[verb] = True
                else:
                    verbs[verb] = self.some(names)
            for verb in ("supersede", "retract", "alias"):
                if self.rng.random() < 0.2:
                    verbs[verb] = True
            found[t] = verbs
        return found

    def installation(self) -> str:
        grants: list[dict[str, Any]] = []
        style = self.rng.random()
        if style < 0.3:
            grants.append({"graphs": [GRAPH_NAME], "level": "write"})
        else:
            grants.append(
                {"graphs": [GRAPH_NAME], "level": "read", "writes": self.writes()},
            )
            if style > 0.85:  # two grants add up
                grants.append({"graphs": [GRAPH_NAME], "level": "read", "writes": self.writes()})
        document = {
            "schema": "graph_cli.policy/1",
            "id": "installation",
            "grants": grants,
            "no_project": {"*": "write"},
        }
        return json.dumps(document)

    def graph_layer(self) -> str | None:
        if self.rng.random() < 0.4:
            return None
        deny: list[dict[str, Any]] = []
        for index in range(self.rng.randint(0, 2)):
            t = self.rng.choice(TYPES)
            kind = self.rng.choice(("type", "attribute", "relation"))
            rule: dict[str, Any] = {"id": f"d{index}", "type": [t]}
            if kind == "type":
                rule["action"] = self.some(WRITE_ACTIONS)
            elif kind == "attribute" and self.attributes[t]:
                rule["action"] = self.some(ATTRIBUTE_ACTIONS)
                rule["attribute"] = self.some(self.attributes[t])
            elif kind == "relation" and self.relations[t]:
                rule["action"] = self.some(RELATION_ACTIONS)
                rule["relation"] = self.some(self.relations[t])
            else:
                continue
            deny.append(rule)
        limits = {t: "read" for t in self.some(TYPES, low=0) if self.rng.random() < 0.25}
        document: dict[str, Any] = {"schema": "graph_cli.policy/1", "id": "graph", "deny": deny}
        if limits:
            document["types"] = limits
        return json.dumps(document)

    # ---- change sets

    def pool(self, t: str) -> list[str]:
        """The attribute names of ``t``; an unknown one only now and then."""
        names = [*self.attributes[t], *self.lifecycles[t]]
        return [*names, "nonsense"] if self.rng.random() < 0.15 or not names else names

    def rel(self, t: str) -> str:
        known = self.relations[t]
        return "ops:bogus" if self.rng.random() < 0.1 or not known else self.rng.choice(known)

    def entity(self, t: str) -> str:
        return str(eid(f"{t}#{self.rng.randint(1, 2)}"))

    def op(self) -> dict[str, Any]:
        t = self.rng.choice(TYPES)
        kinds = ["create", "update", "update", "link", "unlink", "transition"]
        kind = self.rng.choice([*kinds, "supersede", "retract", "set_alias"])
        other = self.entity(self.rng.choice(TYPES))
        if kind == "create":
            op: dict[str, Any] = {"op": "create", "type": t}
            pool = self.pool(t)
            names = self.rng.sample(pool, self.rng.randint(0, min(3, len(pool))))
            if names:
                op["attrs"] = {n: 1 for n in names[:-1] or names}
            if len(names) > 1:  # a text value of an attribute the values do not give
                op["attrs_text"] = {names[-1]: "x"}
            if self.rng.random() < 0.5:
                op["edges"] = [{"rel": self.rel(t), "target": other}]
            return op
        subject = self.entity(t)
        if kind == "update":
            op = {"op": "update", "id": subject}
            pool = self.pool(t)
            keys = self.rng.sample(
                ["set", "set_text", "unset", "add", "add_text", "remove", "remove_text"],
                self.rng.randint(1, min(3, len(pool))),
            )
            for key, name in zip(keys, self.rng.sample(pool, len(keys)), strict=True):
                values: dict[str, Any] = {
                    "set": {name: 1},
                    "set_text": {name: "x"},
                    "unset": [name],
                    "add": {name: [1]},
                    "add_text": {name: ["x"]},
                    "remove": {name: [1]},
                    "remove_text": {name: ["x"]},
                }
                op[key] = values[key]
            return op
        if kind == "link":
            return {"op": "link", "id": subject, "rel": self.rel(t), "target": other}
        if kind == "unlink":
            if self.rng.random() < 0.5 and self.relations[t]:
                edge_id = str(edge(self.relations[t][0], "x", name=f"{t}#{subject[-4:]}").edge_id)
                return {"op": "unlink", "id": subject, "edge_id": edge_id}
            return {"op": "unlink", "id": subject, "rel": self.rel(t), "target": other}
        if kind == "transition":
            op = {"op": "transition", "id": subject, "to": "done"}
            if self.rng.random() < 0.5:
                op["attr"] = self.rng.choice([*self.lifecycles[t], "name"])
            return op
        if kind == "supersede":
            return {"op": "supersede", "by": subject, "targets": [other]}
        if kind == "retract":
            return {"op": "retract", "id": subject}
        return {"op": "set_alias", "id": subject, "alias": "POS-0099"}

    def changeset(self) -> ChangeSet:
        count = self.rng.choice([1, 1, 1, 2, 3])
        return parse_changeset({"ops": [self.op() for _ in range(count)]})


def world(model: CompiledModel, generator: Generator) -> DictSnapshot:
    """Two entities per concrete type, each with an edge of a relation of its type."""
    revisions = []
    for t in TYPES:
        for number in (1, 2):
            name = f"{t}#{number}"
            edges = [
                edge(rel, f"{t}#1", name=f"{t}#{str(eid(name))[-4:]}")
                for rel in generator.relations[t][:1]
            ]
            revisions.append(revision(name, t, {}, edges))
    return DictSnapshot(*revisions)


def outcome(decisions: Sequence[Any]) -> bool:
    """Whether the whole write is allowed: every decision allowed."""
    return all(d.effect == "allow" for d in decisions)


def decide(
    env: ServiceEnv,
    policy: PolicySet,
    pairs: Sequence[tuple[str, str]],
    fine: Sequence[tuple[str, str, str, str]],
    types: Callable[[PolicySet], frozenset[str]],
    op_count: int,
) -> bool:
    """The outcome of a write of ``op_count`` ops on ``pairs`` and the terms below the types
    ``types`` names (a dry-run without a reason, as the service decides it)."""
    ctx = AuthzContext(op_count=op_count, has_reason=False, dry_run=True)
    graph = env.entry.id
    resources = {(a, f"graph:{graph}/type:{t}") for a, t in pairs}
    allowed = types(policy)
    for action, type_curie, kind, term in fine:
        resource = term_resource(graph, type_curie, kind, term)
        if resource is not None and type_curie in allowed:
            resources.add((action, resource))
    return outcome([authorize(app_actor(), a, r, ctx, policy) for a, r in sorted(resources)])


@pytest.mark.parametrize("seed", range(6))
def test_fine_decisions_for_the_named_types_decide_like_those_for_all_types(
    env: ServiceEnv, seed: int
) -> None:
    rng = random.Random(1000 + seed)
    generator = Generator(env.model, rng)
    snapshot = world(env.model, generator)
    names = FineTypes()
    seen = {"allowed": 0, "denied": 0, "fine": 0}
    for _ in range(120):
        policy = compiled(env, generator.installation(), generator.graph_layer())
        service = env.make_service(Fixed(policy))
        for _ in range(4):
            changeset = generator.changeset()
            found = resolve_refs(changeset, snapshot)
            wanted = _wanted(found, env.model)
            pairs = [(str(a), str(t)) for a, t in wanted.pairs]
            fine = [(str(a), str(t), k, n) for a, t, k, n in wanted.fine]
            count = len(changeset.ops)
            everywhere = decide(env, policy, pairs, fine, lambda _: frozenset(TYPES), count)
            named = decide(env, policy, pairs, fine, lambda p: names.of(p, env.entry.id), count)
            assert named == everywhere, (changeset, fine_types(policy.documents, env.entry.id))
            # the service itself decides the same
            try:
                service._authorize(request(env, changeset), env.entry, wanted)
                served = True
            except GraphCliError as exc:
                assert exc.code.startswith("access."), exc.code
                served = False
            assert served == everywhere
            seen["allowed" if everywhere else "denied"] += 1
            seen["fine"] += bool(fine_types(policy.documents, env.entry.id))
    assert seen["allowed"] > 100 and seen["denied"] > 100 and seen["fine"] > 200, seen


@pytest.mark.parametrize("seed", range(3))
def test_the_listing_of_actions_decides_like_the_fine_decisions_for_all_types(
    env: ServiceEnv, seed: int
) -> None:
    """The same equivalence for the any-of rule of the action listing: an action is listed
    whether the terms are decided for all types or for the named ones."""
    model = company_model()
    document, problems = compile_actions(valid_sources(), model)
    assert problems == []
    entries = load_action_entries(document)
    rng = random.Random(2000 + seed)
    generator = Generator(model, rng)
    names = FineTypes()
    ctx = AuthzContext(op_count=1, has_reason=True, dry_run=False)
    listed = 0
    for _ in range(150):
        policy = compiled(env, generator.installation(), generator.graph_layer())
        everywhere = frozenset(model.classes)
        for entry in entries:
            met_all = all(
                need_decisions(app_actor(), env.entry.id, n, ctx, policy, everywhere).met
                for n in action_needs(entry, model)
            )
            met_named = all(
                need_decisions(
                    app_actor(), env.entry.id, n, ctx, policy, names.of(policy, env.entry.id)
                ).met
                for n in action_needs(entry, model)
            )
            assert met_named == met_all, entry.name
            listed += met_all
    assert listed > 20


def test_a_deny_rule_below_a_type_does_not_change_the_other_types_decisions(
    env: ServiceEnv,
) -> None:
    """Spot check of what the property rests on: the types the policy has no term rules for
    keep exactly the decisions they had."""
    base = json.dumps(
        {
            "schema": "graph_cli.policy/1",
            "id": "installation",
            "grants": [{"graphs": [GRAPH_NAME], "level": "write"}],
            "no_project": {"*": "write"},
        }
    )
    layer = json.dumps(
        {
            "schema": "graph_cli.policy/1",
            "id": "graph",
            "deny": [
                {
                    "id": "no_name",
                    "action": ["write.update"],
                    "type": ["ppl:Employee"],
                    "attribute": ["name"],
                }
            ],
        }
    )
    policy = compiled(env, base, layer)
    assert fine_types(policy.documents, env.entry.id) == {"ppl:Employee"}
    task = made(env, "ops:Task", "t")
    service = env.make_service(Fixed(policy))
    changeset = parse_changeset(
        {"ops": [{"op": "update", "id": str(task), "set": {"title": "T2"}}]}
    )
    resolution = resolve_refs(changeset, env.snapshot())
    decisions = service._authorize(
        request(env, changeset), env.entry, _wanted(resolution, env.model)
    )
    assert [d.resource for d in decisions] == [f"graph:{env.entry.id}/type:ops:Task"]


def test_the_derived_needs_are_cached_by_compiled_hash_and_pin(env: ServiceEnv) -> None:
    publish_actions(env, valid_sources())
    reader = env.make_reader()
    catalog = reader.published_actions(env.entry)
    assert catalog is not None
    first = reader.action_needs(env.entry, catalog)
    assert reader.action_needs(env.entry, catalog) is first
    assert set(first) == {e.name for e in catalog.entries}
    other_pin = replace(env.entry.pin, artifact_hash="sha256:" + "0" * 64)
    moved = reader.action_needs(replace(env.entry, pin=other_pin), catalog)
    assert moved is not first and moved == first  # another pin derives again, here the same
