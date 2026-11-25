"""The write path of a caller who sees only some types (type read limits plan V6), in process on a
small ontology whose relations cross the line between what the caller sees and what it does not.

Documents are readable (``ex:Doc``), secrets are not (``ex:Secret``); both are records, which
share a unique code and a name, may mention each other (acyclic, at most two, a mention of an
inactive target is refused) and are judged by a rule that counts the mentions of a document. The
data is written by an unlimited caller, then a caller that reads documents only writes.

Integrity is judged on the whole graph; what a refusal says is limited to what the caller sees:
a finding that names something hidden keeps its code, severity and visible target and nothing
else. Every case also runs for the unlimited caller, whose answer is the full one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.data_validator import CONCEALED
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.write import WriteResult

GRAPH = "example_graph"
MODULE = """\
format: graph_cli.ir/1
module: {id: example_module, prefix: ex, version: 1.0.0, summary: Records that mention each other.}
classes:
  Record:
    summary: Anything the system holds.
    abstract: true
    attributes:
      name: {summary: Name of the record., type: string, required: true, role: label}
      code: {summary: Short unique code., type: string, unique: true}
  Doc:
    summary: A document anyone may read.
    is_a: Record
    display_prefix: DOC
  Secret:
    summary: A record only some may read.
    is_a: Record
    display_prefix: SEC
  Memo:
    summary: A note that must be classified under a secret.
    display_prefix: MEM
    attributes:
      name: {summary: Name of the memo., type: string, required: true, role: label}
relations:
  mentions:
    summary: The record mentions the other one.
    domain: Record
    range: Record
    cardinality: {out: {max: 2}}
    characteristics: [acyclic]
    on_target_inactive: refuse
  refers:
    summary: The secret refers to a document.
    domain: Secret
    range: Doc
  classified_under:
    summary: The memo is classified under the secret.
    domain: Memo
    range: Secret
    cardinality: {out: {min: 1}}
rules:
  - id: doc_few_mentions
    summary: A document is mentioned at most once.
    severity: error
    when: write
    scope: Doc
    then:
      - {count: {rel: mentions, direction: in}, maximum_value: 1}
"""
READER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [ex:Doc]
    writes:
      ex:Doc: {create: true, update: true, link: true, unlink: true, retract: true, alias: true}
no_project: {"*": write}
"""
FULL = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
HIDE_SECRETS = """\
schema: graph_cli.policy/1
id: graph
types:
  "ex:Secret": none
"""


class Fixed:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def model() -> CompiledModel:
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": MODULE.encode()}))
    )
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


class World:
    """A graph with documents and secrets, written unlimited, and the two callers."""

    def __init__(self, tmp_path: Path) -> None:
        self.env = ServiceEnv("memory", "inventory", tmp_path / "root", model_override=model())
        self.ids: dict[str, EntityId] = {}
        graph = {GRAPH: self.env.entry.id}
        reads = compile_policy(READER.encode(), file="installation.yaml", graphs=graph).document
        full = compile_policy(FULL.encode(), file="installation.yaml", graphs=graph).document
        layer = compile_policy(
            HIDE_SECRETS.encode(),
            file="graph.yaml",
            graphs=graph,
            graph=self.env.entry.id,
            roles=frozenset(),
        ).document
        # Two callers that do not see secrets: one whose write rights are a ``writes`` list on
        # an allow list of types, one that may write everything but secrets are ``none`` to it
        self.reader = self.env.make_service(Fixed(policy_set(reads)))  # type: ignore[arg-type]
        self.limited = self.env.make_service(Fixed(policy_set(full, layer)))  # type: ignore[arg-type]

    def make(self, *ops: Mapping[str, Any]) -> WriteResult:
        result = self.env.apply(*ops, bindings=self.ids, reason="test")
        self.ids.update(result.bindings)
        return result

    def doc(self, local: str, name: str, *edges: tuple[str, str], **attrs: str) -> dict[str, Any]:
        return self.create("ex:Doc", local, name, edges, attrs)

    def secret(
        self, local: str, name: str, *edges: tuple[str, str], **attrs: str
    ) -> dict[str, Any]:
        return self.create("ex:Secret", local, name, edges, attrs)

    def create(
        self, type_: str, local: str, name: str, edges: Sequence[tuple[str, str]], attrs: Any
    ) -> dict[str, Any]:
        return {
            "op": "create",
            "type": type_,
            "local": local,
            "attrs": {"name": name, **attrs},
            "edges": [{"rel": rel, "target": target} for rel, target in edges],
        }

    def id_of(self, local: str) -> str:
        return str(self.ids[local])

    def as_reader(self, *ops: Mapping[str, Any], dry_run: bool = False, **top: Any) -> WriteResult:
        """A write of the caller that may write everything but secrets, which it does not see."""
        request = self.env.request(
            *ops, dry_run=dry_run, bindings=self.ids, **{"reason": "test", **top}
        )
        return self.limited.apply(request)

    def refused(self, *ops: Mapping[str, Any], as_reader: bool = True, **top: Any) -> GraphCliError:
        with pytest.raises(GraphCliError) as caught:
            if as_reader:
                self.as_reader(*ops, **top)
            else:
                self.env.apply(*ops, bindings=self.ids, **{"reason": "test", **top})
        return caught.value

    def findings(self, error: GraphCliError) -> list[dict[str, Any]]:
        assert error.code == "write.invalid", error
        found = error.details["findings"]
        assert isinstance(found, list)
        return [f for f in found if isinstance(f, dict)]

    def raw(self, local: str) -> Any:
        revision = self.env.snapshot().get(self.ids[local])
        assert revision is not None
        return revision

    def edges_of(self, local: str) -> list[tuple[str, str]]:
        names = {entity: local for local, entity in self.ids.items()}
        return sorted((e.rel, names[e.target]) for e in self.raw(local).edges)


@pytest.fixture
def world(tmp_path: Path) -> World:
    found = World(tmp_path)
    found.make(
        found.doc("d1", "First document", code="D-1"),
        found.doc("d2", "Second document"),
        found.doc("d3", "Third document"),
        found.doc("d4", "Fourth document"),
        found.doc("d5", "Fifth document"),
        found.secret("s1", "Hidden record", code="S-1"),
        found.secret("s2", "Another hidden record"),
    )
    return found


def at(ref: str) -> str:
    """A local name as the change set writes it; ids and aliases (they hold a dash) as they are."""
    return ref if "-" in ref else f"@{ref}"


def link(subject: str, rel: str, target: str) -> dict[str, Any]:
    return {"op": "link", "id": at(subject), "rel": rel, "target": at(target)}


def only(findings: list[dict[str, Any]], code: str) -> dict[str, Any]:
    (found,) = [f for f in findings if f["code"].endswith(code)]
    return found


def concealed(found: dict[str, Any], *, severity: str = "error") -> None:
    """A finding that keeps its code and severity and says nothing else."""
    assert found["message"] == CONCEALED
    assert found["severity"] == severity
    for key in ("expected", "actual", "connecting", "rule_id", "hint"):
        assert key not in found, (key, found)
    assert set(found["target"]) <= {"id", "type", "path", "local"}


def hidden_names(world: World, error: GraphCliError) -> list[str]:
    """What of the hidden entities an error names: ids, names, codes, aliases, the type."""
    text = str([error.message, error.details])
    needles = [world.id_of("s1"), world.id_of("s2"), "Hidden record", "S-1", "ex:Secret", "SEC-"]
    return [n for n in needles if n in text]


# ---- a unique value held by a hidden entity ----------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_a_unique_value_held_by_a_hidden_entity_is_refused_without_naming_it(
    world: World,
) -> None:
    ops = [world.doc("x", "New document", code="S-1")]
    full = world.findings(world.refused(*ops, as_reader=False))
    assert world.id_of("s1") in only(full, "unique_violation")["message"]  # the unlimited answer
    error = world.refused(*ops)
    found = only(world.findings(error), "unique_violation")
    assert found["code"] == "data.semantic.unique_violation"
    concealed(found)
    assert found["target"]["path"] == "attrs.code"
    assert hidden_names(world, error) == []


def test_a_unique_value_held_by_a_visible_entity_is_named_as_before(world: World) -> None:
    error = world.refused(world.doc("x", "New document", code="D-1"))
    found = only(world.findings(error), "unique_violation")
    assert world.id_of("d1") in found["message"]


# ---- an alias owned by a hidden entity ---------------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_an_alias_owned_by_a_hidden_entity_is_refused_without_naming_it(world: World) -> None:
    alias = world.raw("s1").alias
    assert alias is not None
    op = {"op": "set_alias", "id": world.id_of("d1"), "alias": alias}
    assert (
        world.id_of("s1")
        in only(world.findings(world.refused(op, as_reader=False)), "unique_violation")["message"]
    )
    error = world.refused(op)
    found = only(world.findings(error), "unique_violation")
    concealed(found)
    assert found["target"]["path"] == "alias"
    assert hidden_names(world, error) == []
    visible = world.raw("d2").alias
    again = world.refused({"op": "set_alias", "id": world.id_of("d1"), "alias": visible})
    assert world.id_of("d2") in only(world.findings(again), "unique_violation")["message"]


# ---- counts that include hidden edges ----------------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_a_count_that_includes_hidden_edges_is_not_shown(world: World) -> None:
    world.make(link("d1", "ex:mentions", "s1"), link("d1", "ex:mentions", "s2"))
    # d1 mentions two hidden records: a third mention is one too many
    op = link("d1", "ex:mentions", "d2")
    full = only(world.findings(world.refused(op, as_reader=False)), "cardinality_out")
    assert full["actual"] == 3
    found = only(world.findings(world.refused(op)), "cardinality_out")
    concealed(found)
    assert found["target"]["path"] == "edges.ex:mentions"
    # with only visible edges the count is shown
    for hidden in ("@s1", "@s2"):
        world.make({"op": "unlink", "id": "@d1", "rel": "ex:mentions", "target": hidden})
    world.make(link("d1", "ex:mentions", "d2"), link("d1", "ex:mentions", "d3"))
    shown = only(world.findings(world.refused(link("d1", "ex:mentions", "d1"))), "cardinality_out")
    assert shown["actual"] == 3 and shown["message"] != CONCEALED


# ---- a retract blocked by a hidden referrer ----------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_a_retract_blocked_by_a_hidden_referrer_is_refused_without_naming_it(
    world: World,
) -> None:
    world.make(link("s1", "ex:mentions", "d1"))
    op = {"op": "retract", "id": world.id_of("d1")}
    full = only(world.findings(world.refused(op, as_reader=False)), "target_referenced")
    assert full["actual"]["sources"] == [world.id_of("s1")]
    error = world.refused(op)
    found = only(world.findings(error), "target_referenced")
    concealed(found)
    assert found["target"]["path"] == "incoming.ex:mentions"
    assert hidden_names(world, error) == []
    # a visible referrer is named as before
    world.make(link("d3", "ex:mentions", "d2"))
    shown = only(
        world.findings(world.refused({"op": "retract", "id": world.id_of("d2")})),
        "target_referenced",
    )
    assert shown["actual"]["sources"] == [world.id_of("d3")]


# ---- a cycle through hidden entities -----------------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_a_cycle_through_hidden_entities_is_refused_without_naming_them(world: World) -> None:
    world.make(link("d1", "ex:mentions", "s1"), link("s1", "ex:mentions", "d2"))
    op = link("d2", "ex:mentions", "d1")  # d2 -> d1 -> s1 -> d2
    full = only(world.findings(world.refused(op, as_reader=False)), "cycle")
    assert world.id_of("s1") in full["actual"]
    error = world.refused(op)
    found = only(world.findings(error), "cycle")
    concealed(found)
    assert hidden_names(world, error) == []
    # a cycle of visible entities is shown
    world.make(link("d4", "ex:mentions", "d5"))
    visible = only(world.findings(world.refused(link("d5", "ex:mentions", "d4"))), "cycle")
    assert visible["actual"][0] == world.id_of("d5") and visible["message"] != CONCEALED


# ---- a rule that counts hidden entities --------------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_the_values_of_a_rule_that_counts_hidden_entities_are_dropped(world: World) -> None:
    world.make(link("s1", "ex:mentions", "d1"))  # one mention of d1 the reader cannot see
    op = link("d2", "ex:mentions", "d1")  # the second mention: the rule says at most one
    full = only(world.findings(world.refused(op, as_reader=False)), "violated")
    assert full["actual"] is not None and full["rule_id"] == "ex.doc_few_mentions"
    found = only(world.findings(world.refused(op)), "violated")
    assert found["rule_id"] == "ex.doc_few_mentions"  # the rule is in the reader's model
    assert found["message"] == "A document is mentioned at most once."
    assert "actual" not in found


# ---- findings keep the visible parts they always had -------------------------------------


def test_a_finding_about_visible_data_is_what_it_was(world: World) -> None:
    bad = {"op": "update", "id": world.id_of("d1"), "set": {"name": 7}}
    full = world.findings(world.refused(bad, as_reader=False))
    assert world.findings(world.refused(bad)) == full


# ---- possible duplicates ----------------------------------------------------------------


def test_a_possible_duplicate_of_a_hidden_entity_is_dropped(world: World) -> None:
    result = world.as_reader(world.doc("x", "hidden record"))  # a name a secret has
    assert [f.code for f in result.findings] == []
    other = world.as_reader(world.doc("y", "FIRST document"))  # a name a document has
    assert [f.code for f in other.findings] == ["data.semantic.possible_duplicate"]
    full = world.env.apply(world.doc("z", "hidden Record"), bindings=world.ids)
    assert [f.code for f in full.findings] == ["data.semantic.possible_duplicate"]
    # a refused write does not carry the warning about a hidden entity either
    error = world.refused(
        world.doc("w", "another HIDDEN record"), {"op": "update", "id": "@d1", "set": {"name": 7}}
    )
    assert [f["code"] for f in world.findings(error)] == ["data.structural.datatype_mismatch"]
    assert hidden_names(world, error) == []


# ---- hidden targets and what a write keeps ----------------------------------------------


def test_a_new_edge_to_a_hidden_entity_is_an_edge_to_a_missing_one(world: World) -> None:
    ghost = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
    hidden = world.findings(world.refused(link("d1", "ex:mentions", world.id_of("s1"))))
    missing = world.findings(world.refused(link("d1", "ex:mentions", ghost)))
    swapped = str(hidden).replace(world.id_of("s1"), ghost)
    assert swapped == str(missing)
    assert only(hidden, "target_missing")["code"] == "data.referential.target_missing"
    create = world.doc("x", "New", ("ex:mentions", world.id_of("s1")))
    again = world.findings(world.refused(create))
    assert [f["code"] for f in again] == ["data.referential.target_missing"]
    # the unlimited caller reaches it
    world.env.apply(link("d1", "ex:mentions", "s1"), bindings=world.ids)


def test_an_update_keeps_the_hidden_edges_and_an_unlink_cannot_reach_them(world: World) -> None:
    world.make(link("d1", "ex:mentions", "s1"), link("d1", "ex:mentions", "d2"))
    raw_edge = next(e for e in world.raw("d1").edges if e.target == world.ids["s1"])
    result = world.as_reader({"op": "update", "id": world.id_of("d1"), "set": {"name": "Renamed"}})
    assert [e.alias for e in result.entities] == [world.raw("d1").alias]
    assert world.edges_of("d1") == [("ex:mentions", "d2"), ("ex:mentions", "s1")]  # kept
    # an unlink of the hidden edge, by target or by edge id, finds no edge
    by_target = {"op": "unlink", "id": world.id_of("d1"), "rel": "ex:mentions"}
    ghost = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
    hidden = world.findings(world.refused({**by_target, "target": world.id_of("s1")}))
    missing = world.findings(world.refused({**by_target, "target": ghost}))
    assert str(hidden).replace(world.id_of("s1"), ghost) == str(missing)
    assert only(hidden, "edge_missing")["code"] == "data.referential.edge_missing"
    by_id = {"op": "unlink", "id": world.id_of("d1"), "edge_id": str(raw_edge.edge_id)}
    other = {**by_id, "edge_id": ghost}
    assert str(world.findings(world.refused(by_id))).replace(str(raw_edge.edge_id), ghost) == str(
        world.findings(world.refused(other))
    )
    assert world.edges_of("d1") == [("ex:mentions", "d2"), ("ex:mentions", "s1")]
    # a visible edge goes, the hidden one stays
    world.as_reader({**by_target, "target": world.id_of("d2")})
    assert world.edges_of("d1") == [("ex:mentions", "s1")]


def test_a_ref_to_a_hidden_entity_is_a_ref_to_a_missing_one(world: World) -> None:
    alias = world.raw("s1").alias
    for ref, ghost in (
        (world.id_of("s1"), "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"),
        (alias, "SEC-0099"),
    ):
        hidden = world.refused({"op": "update", "id": ref, "set": {"name": "X"}})
        missing = world.refused({"op": "update", "id": ghost, "set": {"name": "X"}})
        assert (hidden.code, hidden.message.replace(str(ref), ghost)) == (
            missing.code,
            missing.message,
        )
        assert missing.code == "entity.not_found"
        assert hidden.details["ref"] == ref


# ---- a type the caller cannot see is a type that does not exist --------------------------


def test_a_create_of_a_hidden_type_is_a_create_of_an_unknown_type(world: World) -> None:
    hidden = world.findings(world.refused(world.secret("x", "Hidden record")))
    unknown = world.findings(
        world.refused({"op": "create", "type": "ex:Gone", "attrs": {"name": "Hidden record"}})
    )
    (a,), (b,) = hidden, unknown
    assert a["code"] == b["code"] == "data.structural.unknown_type"
    assert a["message"] == "type ex:Secret does not exist"
    assert "ex:Secret" not in str(a["expected"]) and a["actual"] == "ex:Secret"
    assert {k: v for k, v in a.items() if k not in ("message", "actual", "target", "expected")} == {
        k: v for k, v in b.items() if k not in ("message", "actual", "target", "expected")
    }
    assert a["target"] == {"type": "ex:Secret", "path": "type", "local": "x"} or a["target"] == {
        "type": "ex:Secret",
        "path": "type",
    }


def test_an_abstract_type_names_only_the_subtypes_the_caller_sees(world: World) -> None:
    unlimited = only(
        world.findings(
            world.refused({"op": "create", "type": "ex:Record", "attrs": {}}, as_reader=False)
        ),
        "abstract_type",
    )
    assert unlimited["expected"] == ["ex:Doc", "ex:Secret"]
    found = only(
        world.findings(world.refused({"op": "create", "type": "ex:Record", "attrs": {}})),
        "abstract_type",
    )
    assert found["expected"] == ["ex:Doc"]


def test_a_relation_the_caller_does_not_see_is_an_unknown_relation(world: World) -> None:
    op = {
        "op": "create",
        "type": "ex:Doc",
        "attrs": {"name": "X"},
        "edges": [{"rel": "ex:refers", "target": world.id_of("d1")}],
    }
    full = world.findings(world.refused(op, as_reader=False))
    assert [f["code"] for f in full] == ["data.referential.domain_violation"]
    found = world.findings(world.refused(op))
    assert [f["code"] for f in found] == ["data.referential.unknown_relation"]
    assert "ex:refers" in found[0]["message"]  # the relation the caller named
    assert "refers" not in str(found[0].get("expected"))


# ---- the result -------------------------------------------------------------------------


def test_the_result_names_entities_of_visible_types_only(world: World) -> None:
    snapshot = world.env.snapshot()
    revisions = {
        "d1": snapshot.get(world.ids["d1"]),
        "s1": snapshot.get(world.ids["s1"]),
    }
    assert all(revisions.values())
    working = _working(world, [("update", "d1"), ("update", "s1")])
    from graph_cli.kernel.read_view import ReadView
    from graph_cli.services.write import _result

    kwargs: dict[str, Any] = {
        "tx_id": None,
        "seq": None,
        "event_id": None,
        "dry_run": False,
        "warnings": (),
        "findings": (),
    }
    everything = _result(world.env.entry, working, **kwargs)
    assert len(everything.entities) == 2
    shown = _result(world.env.entry, working, view=ReadView(frozenset({"ex:Doc"})), **kwargs)
    assert [e.type for e in shown.entities] == ["ex:Doc"]
    assert world.id_of("s1") not in str(shown.to_json())
    unchanged = replace(working, unchanged=working.revisions, revisions={})
    quiet = _result(world.env.entry, unchanged, view=ReadView(frozenset({"ex:Doc"})), **kwargs)
    assert [w.code for w in quiet.warnings] == ["entity.unchanged"]
    assert world.id_of("s1") not in str([w.message for w in quiet.warnings])


def _working(world: World, touches: list[tuple[str, str]]) -> Any:
    """The working state of an unlimited update of the named entities."""
    from graph_cli.kernel.ids import TxId
    from graph_cli.temporal_knowledge.normalize import normalize

    ops = [
        world.env.changeset({"op": op, "id": world.id_of(local), "set": {"name": f"New {local}"}})
        for op, local in touches
    ]
    snapshot = world.env.snapshot()
    refs: dict[str, EntityId] = {}
    return normalize(
        tuple(o for cs in ops for o in cs.ops),
        snapshot,
        world.env.model,
        refs=refs,
        new_id=world.env.ids.new_id,
        recorded_at=world.env.clock.now(),
        tx_id=TxId(world.env.ids.new_id()),
        tx_seq=snapshot.seq + 1,
        pin_version="1.0.0",
        alias_counters={},
        alias_prefixes={},
    )


# ---- the answer and the audit of a create of a type the caller cannot see ----------------


def decisions_of(event: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    return [
        (d["action"], d["resource"].rpartition("type:")[2], d["effect"], d["rule"])
        for d in event["authz"]["decisions"]
    ]


@pytest.mark.doc_claim("security_model.writes_only_visible_types")
def test_a_writes_only_caller_is_denied_a_hidden_type_as_it_is_an_unknown_one(
    world: World,
) -> None:
    def attempt(type_: str) -> GraphCliError:
        with pytest.raises(GraphCliError) as caught:
            world.reader.apply(
                world.env.request(
                    {"op": "create", "type": type_, "attrs": {"name": "N"}}, reason="test"
                )
            )
        return caught.value

    before = len(world.env.installation_events())
    hidden, unknown = attempt("ex:Secret"), attempt("ex:Gone")
    assert hidden.code == unknown.code == "access.denied"
    assert hidden.details["rule"] == unknown.details["rule"] == "default.deny"
    assert hidden.message.replace("ex:Secret", "ex:Gone") == unknown.message
    assert hidden.details["resource"].replace("Secret", "Gone") == unknown.details["resource"]
    events = world.env.installation_events()[before:]
    assert [e["result"]["status"] for e in events] == ["denied", "denied"]
    # the audit has the decision on the type itself: the type is hidden by a read allow list
    assert ("read.get", "ex:Secret", "deny", "default.deny") in decisions_of(events[0])
    assert ("write.create", "ex:Secret", "deny", "default.deny") in decisions_of(events[0])
    assert [d for d in decisions_of(events[1]) if d[0] == "read.get"] == []  # no such type


@pytest.mark.doc_claim("security_model.required_relation_hidden_range")
def test_a_required_relation_to_a_hidden_type_blocks_every_create_of_its_type(
    world: World,
) -> None:
    """The caller sees memos and cannot see the secrets a memo must be classified under: it
    cannot give a target, so no create of a memo gets through, with or without an edge."""
    memo = {"op": "create", "type": "ex:Memo", "attrs": {"name": "Memo"}}
    head = world.env.head_seq()
    without = world.refused(memo)
    found = only(world.findings(without), "cardinality_out")
    concealed(found)
    assert hidden_names(world, without) == []
    edge = {"rel": "ex:classified_under", "target": world.id_of("s1")}
    with_edge = world.refused({**memo, "edges": [edge]})
    assert hidden_names(world, with_edge) == []
    assert world.env.head_seq() == head  # nothing was written
    # the caller that sees secrets creates the memo with a target
    created = world.env.apply({**memo, "edges": [edge]}, bindings=world.ids, reason="test")
    assert created.seq is not None


def test_a_caller_whose_type_is_none_gets_the_answer_for_an_unknown_type(world: World) -> None:
    """The ``none`` limit denies ``write.*`` on the type by a rule that names it, and a refusal
    built from that decision would name the type's rule. The answer is the one for a type the
    model lacks: validation judges the type unknown. The audit has the real decisions."""
    before, head = len(world.env.installation_events()), world.env.head_seq()
    hidden = world.refused(world.secret("x", "N"))
    unknown = world.refused({"op": "create", "type": "ex:Gone", "attrs": {"name": "N"}})
    (a,), (b,) = world.findings(hidden), world.findings(unknown)
    assert (a["code"], a["message"].replace("Secret", "Gone")) == (b["code"], b["message"])
    assert "type.ex:Secret" not in str(hidden.details)
    events = world.env.installation_events()[before:]
    assert [e["result"]["status"] for e in events] == ["denied"]  # the unknown one is no denial
    decisions = decisions_of(events[0])
    assert ("read.get", "ex:Secret", "deny", "type.ex:Secret") in decisions
    assert ("write.create", "ex:Secret", "deny", "type.ex:Secret") in decisions
    assert world.env.head_seq() == head  # nothing was written


def test_a_dry_run_of_a_hidden_create_is_answered_alike_and_not_audited(world: World) -> None:
    before = len(world.env.installation_events()) + len(world.env.graph_events())
    with pytest.raises(GraphCliError) as caught:
        world.as_reader(world.secret("x", "N"), dry_run=True)
    assert caught.value.code == "write.invalid"
    assert len(world.env.installation_events()) + len(world.env.graph_events()) == before


def test_a_write_of_a_limited_caller_records_the_read_decision_of_every_type(
    world: World,
) -> None:
    world.as_reader({"op": "update", "id": world.id_of("d1"), "set": {"name": "Renamed"}})
    event = [e for e in world.env.graph_events() if e["result"]["status"] == "ok"][-1]
    decisions = [(a, t, e) for a, t, e, _ in decisions_of(event)]
    assert decisions == [("read.get", "ex:Doc", "allow"), ("write.update", "ex:Doc", "allow")]


# ---- dry runs and batches ---------------------------------------------------------------


def batch(world: World, documents: Sequence[Sequence[Mapping[str, Any]]]) -> Any:
    requests = [
        world.env.request(*doc, dry_run=True, bindings=world.ids, reason="test")
        for doc in documents
    ]
    return world.limited.apply_batch(requests)


def test_a_dry_run_batch_judges_the_working_state_and_answers_as_a_limited_caller(
    world: World,
) -> None:
    done = batch(
        world,
        [
            [world.doc("a", "Batch A")],
            [world.doc("b", "Batch B", ("ex:mentions", "@a"))],  # an earlier create
            [link("b", "ex:mentions", "d2")],
        ],
    )
    assert done.error is None and len(done.results) == 3
    assert all(e.id is None for e in done.results[0].entities)
    assert world.id_of("s1") not in str([r.to_json() for r in done.results])
    # the working state still holds the hidden mention of d1: the second one is one too many
    world.make(link("s1", "ex:mentions", "d1"))
    refused = batch(
        world,
        [[world.doc("a", "Batch A")], [link("a", "ex:mentions", "d1")]],
    )
    assert refused.failed_index == 1 and refused.error is not None
    found = only(world.findings(refused.error), "violated")
    assert found["rule_id"] == "ex.doc_few_mentions" and "actual" not in found
    # a hidden type fails the document it is in, as an unknown type does
    unknown = batch(world, [[world.doc("a", "Batch A")], [world.secret("c", "Batch C")]])
    assert unknown.failed_index == 1 and unknown.error is not None
    assert only(world.findings(unknown.error), "unknown_type")["actual"] == "ex:Secret"


def test_a_dry_run_ref_to_a_hidden_entity_is_a_missing_one(world: World) -> None:
    ghost = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
    errors = []
    for ref in (world.id_of("s1"), ghost):
        with pytest.raises(GraphCliError) as caught:
            world.as_reader({"op": "update", "id": ref, "set": {"name": "X"}}, dry_run=True)
        errors.append(caught.value)
    assert errors[0].code == errors[1].code == "entity.not_found"
    assert str([errors[0].message, errors[0].details]).replace(world.id_of("s1"), ghost) == str(
        [errors[1].message, errors[1].details]
    )


# ---- a caller that sees everything, and a policy without read rules ----------------------

OPEN = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: documents only"}
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [ex:Doc]
no_project: {"*": write}
"""


def run_script(tmp_path: Path, source: str | None) -> tuple[list[Any], list[Any]]:
    """The same writes on a fresh graph: its transactions and audit events, as stored."""
    found = World(tmp_path)
    if source is not None:
        graph = {GRAPH: found.env.entry.id}
        document = compile_policy(source.encode(), file="installation.yaml", graphs=graph)
        service = found.env.make_service(Fixed(policy_set(document.document)))  # type: ignore[arg-type]
    else:
        service = found.env.service
    ids: dict[str, EntityId] = {}

    def apply(*ops: Mapping[str, Any]) -> None:
        result = service.apply(found.env.request(*ops, bindings=ids, reason="script"))
        ids.update(result.bindings)

    apply(found.doc("a", "Alpha", code="A-1"), found.secret("s", "Hidden", code="S-1"))
    apply(link("a", "ex:mentions", "s"), {"op": "update", "id": "@a", "set": {"name": "Alpha 2"}})
    with pytest.raises(GraphCliError):
        apply(found.doc("b", "Beta", code="A-1"))
    apply({"op": "unlink", "id": "@a", "rel": "ex:mentions", "target": "@s"})
    apply({"op": "retract", "id": "@s"})
    txs = [t.__repr__() for t in found.env.txs()]
    return txs, [dict(e) for e in found.env.graph_events()]


def test_a_caller_who_sees_everything_writes_byte_for_byte_as_without_read_rules(
    tmp_path: Path,
) -> None:
    plain = run_script(tmp_path / "plain", None)
    open_graph = run_script(tmp_path / "open", OPEN)  # read limits for other callers exist
    assert plain[0] == open_graph[0]
    assert [_without_policy(e) for e in plain[1]] == [_without_policy(e) for e in open_graph[1]]
    assert "read.get" not in str(open_graph[1])  # no read decision: this caller is not limited


def _without_policy(event: Mapping[str, Any]) -> dict[str, Any]:
    """An event but for what differs by the installed policy itself: its hash, the ids of the
    rules that decided, and the hash chain that covers them."""
    found = {k: v for k, v in event.items() if k not in ("authz", "prev_hash", "hash")}
    authz = event["authz"]
    found["decisions"] = [{k: v for k, v in d.items() if k != "rule"} for d in authz["decisions"]]
    return found
