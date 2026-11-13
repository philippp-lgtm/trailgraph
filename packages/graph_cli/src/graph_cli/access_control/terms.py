"""What the policy compiler needs of a graph's model, and what it finds out (contracts B6.13).

Access control imports only the kernel, so the compiler never sees a compiled ontology: the
caller condenses a graph's pinned model into a ``TermModel`` (per concrete or abstract type:
its attributes, the relations whose domain covers it, whether it is extended or a stored
file) and passes it through ``compile_policy(models=...)``. What the compiler finds is
collected, never raised, because a later release may add the term: the caller warns.

- ``UnknownWriteType``: a type of a ``writes`` grant the graph's model does not have;
- ``UnknownReadType``: a type (or ``prefix:*`` that matches none) of a ``reads`` grant the
  graph's model does not have;
- ``UnknownTerm``: an attribute, relation or lifecycle attribute a ``writes`` grant or a
  term deny rule names that the type does not have (nobody can write it: fail closed);
- ``UnusableWrite``: a ``writes`` entry no write can satisfy (``update`` of a lifecycle
  attribute; a ``create`` list that leaves out an attribute a create must give).

``term_resource`` builds the resource a write of one term is decided on; ``named_terms``
and ``named_read_types`` read back which types and terms, and which types a read rule
names, the installed documents name for one graph (an ontology release checks them against
the new model).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.access_control.documents import CURIE_RE, NAME_RE, glob, rules, strings
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId


@dataclass(frozen=True, slots=True)
class AttributeTerms:
    lifecycle: bool  # a lifecycle attribute: moved by a transition, never by an update
    # A create has to give it: required without a default, a lifecycle with several
    # initial states (a single initial state is filled in), or, for a stored file, one that
    # ``graph file add`` sets itself.
    must_give: bool


@dataclass(frozen=True, slots=True)
class TypeTerms:
    extended: bool  # abstract or has subclasses: an exact rule would miss their entities
    file: bool  # is_a the stored-file class
    attributes: Mapping[str, AttributeTerms]  # effective, including inherited
    relations: frozenset[str]  # the relations whose domain covers the type


@dataclass(frozen=True, slots=True)
class TermModel:
    types: Mapping[str, TypeTerms]


@dataclass(frozen=True, slots=True, order=True)
class UnknownWriteType:
    graph: GraphId
    type: str


@dataclass(frozen=True, slots=True, order=True)
class UnknownReadType:
    graph: GraphId
    type: str  # a type CURIE or a ``prefix:*`` glob


@dataclass(frozen=True, slots=True, order=True)
class UnknownTerm:
    graph: GraphId
    type: str
    term: str
    kind: str  # attribute | relation | lifecycle


@dataclass(frozen=True, slots=True, order=True)
class UnusableWrite:
    graph: GraphId
    type: str
    verb: str  # create | update
    attributes: tuple[
        str, ...
    ]  # sorted: the lifecycle attributes of an update, the missing of a create
    file: bool  # the type is a stored file: ``graph file add`` sets some attributes itself


def term_resource(graph: GraphId | str, type_curie: str, kind: str, term: str) -> str | None:
    """``graph:<graph>/type:<type>/attr:<name>`` (``rel`` for a relation), the resource a write
    of one term is decided on (B6.9); ``None`` for a term outside the grammar of resources,
    which no rule can name and validation of the write reports."""
    pattern = NAME_RE if kind == "attr" else CURIE_RE
    if kind not in ("attr", "rel") or not pattern.match(term):
        return None
    return f"graph:{graph}/type:{type_curie}/{kind}:{term}"


_GRANT_RULE_RE: Final = re.compile(
    r"^grant\.[0-9]+\.(create|update|link|edge|unlink|transition|supersede|retract|alias)$"
)


@dataclass(frozen=True, slots=True, order=True)
class NamedTerm:
    """A type, or a term of a type, that an installed rule names for one graph."""

    type: str
    kind: str  # "type" (the type itself) | attribute | relation | lifecycle
    term: str  # "" for the type itself
    grant: bool  # named by a write grant; otherwise by a term deny rule of the graph layer


def named_terms(
    documents: Sequence[Mapping[str, JsonValue]], graph: GraphId
) -> tuple[NamedTerm, ...]:
    """What the write grants (``grant.<n>.<verb>`` rules of the installation layer) and the term
    deny rules (a resource below a type, in the graph layer) of ``documents`` name under
    ``graph:<graph>/type:``, sorted and unique. A glob below a type (``attr:*``) names no
    single term; the type of a rule that has one is still named."""
    prefix = f"graph:{graph}/type:"
    found: set[NamedTerm] = set()
    for document in documents:
        layer = document.get("layer")
        for rule in rules(document):
            rule_id = rule.get("id")
            grant = layer == "installation" and bool(
                isinstance(rule_id, str) and _GRANT_RULE_RE.match(rule_id)
            )
            if not grant and layer != "graph":
                continue
            lifecycle = isinstance(rule_id, str) and rule_id.endswith(".transition")
            for resource in strings(rule, "resource"):
                if not resource.startswith(prefix):
                    continue
                curie, _, below = resource[len(prefix) :].partition("/")
                if not CURIE_RE.match(curie):
                    continue  # a glob of types
                if not below:
                    if grant:
                        found.add(NamedTerm(curie, "type", "", True))
                    continue
                kind, _, term = below.partition(":")
                if kind not in ("attr", "rel"):
                    continue
                found.add(NamedTerm(curie, "type", "", grant))
                if term != "*":
                    named = (
                        "relation" if kind == "rel" else "lifecycle" if lifecycle else "attribute"
                    )
                    found.add(NamedTerm(curie, named, term, grant))
    return tuple(sorted(found))


_READS_RULE_RE: Final = re.compile(r"^grant\.[0-9]+\.reads$")


@dataclass(frozen=True, slots=True, order=True)
class NamedReadType:
    """A concrete type an installed rule decides the visibility of in one graph."""

    type: str
    grant: bool  # named by a ``reads`` grant (an allow list); otherwise hidden by a deny rule


def named_read_types(
    documents: Sequence[Mapping[str, JsonValue]], graph: GraphId
) -> tuple[NamedReadType, ...]:
    """The exact types the ``grant.<n>.reads`` rules (installation layer) and the deny rules of
    the graph layer that cover ``read.get`` name under ``graph:<graph>/type:``, sorted and
    unique. A glob of types names no single type and is left out."""
    prefix = f"graph:{graph}/type:"
    found: set[NamedReadType] = set()
    for document in documents:
        layer = document.get("layer")
        for rule in rules(document):
            rule_id = rule.get("id")
            grant = (
                layer == "installation"
                and isinstance(rule_id, str)
                and _READS_RULE_RE.match(rule_id) is not None
            )
            hidden = layer == "graph" and rule.get("effect") == "deny"
            if not (grant or hidden) or not any(
                glob(pattern, "read.get") for pattern in strings(rule, "action")
            ):
                continue
            for resource in strings(rule, "resource"):
                if resource.startswith(prefix) and CURIE_RE.match(resource[len(prefix) :]):
                    found.add(NamedReadType(resource[len(prefix) :], grant))
    return tuple(sorted(found))
