"""Shared pieces of the field visibility tests (markings, stage 4): the ``staff`` fixture and
its twin, a data set with hidden values, policies that grant markings, and helpers that check
what a caller sees of a hidden attribute.

The ``staff`` module (``tests/fixtures/ontologies/staff/1.0.0``) marks the personal data of
``stf:Employee`` with ``stf:personal`` (``salary`` also with ``stf:pay``), the only label of
``stf:Badge`` and the ``remark`` of ``stf:PersonnelFile``; ``stf:Contractor`` declares an
unmarked ``email`` of its own. Its twin (``tests/fixtures/markings/staff_twin``) is the module
without everything a caller holding no marking may not see: a store of the twin holding the
same entities without the hidden values is the oracle of "as if the type did not have it".

Without an installation policy no app caller holds a marking (the built-in policy is
unchanged), so the plain local caller of the CLI tests is the limited reader; ``HOLDER``
grants every marking to the app entrypoint.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from graph_cli.kernel.records import EdgeRecord, content_hash, content_projection
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from support.fixtures import FIXTURES, ONTOLOGIES

STAFF = ONTOLOGIES / "staff"
STAFF_TWIN = FIXTURES / "markings" / "staff_twin"
EMPLOYEE = "stf:Employee"
CONTRACTOR = "stf:Contractor"
BADGE = "stf:Badge"
CREW = "stf:Crew"
PERSONNEL_FILE = "stf:PersonnelFile"
# The attributes a caller without markings cannot see, per type.
HIDDEN = {
    EMPLOYEE: ("badge_code", "birth_date", "email", "pay_grade", "salary", "weekly_hours"),
    BADGE: ("holder_name",),
    PERSONNEL_FILE: ("remark",),
}
HIDDEN_NAMES = tuple(sorted({n for names in HIDDEN.values() for n in names}))

# Policies for ``example_graph`` (staff) next to ``twin_graph`` (the twin, which uses no
# marking): every grant of ``example_graph`` comes with one of ``twin_graph`` so both stay
# readable. ``HOLDER`` grants every marking, ``PERSONAL_ONLY`` all but ``stf:pay`` (the salary
# stays hidden), ``READER`` none.
HOLDER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal, stf:pay]}
  - {entrypoint: app, graphs: [twin_graph], level: write}
no_project: {"*": write}
"""
PERSONAL_ONLY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal]}
  - {entrypoint: app, graphs: [twin_graph], level: write}
no_project: {"*": write}
"""
READER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph, twin_graph], level: write}
no_project: {"*": write}
"""


def twin_model() -> CompiledModel:
    """The compiled twin of ``staff`` 1.0.0."""
    outcome = compile_bundle("staff", LayeredSource(DirectorySource(STAFF_TWIN)))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def _employee(
    local: str, name: str, *, crew: str | None = None, badge: str | None = None, **attrs: Any
) -> dict[str, Any]:
    edges = []
    if crew is not None:
        edges.append({"rel": "stf:in_crew", "target": f"@{crew}"})
    if badge is not None:
        edges.append({"rel": "stf:holds_badge", "target": f"@{badge}"})
    return {
        "op": "create",
        "type": EMPLOYEE,
        "local": local,
        "attrs": {"name": name, **attrs},
        "edges": edges,
    }


# The data set: values a caller without markings must never meet (``SECRETS``) next to
# visible ones. ``e1`` and ``e4`` share a name (lookup facts), ``b1``'s only label is hidden,
# ``k1`` is a contractor whose e-mail is visible.
BUILD: list[dict[str, Any]] = [
    {"op": "create", "type": CREW, "local": "c1", "attrs": {"name": "Core crew"}},
    {"op": "create", "type": CREW, "local": "c2", "attrs": {"name": "Ops crew"}},
    {
        "op": "create",
        "type": BADGE,
        "local": "b1",
        "attrs": {"holder_name": "Ada Lovelace", "number": 1},
    },
    {
        "op": "create",
        "type": BADGE,
        "local": "b2",
        "attrs": {"holder_name": "Bo Brick", "number": 2},
    },
    {"op": "create", "type": BADGE, "local": "b3", "attrs": {"number": 3}},
    _employee(
        "e1",
        "Ada",
        crew="c1",
        badge="b1",
        phone="+1 555 0101",
        email="ada@private.example",
        birth_date="1990-01-01",
        pay_grade="senior",
        salary="91234.50",
        weekly_hours=30,
        badge_code="ADA-77",
        shift="day",
    ),
    _employee(
        "e2",
        "Bo",
        crew="c1",
        badge="b2",
        email="bo@private.example",
        pay_grade="junior",
        weekly_hours=40,
        shift="night",
    ),
    _employee("e3", "Cy", crew="c2"),
    _employee("e4", "Ada", crew="c2", badge="b3", weekly_hours=10),
    {
        "op": "create",
        "type": CONTRACTOR,
        "local": "k1",
        "attrs": {"name": "Dee", "email": "dee@agency.example", "agency": "Temps"},
        "edges": [{"rel": "stf:in_crew", "target": "@c1"}],
    },
    {"op": "create", "type": CONTRACTOR, "local": "k2", "attrs": {"name": "Eve"}},
]
# Later transactions, each with a visible change (so the twin gets the same transactions): a
# mixed update, an update that sets one visible and unsets one hidden attribute.
CHANGES: list[dict[str, Any]] = [
    {
        "reason": "new contact",
        "ops": [
            {
                "op": "update",
                "id": "@e1",
                "set": {"phone": "+1 555 0199", "email": "ada@new.example", "weekly_hours": 35},
            }
        ],
    },
    {
        "reason": "regrade",
        "ops": [{"op": "update", "id": "@e2", "set": {"shift": "day"}, "unset": ["pay_grade"]}],
    },
]
# A change of a hidden attribute only: a revision the twin does not have (a documented
# residual, ``rev`` and ``changed: []``), so it is never part of the oracle's data.
HIDDEN_ONLY: dict[str, Any] = {
    "reason": "more hours",
    "ops": [{"op": "update", "id": "EMP-0004", "set": {"weekly_hours": 12}}],
}
SECRETS = (
    "ada@private.example",
    "ada@new.example",
    "bo@private.example",
    "1990-01-01",
    "senior",
    "junior",
    "91234.50",
    "ADA-77",
    "Ada Lovelace",
    "Bo Brick",
)
LOCALS = ("c1", "c2", "b1", "b2", "b3", "e1", "e2", "e3", "e4", "k1", "k2")


def without_hidden(op: Mapping[str, Any]) -> dict[str, Any]:
    """``op`` as it is written to the twin: without the values of hidden attributes."""
    found = dict(op)
    hidden = HIDDEN.get(str(op.get("type")), ())
    if op.get("op") == "create":
        found["attrs"] = {k: v for k, v in op["attrs"].items() if k not in hidden}
    if op.get("op") == "update":  # every update of the data set is of an employee
        hidden = HIDDEN[EMPLOYEE]
        if "set" in op:
            found["set"] = {k: v for k, v in op["set"].items() if k not in hidden}
        if "unset" in op:
            found["unset"] = [n for n in op["unset"] if n not in hidden]
        found = {k: v for k, v in found.items() if v != [] and v != {}}
    return found


def documents(*, twin: bool = False) -> list[dict[str, Any]]:
    """The data set as changeset documents (the first creates everything); ``twin``: without
    hidden values."""
    docs = [{"reason": "build", "ops": BUILD}, *CHANGES]
    if not twin:
        return docs
    found = []
    for doc in docs:
        ops = [without_hidden(op) for op in doc["ops"]]
        found.append({**doc, "ops": ops})
    return found


def recomputed(entity: Mapping[str, Any]) -> str:
    """The content hash of an entity as shown (``--full``), computed from what is shown."""
    projection = content_projection(
        type=entity["type"],
        record_status=entity["record_status"],
        alias=entity["alias"],
        attrs=entity["attrs"],
        edges=tuple(EdgeRecord.from_json(e) for e in entity["edges"]),
        valid_from=entity["valid_from"],
        valid_to=entity["valid_to"],
    )
    return content_hash(projection)


# Terms a caller without markings must never meet: the hidden attributes (``email`` stays out:
# a contractor's e-mail is visible), the enum and datatype only they use, the rules and the
# guard that read them, the markings.
HIDDEN_TERMS = (
    *(n for n in HIDDEN_NAMES if n != "email"),
    "PayGrade",
    "BadgeCode",
    "employee_contact",
    "crew_hours",
    "badge_known",
    "stf:personal",
    "stf:pay",
)


def leaks(answer: object, tokens: Iterable[str] = (*SECRETS, *HIDDEN_TERMS)) -> list[str]:
    """The hidden values and terms that appear anywhere in ``answer`` (its JSON text)."""
    text = json.dumps(answer, sort_keys=True, default=str)
    return [token for token in tokens if token in text]


@dataclass(frozen=True)
class Answer:
    """What a caller gets: the data (or error code, message and details) as JSON."""

    ok: bool
    body: Any

    @classmethod
    def of(cls, call: Any) -> "Answer":
        from graph_cli.kernel.errors import GraphCliError

        try:
            return cls(True, _jsonable(call()))
        except GraphCliError as error:
            return cls(False, _jsonable([error.code, error.message, error.details]))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_json"):
        return _jsonable(value.to_json())
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_jsonable(v) for v in value]
    return json.loads(json.dumps(value, default=str))
