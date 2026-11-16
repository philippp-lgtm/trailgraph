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
  attribute; a ``create`` list that leaves out an attribute a create must give);
- ``HiddenRangeCreate``: a ``create`` of a type with a required relation whose range the
  same grant's ``reads`` hide entirely, so a caller with only that grant cannot create it;
- ``UnknownMarking``: a marking a ``markings`` grant names that the graph's model does not
  declare (nobody holds a marking that does not exist yet: the grant has no effect);
- ``UnusableMarkedWrite``: a ``writes`` entry that names, or covers with ``true``, a marked
  attribute the same grant does not hold the markings of: a write right no caller with only
  this grant can use (rights add up, so another grant may supply the marking).

``marking_resource`` builds the resource a marking is held on (``marking:<graph>/<curie>``) and
``named_markings`` reads back which markings the installed documents grant for one graph.
``term_resource`` builds the resource a write of one term is decided on; ``named_terms``
and ``named_read_types`` read back which types and terms, and which types a read rule
names, the installed documents name for one graph (an ontology release checks them against
the new model); ``rule_entries`` lists every rule's resource patterns, effect and actions, so a
release that renames terms can tell which deny rules would stop applying (migration plan M8).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.access_control.documents import (
    CURIE_RE,
    INSTALLATION_LAYER,
    MARKING_RE,
    MARKING_RESOURCE_PREFIX,
    NAME_RE,
    glob,
    layer_of,
    rules,
    strings,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId


@dataclass(frozen=True, slots=True)
class AttributeTerms:
    lifecycle: bool  # a lifecycle attribute: moved by a transition, never by an update
    # A create has to give it: required without a default, a lifecycle with several
    # initial states (a single initial state is filled in), or, for a stored file, one that
    # ``graph file add`` sets itself.
    must_give: bool
    # The markings a caller must hold to see (and write) the attribute, sorted CURIEs; ``()``
    # for an unmarked attribute.
    markings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeTerms:
    extended: bool  # abstract or has subclasses: an exact rule would miss their entities
    file: bool  # is_a the stored-file class
    attributes: Mapping[str, AttributeTerms]  # effective, including inherited
    relations: frozenset[str]  # the relations whose domain covers the type
    # The required relations of the type (outgoing cardinality minimum >= 1) with a closed
    # range: relation -> the concrete types of its range, subtypes expanded, sorted. A relation
    # with an open range (any class) is left out: no reads list can hide all of it.
    required_ranges: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class TermModel:
    types: Mapping[str, TypeTerms]
    # The markings the model declares (CURIEs): what a ``markings`` grant may name.
    markings: frozenset[str] = frozenset()


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


@dataclass(frozen=True, slots=True, order=True)
class HiddenRangeCreate:
    graph: GraphId
    type: str  # the type a ``create`` is granted for
    relation: str  # a required relation of the type
    range: tuple[str, ...]  # the concrete types of its range, all outside the grant's ``reads``


@dataclass(frozen=True, slots=True, order=True)
class UnknownMarking:
    graph: GraphId
    marking: str


@dataclass(frozen=True, slots=True, order=True)
class UnusableMarkedWrite:
    """The write grant lists (or, with ``true``, covers) marked ``attributes`` of ``type`` without
    holding ``markings``, the markings those attributes need that the grant lacks (sorted).
    ``blocks_create``: the attributes are required without a default, so no create succeeds;
    otherwise only the writes of these attributes are dead."""

    graph: GraphId
    type: str
    verb: str  # create | update
    attributes: tuple[str, ...]  # sorted
    markings: tuple[str, ...]  # sorted
    blocks_create: bool


def marking_resource(graph: GraphId | str, marking: str) -> str | None:
    """``marking:<graph>/<curie>``, the resource a marking is held on (B6.9); ``None`` for a
    string that is no marking CURIE, which no rule can name (fail closed)."""
    if not MARKING_RE.match(marking):
        return None
    return f"{MARKING_RESOURCE_PREFIX}{graph}/{marking}"


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
_MARKINGS_RULE_RE: Final = re.compile(r"^grant\.[0-9]+\.markings$")


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


def named_markings(documents: Sequence[Mapping[str, JsonValue]], graph: GraphId) -> tuple[str, ...]:
    """The markings the ``grant.<n>.markings`` rules (installation layer) of ``documents`` grant
    for ``graph`` (their ``marking:<graph>/<curie>`` resources), sorted and unique."""
    prefix = f"{MARKING_RESOURCE_PREFIX}{graph}/"
    found: set[str] = set()
    for document in documents:
        if layer_of(document) != INSTALLATION_LAYER:
            continue
        for rule in rules(document):
            rule_id = rule.get("id")
            if not isinstance(rule_id, str) or _MARKINGS_RULE_RE.match(rule_id) is None:
                continue
            for resource in strings(rule, "resource"):
                if resource.startswith(prefix) and MARKING_RE.match(resource[len(prefix) :]):
                    found.add(resource[len(prefix) :])
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True, order=True)
class RuleEntry:
    """One resource pattern of one compiled rule: where it sits (``layer``, ``rule`` id), its
    ``effect`` (``allow`` or ``deny``), the pattern as compiled and the action patterns of the
    rule."""

    layer: str
    rule: str
    effect: str
    resource: str
    actions: tuple[str, ...]


def rule_entries(documents: Sequence[Mapping[str, JsonValue]]) -> tuple[RuleEntry, ...]:
    """Every (rule, resource pattern) of ``documents``, sorted and unique. Pure: a release looks
    for the rules that name a type or term it renames (``unless`` conditions are not rules'
    own resources and are left out)."""
    found: set[RuleEntry] = set()
    for document in documents:
        layer = layer_of(document)
        for rule in rules(document):
            rule_id, effect = rule.get("id"), rule.get("effect")
            if not isinstance(rule_id, str) or not isinstance(effect, str):
                continue
            actions = tuple(sorted(strings(rule, "action")))
            for resource in strings(rule, "resource"):
                found.add(RuleEntry(layer, rule_id, effect, resource, actions))
    return tuple(sorted(found))
