"""What the policy compiler needs of a graph's model, and what it finds out (contracts B6.13).

Access control imports only the kernel, so the compiler never sees a compiled ontology: the
caller condenses a graph's pinned model into a ``TermModel`` (per concrete or abstract type:
its attributes, the relations whose domain covers it, whether it is extended or a stored
file) and passes it through ``compile_policy(models=...)``. What the compiler finds is
collected, never raised, because a later release may add the term: the caller warns.

- ``UnknownWriteType``: a type of a ``writes`` grant the graph's model does not have;
- ``UnknownTerm``: an attribute, relation or lifecycle attribute a ``writes`` grant or a
  term deny rule names that the type does not have (nobody can write it: fail closed);
- ``UnusableWrite``: a ``writes`` entry no write can satisfy (``update`` of a lifecycle
  attribute; a ``create`` list that leaves out an attribute a create must give).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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
