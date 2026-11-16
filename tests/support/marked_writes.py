"""The write side of field visibility (markings, stage 4 step 6): an ontology whose marked
attributes cover every way a write could meet a hidden attribute, its twin, the callers, and a
pair of in-memory graphs that answer the same write side by side.

``MODULE`` marks the personal data of ``ex:Person`` (a plain value, a list, an enum, an immutable
code, a number that a team's rule sums up and a guard reads) with ``ex:personal``, plus one
hidden attribute each on types that show what a default does with it: required with a default
(``ex:Badge``), required without one (``ex:Card``), unique with a default (``ex:Token``),
deprecated with a default (``ex:Ticket``), and a hidden alternative label (``ex:Tag``).
``ex:Vault`` keeps a marked attribute in both modules.

``TWIN`` is the module without every hidden attribute and what only they use, but with
``ex:Vault``'s marking: a caller without the marking has an attribute view of both graphs, so a
name it writes takes the same path in both, and an attribute hidden in one graph is a name the
type does not declare in the other. ``Pair`` holds both graphs with the same seeded ids; the
oracle is that every answer to a caller without the marking is the same in both.
"""

import difflib
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.write import WriteRequest, WriteService
from support.markings import Answer
from support.services import ServiceEnv

GRAPH = "example_graph"
PERSONAL = "ex:personal"
_HEAD = """\
format: graph_cli.ir/1
module: {id: example_module, prefix: ex, version: 1.0.0, summary: People and their things.}
markings:
  personal: {summary: Personal data of a natural person.}
"""
_FLOW = """\
lifecycles:
  Flow:
    summary: Onboarding of a person.
    states:
      open: {summary: Onboarding started., initial: true}
      done: {summary: Onboarding done.}
    transitions:
      - from: open
        to: done
        summary: Finish the onboarding.
"""
_GUARD = """\
        guards:
          - id: has_code
            summary: The personal code is recorded.
            require:
              - {attr: code, value_presence: PRESENT}
"""
_KIND = """\
enums:
  Kind:
    summary: A kind of team.
    values:
      line: {summary: Line team.}
      staff: {summary: Staff team.}
"""
_GRADE = """\
  Grade:
    summary: A pay grade.
    values:
      low: {summary: Low.}
      mid: {summary: Middle.}
      high: {summary: High.}
"""
_PERSON = """\
  Person:
    summary: A person.
    display_prefix: PER
    attributes:
      name: {summary: Name., type: string, required: true, role: label}
      phone: {summary: Work phone., type: string}
      stage: {summary: Onboarding stage., lifecycle: Flow, required: true}
"""
_PERSON_HIDDEN = """\
      email: {summary: Private e-mail., type: string, markings: [personal]}
      nicknames: {summary: Nicknames., type: string, many: true, markings: [personal]}
      grade: {summary: Pay grade., type: Grade, markings: [personal]}
      code: {summary: Personal code., type: string, mutability: immutable, markings: [personal]}
      hours: {summary: Weekly hours., type: integer, markings: [personal]}
"""
_OTHERS = """\
  Team:
    summary: A team.
    display_prefix: TEAM
    attributes:
      name: {summary: Name., type: string, required: true, role: label}
      kind: {summary: Kind., type: Kind}
  Tag:
    summary: A tag.
    display_prefix: TAG
    attributes:
      title: {summary: Title., type: string, role: label}
<tag>
  Badge:
    summary: A badge.
    display_prefix: BDG
    attributes:
      number: {summary: Number., type: integer}
<badge>
  Card:
    summary: A card.
    display_prefix: CARD
    attributes:
      number: {summary: Number., type: integer}
<card>
  Token:
    summary: A token.
    display_prefix: TOK
    attributes:
      number: {summary: Number., type: integer}
<token>
  Ticket:
    summary: A ticket.
    display_prefix: TCK
    attributes:
      number: {summary: Number., type: integer}
<ticket>
  Vault:
    summary: A vault.
    display_prefix: VLT
    attributes:
      number: {summary: Number., type: integer}
      secret: {summary: Secret., type: string, markings: [personal]}
relations:
  member_of:
    summary: The team a person belongs to.
    domain: Person
    range: Team
"""
_HIDDEN_OTHERS = {
    "tag": "      private_name: {summary: Private name., type: string, role: alt_label,"
    " markings: [personal]}\n",
    "badge": "      level: {summary: Level., type: Grade, required: true, default: low,"
    " markings: [personal]}\n",
    "card": "      holder: {summary: Holder., type: string, required: true,"
    " markings: [personal]}\n",
    "token": "      serial: {summary: Serial., type: string, unique: true, default: T-1,"
    " markings: [personal]}\n",
    "ticket": "      legacy: {summary: Old field., type: string, default: L, deprecated: true,"
    " markings: [personal]}\n",
}
_RULES = """\
rules:
  - id: contact
    summary: A person with a private e-mail has a work phone.
    severity: error
    when: write
    scope: Person
    if:
      - {attr: email, value_presence: PRESENT}
    then:
      - {attr: phone, value_presence: PRESENT}
  - id: team_hours
    summary: A team works at most 60 hours a week.
    severity: error
    when: write
    scope: Team
    then:
      - {sum: {rel: member_of, direction: in, attr: hours}, maximum_value: 60}
  - id: switchboard
    summary: A work phone is a switchboard line.
    severity: error
    when: write
    scope: Person
    if:
      - {attr: phone, value_presence: PRESENT}
    then:
      - {attr: phone, equals_string_in: ["+1", "+2"]}
"""
_TWIN_RULES = """\
rules:
  - id: switchboard
    summary: A work phone is a switchboard line.
    severity: error
    when: write
    scope: Person
    if:
      - {attr: phone, value_presence: PRESENT}
    then:
      - {attr: phone, equals_string_in: ["+1", "+2"]}
"""


def _fill(text: str, parts: Mapping[str, str]) -> str:
    for key, part in parts.items():
        text = text.replace(f"<{key}>\n", part)
    return text


MODULE = (
    _HEAD
    + _KIND
    + _GRADE
    + _FLOW
    + _GUARD
    + "classes:\n"
    + _PERSON
    + _PERSON_HIDDEN
    + _fill(_OTHERS, _HIDDEN_OTHERS)
    + _RULES
)
TWIN = (
    _HEAD
    + _KIND
    + _FLOW
    + "classes:\n"
    + _PERSON
    + _fill(_OTHERS, dict.fromkeys(_HIDDEN_OTHERS, ""))
    + _TWIN_RULES
)
# The attributes a caller without ``ex:personal`` cannot see, per type (``ex:Vault.secret`` is
# hidden in both modules and never written).
HIDDEN = {
    "ex:Person": ("code", "email", "grade", "hours", "nicknames"),
    "ex:Tag": ("private_name",),
    "ex:Badge": ("level",),
    "ex:Card": ("holder",),
    "ex:Token": ("serial",),
    "ex:Ticket": ("legacy",),
}

# The callers. ``LISTED`` may write a list of attributes that names hidden ones; ``WRITER`` may
# write every attribute of the types it names (``update: true``); ``EDITOR`` may write the graph
# (no term rules); ``HOLDER`` also holds the marking.
_ALL_VERBS = "{create: true, update: true, transition: true, link: true, unlink: true}"
POLICIES = {
    "listed": """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ex:Person:
        create: [name, phone, email, code]
        update: [name, phone, email, nicknames, grade, code, hours]
        transition: true
no_project: {"*": write}
""",
    "writer": f"""\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ex:Person: {_ALL_VERBS}
      ex:Team: {_ALL_VERBS}
      ex:Tag: {_ALL_VERBS}
      ex:Badge: {_ALL_VERBS}
      ex:Card: {_ALL_VERBS}
      ex:Token: {_ALL_VERBS}
      ex:Ticket: {_ALL_VERBS}
no_project: {{"*": write}}
""",
    "editor": """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
""",
    "holder": """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [ex:personal]}
no_project: {"*": write}
""",
}
LIMITED = ("listed", "writer", "editor")  # the callers without the marking


def compiled(source: str) -> CompiledModel:
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": source.encode()}))
    )
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


class Fixed:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def bare(op: Mapping[str, Any], types: Mapping[str, str]) -> dict[str, Any]:
    """``op`` as it is written to the twin: without hidden attributes (``types``: the type of
    every entity an update may name, by reference)."""
    found = dict(op)
    if op.get("op") == "create":
        hidden = HIDDEN.get(str(op["type"]), ())
        found["attrs"] = {k: v for k, v in op.get("attrs", {}).items() if k not in hidden}
    elif op.get("op") == "update":
        hidden = HIDDEN.get(types[str(op["id"])], ())
        for key in ("set", "add", "remove"):
            if key in op:
                found[key] = {k: v for k, v in op[key].items() if k not in hidden}
        if "unset" in op:
            found["unset"] = [n for n in op["unset"] if n not in hidden]
    return found


class Pair:
    """``MODULE`` and ``TWIN`` in two in-memory graphs with the same seeded ids (graph, entities,
    transactions, events), one write service per caller and graph."""

    def __init__(self, tmp_path: Path) -> None:
        self.marked = ServiceEnv(
            "memory", "inventory", tmp_path / "m", model_override=compiled(MODULE)
        )
        self.twin = ServiceEnv("memory", "inventory", tmp_path / "t", model_override=compiled(TWIN))
        assert self.marked.entry.id == self.twin.entry.id
        self.services: dict[str, tuple[WriteService, WriteService]] = {}
        for name, text in POLICIES.items():
            graphs = {GRAPH: self.marked.entry.id}
            document = compile_policy(text.encode(), file="i.yaml", graphs=graphs).document
            source = Fixed(policy_set(document))
            self.services[name] = (
                self.marked.make_service(source),
                self.twin.make_service(source),
            )
        self.ids: dict[str, EntityId] = {}
        self.types: dict[str, str] = {}  # reference -> type, for ``bare``

    def request(self, env: ServiceEnv, *ops: Mapping[str, Any], **kw: Any) -> WriteRequest:
        kw.setdefault("reason", "test")
        return env.request(*ops, bindings=self.ids, **kw)

    def setup(self, *ops: Mapping[str, Any], **kw: Any) -> None:
        """Write ``ops`` to the marked graph as the holder, without hidden values to the twin."""
        marked, twin = self.services["holder"]
        result = marked.apply(self.request(self.marked, *ops, **kw))
        # the twin must commit too (same ids): every op keeps something visible
        twin.apply(self.request(self.twin, *(bare(op, self.types) for op in ops), **kw))
        self.ids.update(result.bindings)
        for entity in result.entities:
            if entity.local is not None:
                self.types[f"@{entity.local}"] = entity.type
                assert entity.id is not None
                self.types[str(entity.id)] = entity.type

    def ask(self, caller: str, *ops: Mapping[str, Any], **kw: Any) -> tuple[Answer, Answer]:
        """The answers of both graphs to the same write of ``caller``."""
        marked, twin = self.services[caller]
        return (
            Answer.of(lambda: marked.apply(self.request(self.marked, *ops, **kw))),
            Answer.of(lambda: twin.apply(self.request(self.twin, *ops, **kw))),
        )

    def same(self, caller: str, *ops: Mapping[str, Any], **kw: Any) -> Answer:
        """Ask both graphs: the answers are the same (the oracle) and name no hidden value."""
        got, want = self.ask(caller, *ops, **kw)
        assert got == want, {"marked": got, "twin": want}
        return got

    def raw(self, ref: str) -> dict[str, JsonValue]:
        """The attributes an entity of the marked graph (a local or an id) holds in the store."""
        entity = self.ids.get(ref) or EntityId(parse_id(ref))
        revision = self.marked.snapshot().get(entity)
        assert revision is not None
        return dict(revision.attrs)


def findings(answer: Answer) -> list[dict[str, Any]]:
    """The findings of a ``write.invalid`` answer."""
    assert not answer.ok and answer.body[0] == "write.invalid", answer
    found = answer.body[2]["findings"]
    assert isinstance(found, list)
    return found


@cache
def _twin() -> CompiledModel:
    return compiled(TWIN)


def unknown(type_: str, name: str, **target: Any) -> dict[str, Any]:
    """The ``unknown_attribute`` finding of a name the type lacks, close names from what a
    caller without the marking sees of the type (the twin's attributes)."""
    found: dict[str, Any] = {
        "severity": "error",
        "code": "data.structural.unknown_attribute",
        "target": {**target, "type": type_, "path": f"attrs.{name}"},
        "message": f"{type_} has no attribute {name!r}",
    }
    close = difflib.get_close_matches(name, sorted(_twin().classes[type_].attributes), n=3)
    if close:
        found["expected"] = close
    return found


def decisions_of(event: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    """``(action, resource, effect, rule)`` of every decision an audit event records."""
    return [
        (d["action"], d["resource"], d["effect"], d["rule"]) for d in event["authz"]["decisions"]
    ]


def markings_in(events: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, str, str]]:
    """The decisions on marking resources that ``events`` record."""
    return [d for e in events for d in decisions_of(e) if d[1].startswith("marking:")]
