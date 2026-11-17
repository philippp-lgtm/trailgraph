"""The generated entity card: what is relevant about one entity, in one bounded answer (T17).

``generated_spec`` derives a ``ViewSpec`` from the ontology (the class of the entity), ``render``
turns the spec, one entity revision, the reader's snapshot and model into a ``ViewModel`` and
``ViewModel.to_json`` is the format-neutral answer (``graph_cli.view_model/1``): a ``view`` and an
``entity`` header and a list of ``sections``, each with a ``kind``. A consumer ignores a kind it
does not know, so authored views (stage 2) can add kinds and read the same document. The four kinds
of the generated card:

- ``fields``: the label, the lifecycle state, required and unique attributes with their values;
  every other attribute that has a value is only named in ``other``.
- ``relations``: one group per (relation, direction) with the count, the first few neighbours by
  name and, when the neighbours share one lifecycle attribute, the count per state.
- ``files``: the stored files the entity was taken from (``files:from_file``) with their pages; for
  a file itself the variant ``cited_by``: who cites it and which pages.
- ``affordances``: what applies next, names only (``affordance_summary``).

Everything is built from the snapshot and the model that are passed in, so a limited reader's
filtered views limit the card too. Nothing is stored or cached. The work per card is bounded
(``LOAD_MAX`` neighbours per shown group; a larger group is sampled and says so) and so is the
answer (``BYTE_LIMIT``): fixed caps first, then one deterministic byte guard that trims in a
fixed order and sets ``cut``. Every cut is counted (``more``, ``more_groups``, ``other``,
``other_more``), nothing is dropped silently. No key has an empty value.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kernel.text import normalize_label
from graph_cli.ontology_layer.affordances import ACTIVE, CORE_PREFIX
from graph_cli.ontology_layer.model import AttributeDef, Bound, CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.lookup import label_text, text_values

VIEW_MODEL_SCHEMA: Final = "graph_cli.view_model/1"
CARD: Final = "card"
GENERATED: Final = "generated"
FILES_MODULE_RELATION: Final = "files:from_file"
FILE_CLASS: Final = "files:File"

BYTE_LIMIT: Final = 6144
FIELDS_MAX: Final = 8
VALUE_CHARS: Final = 100
GUARD_VALUE_CHARS: Final = 40
LIST_ITEMS: Final = 5
OTHER_MAX: Final = 12
ALT_LABELS_MAX: Final = 3
GROUPS_MAX: Final = 8
ITEMS_MAX: Final = 3
LABEL_CHARS: Final = 48
FILES_MAX: Final = 5
FILES_LOAD_MAX: Final = 50
LOAD_MAX: Final = 100
NAMES_MAX: Final = 5

Direction = Literal["out", "in"]
Json = dict[str, JsonValue]


# ---- the spec (what stage 2 will read from a published file) ---------------------------------


@dataclass(frozen=True, slots=True)
class FieldChoice:
    """A candidate for the ``fields`` section: an attribute and the role it plays on the card."""

    name: str
    role: str | None  # label, alt_label, state, or None


@dataclass(frozen=True, slots=True)
class FieldsSpec:
    choices: tuple[FieldChoice, ...]
    max_items: int = FIELDS_MAX
    value_chars: int = VALUE_CHARS
    list_items: int = LIST_ITEMS
    max_alt_labels: int = ALT_LABELS_MAX
    other_max: int = OTHER_MAX


@dataclass(frozen=True, slots=True)
class RelationsSpec:
    max_groups: int = GROUPS_MAX
    max_items: int = ITEMS_MAX
    load_max: int = LOAD_MAX
    label_chars: int = LABEL_CHARS
    order: Literal["label"] = "label"  # first by name: deterministic, cacheable


@dataclass(frozen=True, slots=True)
class FilesSpec:
    max_items: int = FILES_MAX
    load_max: int = FILES_LOAD_MAX
    label_chars: int = LABEL_CHARS


@dataclass(frozen=True, slots=True)
class AffordancesSpec:
    max_names: int = NAMES_MAX


type SectionSpec = FieldsSpec | RelationsSpec | FilesSpec | AffordancesSpec


@dataclass(frozen=True, slots=True)
class ViewSpec:
    name: str
    origin: str
    sections: tuple[SectionSpec, ...]
    byte_limit: int = BYTE_LIMIT


def _plain(attribute: AttributeDef) -> bool:
    """Whether an attribute can be a key field: not a label (shown by its role) and not the hash of
    a stored file (long, and of no use to a reader of the card)."""
    return attribute.role not in LABEL_ROLES and attribute.type != "blob"


def generated_spec(model: CompiledModel, class_curie: str) -> ViewSpec:
    """The card of a class, derived from the ontology (nothing is stored).

    Key fields in order: the main label and the alternative labels, the lifecycle attributes,
    the attributes marked ``required``, the attributes marked ``unique``. These are the only
    statements about importance that every ontology makes. Hidden labels and the hash of a stored
    file (type ``blob``) are never candidates.
    """
    cls = model.classes.get(class_curie)
    attributes = {} if cls is None else cls.attributes
    choices: list[FieldChoice] = []
    seen: set[str] = set()

    def add(name: str, role: str | None) -> None:
        if name not in seen:
            seen.add(name)
            choices.append(FieldChoice(name, role))

    for name in sorted(attributes):
        if attributes[name].role == "label":
            add(name, "label")
    for name in sorted(attributes):
        if attributes[name].role == "alt_label":
            add(name, "alt_label")
    for name in sorted(attributes):
        if attributes[name].kind == "lifecycle":
            add(name, "state")
    for name in sorted(attributes):
        if _plain(attributes[name]) and attributes[name].required:
            add(name, None)
    for name in sorted(attributes):
        if _plain(attributes[name]) and attributes[name].unique:
            add(name, None)
    return ViewSpec(
        CARD,
        GENERATED,
        (FieldsSpec(tuple(choices)), RelationsSpec(), FilesSpec(), AffordancesSpec()),
    )


# ---- the view model --------------------------------------------------------------------------


def _cut(text: str, limit: int) -> tuple[str, int | None]:
    """``text`` cut to ``limit`` characters with an ellipsis, and its length when it was cut."""
    if len(text) <= limit:
        return text, None
    return text[:limit] + "…", len(text)


@dataclass(frozen=True, slots=True)
class FieldItem:
    name: str
    role: str | None
    value: JsonValue
    chars: int | None = None  # the length of a string that was cut
    more: int = 0  # elements of a list that were cut

    def to_json(self) -> Json:
        found: Json = {"name": self.name}
        if self.role is not None:
            found["role"] = self.role
        found["value"] = self.value
        if self.chars is not None:
            found["chars"] = self.chars
        if self.more:
            found["more"] = self.more
        return found


@dataclass(frozen=True, slots=True)
class FieldsSection:
    items: tuple[FieldItem, ...]
    other: tuple[str, ...] = ()  # names of attributes with a value that are not shown
    other_more: int = 0  # names cut from ``other``

    def to_json(self) -> Json:
        found: Json = {"kind": "fields", "items": [i.to_json() for i in self.items]}
        if self.other:
            found["other"] = list(self.other)
        if self.other_more:
            found["other_more"] = self.other_more
        return found


@dataclass(frozen=True, slots=True)
class Neighbour:
    ref: str
    label: str | None = None
    type: str | None = None  # only when the group's neighbours have more than one type
    state: str | None = None
    record_status: str | None = None  # only when the neighbour is not active
    qualifiers: Mapping[str, JsonValue] = field(default_factory=dict)
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("label", self.label),
            ("type", self.type),
            ("state", self.state),
            ("record_status", self.record_status),
        ):
            if value is not None:
                found[key] = value
        if self.qualifiers:
            found["qualifiers"] = dict(self.qualifiers)
        return found


@dataclass(frozen=True, slots=True)
class Group:
    relation: str
    direction: Direction
    count: int
    items: tuple[Neighbour, ...]
    states: Mapping[str, int] = field(default_factory=dict)
    sampled: int = 0  # the number of edges items and states come from, when not all
    approx: bool = False  # ``count`` is the raw number of in-edges, inactive sources included
    missing: int = 0  # edges short of the relation's minimum

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found: Json = {"relation": self.relation, "direction": self.direction, "count": self.count}
        if self.states:
            found["states"] = dict(self.states)
        found["items"] = [i.to_json() for i in self.items]
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        if self.approx:
            found["approx"] = True
        if self.missing:
            found["missing"] = self.missing
        return found


@dataclass(frozen=True, slots=True)
class RelationsSection:
    groups: tuple[Group, ...]
    more_groups: tuple[str, ...] = ()  # relation names with edges that are not shown

    def to_json(self) -> Json:
        found: Json = {"kind": "relations", "groups": [g.to_json() for g in self.groups]}
        if self.more_groups:
            found["more_groups"] = list(self.more_groups)
        return found


@dataclass(frozen=True, slots=True)
class FileRef:
    ref: str
    title: str | None = None
    page: int | None = None
    last_page: int | None = None
    locator: str | None = None
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("title", self.title),
            ("page", self.page),
            ("last_page", self.last_page),
            ("locator", self.locator),
        ):
            if value is not None:
                found[key] = value
        return found


@dataclass(frozen=True, slots=True)
class FilesSection:
    items: tuple[FileRef, ...]
    more: int = 0
    sampled: int = 0

    def to_json(self) -> Json:
        found: Json = {"kind": "files", "items": [i.to_json() for i in self.items]}
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        return found


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    label: str | None = None
    page: int | None = None
    last_page: int | None = None
    entity: EntityId | None = None  # for the read signal; not in the JSON

    def to_json(self) -> Json:
        found: Json = {"ref": self.ref}
        for key, value in (
            ("label", self.label),
            ("page", self.page),
            ("last_page", self.last_page),
        ):
            if value is not None:
                found[key] = value
        return found


@dataclass(frozen=True, slots=True)
class CitedBySection:
    """The ``files`` section of a stored file: who cites it, which pages."""

    count: int
    items: tuple[Citation, ...]
    page_min: int | None = None
    page_max: int | None = None
    pages_cited: int = 0
    sampled: int = 0
    approx: bool = False

    @property
    def more(self) -> int:
        return max(0, self.count - len(self.items))

    def to_json(self) -> Json:
        found: Json = {"kind": "files", "variant": "cited_by", "count": self.count}
        if self.page_min is not None:
            found["page_min"] = self.page_min
        if self.page_max is not None:
            found["page_max"] = self.page_max
        if self.pages_cited:
            found["pages_cited"] = self.pages_cited
        found["items"] = [i.to_json() for i in self.items]
        if self.more:
            found["more"] = self.more
        if self.sampled:
            found["sampled"] = self.sampled
        if self.approx:
            found["approx"] = True
        return found


@dataclass(frozen=True, slots=True)
class AffordancesSection:
    summary: Mapping[str, JsonValue]  # ``affordance_summary``, plus ``detail``

    def to_json(self) -> Json:
        return {"kind": "affordances", **self.summary}


type Section = FieldsSection | RelationsSection | FilesSection | CitedBySection | AffordancesSection


@dataclass(frozen=True, slots=True)
class ViewModel:
    name: str
    origin: str
    entity: Mapping[str, JsonValue]
    sections: tuple[Section, ...]
    cut: bool = False

    def to_json(self) -> Json:
        """The compact answer; ``bytes`` is the size of this very document in UTF-8."""
        found: Json = {
            "view": {"name": self.name, "origin": self.origin},
            "entity": dict(self.entity),
            "sections": [s.to_json() for s in self.sections],
            "bytes": 0,
        }
        if self.cut:
            found["cut"] = True
        size = len(dumps(found))
        for _ in range(4):  # the number of digits can only grow, so this settles at once
            found["bytes"] = size
            again = len(dumps(found))
            if again == size:
                break
            size = again
        return found

    def size(self) -> int:
        return len(dumps(self.to_json()))

    def shown(self) -> tuple[EntityId, ...]:
        """The entities the card names besides its own: the neighbours, files and citations
        that are in the answer (after the byte guard), each once."""
        found: dict[EntityId, None] = {}
        for section in self.sections:
            if isinstance(section, RelationsSection):
                named = [i.entity for g in section.groups for i in g.items]
            elif isinstance(section, FilesSection | CitedBySection):
                named = [i.entity for i in section.items]
            else:
                continue
            found.update({e: None for e in named if e is not None})
        return tuple(found)


def dumps(data: JsonValue) -> bytes:
    """The compact UTF-8 JSON the byte limit is measured on."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()


# ---- the affordance summary ------------------------------------------------------------------


def affordance_summary(
    affordances: Mapping[str, JsonValue], spec: AffordancesSpec | None = None
) -> Json:
    """Names only of what ``get --affordances`` lists, at most ``max_names`` per list.

    ``transitions`` has one entry per lifecycle attribute: the states it may move to and the
    states it may not, each with the ids of the guards that fail (never the prose); ``actions``
    the names that apply and how many are blocked; ``functions`` the names. What is cut is
    counted (``actions_more``, ``functions_more``, ``transitions_more``).
    """
    limit = (spec or AffordancesSpec()).max_names
    found: Json = {}
    transitions: list[JsonValue] = []
    for raw in _list(affordances.get("transitions")):
        entry = _mapping(raw)
        allowed: list[JsonValue] = []
        blocked: list[JsonValue] = []
        for target_raw in _list(entry.get("to")):
            target = _mapping(target_raw)
            state = target.get("state")
            if not isinstance(state, str):
                continue
            if target.get("allowed") is True:
                allowed.append(state)
            else:
                guards = [
                    g
                    for guard in _list(target.get("blocked_by"))
                    if isinstance(g := _mapping(guard).get("guard"), str)
                ]
                blocked.append({"state": state, "by": guards[:limit]})
        if not allowed and not blocked:
            continue
        item: Json = {"attr": entry.get("attr")}
        if allowed:
            item["allowed"] = allowed[:limit]
        if len(allowed) > limit:
            item["allowed_more"] = len(allowed) - limit
        if blocked:
            item["blocked"] = blocked[:limit]
        if len(blocked) > limit:
            item["blocked_more"] = len(blocked) - limit
        transitions.append(item)
    if transitions:
        found["transitions"] = transitions[:limit]
        if len(transitions) > limit:
            found["transitions_more"] = len(transitions) - limit
    apply: list[JsonValue] = []
    blocked_actions = 0
    for raw in _list(affordances.get("actions")):
        entry = _mapping(raw)
        name = entry.get("action")
        if entry.get("applies") is True and isinstance(name, str):
            if name not in apply:
                apply.append(name)
        else:
            blocked_actions += 1
    actions: Json = {}
    if apply:
        actions["apply"] = apply[:limit]
    if blocked_actions:
        actions["blocked"] = blocked_actions
    if actions:
        found["actions"] = actions
    more = max(0, len(apply) - limit) + _count(affordances.get("actions_more"))
    if more:
        found["actions_more"] = more
    functions: list[JsonValue] = []
    for raw in _list(affordances.get("functions")):
        name = _mapping(raw).get("function")
        if isinstance(name, str) and name not in functions:
            functions.append(name)
    if functions:
        found["functions"] = functions[:limit]
    more = max(0, len(functions) - limit) + _count(affordances.get("functions_more"))
    if more:
        found["functions_more"] = more
    return found


def _list(value: JsonValue) -> list[JsonValue]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def _count(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# ---- render ----------------------------------------------------------------------------------


def render(
    spec: ViewSpec,
    entity: Revision,
    snapshot: Snapshot,
    model: CompiledModel,
    *,
    language: str | None = None,
    affordances: Mapping[str, JsonValue] | None = None,
    superseded_by: Sequence[str] = (),
    retracted_by: Sequence[str] = (),
    file_classes: Collection[str] | None = None,
) -> ViewModel:
    """The card of ``entity`` as ``snapshot`` and ``model`` show it (pure; reads only through
    ``snapshot``). ``language`` is the graph's language, the one a main label is shown in;
    ``affordances`` the object ``get --affordances`` builds (None: no affordances section content).
    ``file_classes`` are the classes that count as stored files (the family of ``files:File``);
    a limited reader's model may lack ``files:File`` itself although it sees a subclass, so the
    service passes the family of the stored model; None: the family of ``model``.
    """
    ref = entity.alias or str(entity.id)
    header: Json = {"id": str(entity.id)}
    if entity.alias is not None:
        header["alias"] = entity.alias
    header["type"] = str(entity.type)
    header["rev"] = entity.rev
    header["record_status"] = entity.record_status
    label = _main_label(model, entity, language)
    if label is not None:
        header["label"] = _cut(label, VALUE_CHARS)[0]
    if superseded_by:
        header["superseded_by"] = list(superseded_by)
    if retracted_by:
        header["retracted_by"] = list(retracted_by)
    is_file = str(entity.type) in (file_classes_of(model) if file_classes is None else file_classes)
    candidates, citations = _candidates(entity, snapshot, model, is_file)
    sections: list[Section] = []
    for section_spec in spec.sections:
        built: Section | None = None
        if isinstance(section_spec, FieldsSpec):
            built = _fields(section_spec, entity, model, language)
        elif isinstance(section_spec, RelationsSpec):
            built = _relations(section_spec, candidates, snapshot, model, language)
        elif isinstance(section_spec, FilesSpec):
            built = _files(section_spec, entity, is_file, citations, snapshot, model, language)
        elif isinstance(section_spec, AffordancesSpec):
            summary = affordance_summary(affordances or {}, section_spec)
            if summary:
                summary = {**summary, "detail": f"graph get {ref} --affordances"}
            built = AffordancesSection(summary)
        if built is not None:
            sections.append(built)
    return _guard(ViewModel(spec.name, spec.origin, header, tuple(sections)), spec.byte_limit)


def _main_label(model: CompiledModel, revision: Revision, language: str | None) -> str | None:
    if str(revision.type) not in model.classes:
        return None
    for attr, role in model.label_attributes(str(revision.type)):
        if role == "label":
            return label_text(revision.attrs.get(attr), (language,))
    return None


def _has_value(value: JsonValue) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _field_value(value: JsonValue, spec: FieldsSpec) -> tuple[JsonValue, int | None, int]:
    """The value as shown, the length of a string that was cut, the elements of a list cut."""
    if isinstance(value, str):
        text, chars = _cut(value, spec.value_chars)
        return text, chars, 0
    if isinstance(value, Mapping):
        text_raw = value.get("value")
        if isinstance(text_raw, str) and value.keys() == {"value", "lang"}:  # language-tagged
            text, chars = _cut(text_raw, spec.value_chars)
            return {"value": text, "lang": value["lang"]}, chars, 0
        return value, None, 0
    if isinstance(value, Sequence):
        shown: list[JsonValue] = []
        for element in value[: spec.list_items]:
            shown.append(_field_value(element, spec)[0])
        return shown, None, max(0, len(value) - spec.list_items)
    return value, None, 0


def _fields(
    spec: FieldsSpec, entity: Revision, model: CompiledModel, language: str | None
) -> FieldsSection:
    cls = model.classes.get(str(entity.type))
    known = {} if cls is None else cls.attributes
    items: list[FieldItem] = []
    accounted: set[str] = set()  # attributes shown or deliberately never named
    alt_names: list[str] = []
    alt_texts: list[str] = []
    for choice in spec.choices:
        value = entity.attrs.get(choice.name)
        if choice.name not in known or not _has_value(value):
            continue
        if choice.role == "alt_label":
            alt_names.append(choice.name)
            for alt, _tag in text_values(value):
                if alt not in alt_texts:
                    alt_texts.append(alt)
            continue
        if len(items) >= spec.max_items:
            continue
        if choice.role == "label":
            text = label_text(value, (language,))
            if text is None:
                continue
            shown, chars = _cut(text, spec.value_chars)
            items.append(FieldItem(choice.name, "label", shown, chars))
        else:
            shown_value, chars, more = _field_value(value, spec)
            items.append(FieldItem(choice.name, choice.role, shown_value, chars, more))
        accounted.add(choice.name)
    if alt_texts and len(items) < spec.max_items:
        # one item for all alternative labels, placed after the main label
        position = 1 if items and items[0].role == "label" else 0
        shown_texts: list[JsonValue] = [
            _cut(text, spec.value_chars)[0] for text in alt_texts[: spec.max_alt_labels]
        ]
        items.insert(
            position,
            FieldItem(
                alt_names[0],
                "alt_label",
                shown_texts,
                None,
                max(0, len(alt_texts) - spec.max_alt_labels),
            ),
        )
        accounted.update(alt_names)
    other = sorted(
        name
        for name, attribute in known.items()
        if name not in accounted
        and attribute.role != "hidden_label"
        and _has_value(entity.attrs.get(name))
    )
    return FieldsSection(
        tuple(items), tuple(other[: spec.other_max]), max(0, len(other) - spec.other_max)
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    relation: str
    direction: Direction
    edges: tuple[tuple[EntityId, EdgeRecord], ...]  # (neighbour, edge) in stored order


def _bound(model: CompiledModel, relation: str, direction: Direction) -> Bound:
    declared = model.relations.get(relation)
    if declared is None:
        return Bound(0, None)
    return declared.out if direction == "out" else declared.into


def file_classes_of(model: CompiledModel) -> frozenset[str]:
    """The classes of the ``files:File`` family of ``model`` (none without the files module)."""
    return frozenset(model.subtypes(FILE_CLASS)) if FILE_CLASS in model.classes else frozenset()


def _candidates(
    entity: Revision, snapshot: Snapshot, model: CompiledModel, is_file: bool
) -> tuple[list[_Candidate], list[_Candidate]]:
    """The (relation, direction) pairs that have at least one edge the reader sees, in card order;
    the second list is the ``files:from_file`` edges of a stored file (its citations)."""
    out: dict[str, list[tuple[EntityId, EdgeRecord]]] = {}
    for edge in entity.edges:
        name = str(edge.rel)
        if name.startswith(CORE_PREFIX) or name == FILES_MODULE_RELATION:
            continue
        out.setdefault(name, []).append((edge.target, edge))
    found = [_Candidate(name, "out", tuple(edges)) for name, edges in out.items()]
    citations: list[_Candidate] = []
    if is_file:  # read whatever the model of the reader says about the relation: its range is
        # ``files:File``, a class a limited reader may not see although it sees a subclass
        cited = snapshot.incoming(entity.id, TermRef(FILES_MODULE_RELATION))
        if cited:
            citations.append(_Candidate(FILES_MODULE_RELATION, "in", tuple(cited)))
    for name in sorted(model.relations):
        if name.startswith(CORE_PREFIX) or name == FILES_MODULE_RELATION:
            continue
        if not model.is_a_any(str(entity.type), model.relations[name].range):
            continue
        incoming = snapshot.incoming(entity.id, TermRef(name))
        if incoming:
            found.append(_Candidate(name, "in", tuple(incoming)))

    def order(candidate: _Candidate) -> tuple[bool, bool, bool, str]:
        bound = _bound(model, candidate.relation, candidate.direction)
        return (
            candidate.direction == "in",
            bound.max != 1,
            bound.min < 1,
            candidate.relation,
        )

    found.sort(key=order)
    return found, citations


def _label_key(label: str | None, entity: EntityId) -> tuple[bool, str, str]:
    return (label is None, "" if label is None else normalize_label(label), str(entity))


def _lifecycle_attribute(model: CompiledModel, revision: Revision) -> str | None:
    return model.lifecycle_attribute(str(revision.type))


def _state(model: CompiledModel, revision: Revision) -> str | None:
    attr = _lifecycle_attribute(model, revision)
    value = None if attr is None else revision.attrs.get(attr)
    return value if isinstance(value, str) else None


def _relations(
    spec: RelationsSpec,
    candidates: Sequence[_Candidate],
    snapshot: Snapshot,
    model: CompiledModel,
    language: str | None,
) -> RelationsSection | None:
    groups: list[Group] = []
    unshown: list[str] = []
    for position, candidate in enumerate(candidates):
        if len(groups) >= spec.max_groups:
            unshown.extend(c.relation for c in candidates[position:])
            break
        group = _group(spec, candidate, snapshot, model, language)
        if group is not None:
            groups.append(group)
    more: list[str] = []
    for name in unshown:
        if name not in more:
            more.append(name)
    if not groups and not more:
        return None
    return RelationsSection(tuple(groups), tuple(more))


def _group(
    spec: RelationsSpec,
    candidate: _Candidate,
    snapshot: Snapshot,
    model: CompiledModel,
    language: str | None,
) -> Group | None:
    edges = candidate.edges
    inbound = candidate.direction == "in"
    sample = edges[: spec.load_max]
    loaded = snapshot.get_many([neighbour for neighbour, _ in sample])
    rows: list[tuple[EntityId, EdgeRecord, Revision | None]] = []
    for neighbour, edge in sample:
        revision = loaded.get(neighbour)
        if inbound and (revision is None or revision.record_status != ACTIVE):
            continue  # an in-edge counts only while its source is active
        rows.append((neighbour, edge, revision))
    sampled = len(edges) > spec.load_max
    count = (len(edges) if sampled else len(rows)) if inbound else len(edges)
    if count == 0:
        return None
    labels = {n: None if r is None else _main_label(model, r, language) for n, _edge, r in rows}
    types = {str(r.type) for _n, _e, r in rows if r is not None}
    ordered = sorted(rows, key=lambda row: _label_key(labels[row[0]], row[0]))
    items = tuple(
        _neighbour(spec, model, n, edge, r, labels[n], len(types) > 1)
        for n, edge, r in ordered[: spec.max_items]
    )
    states: dict[str, int] = {}
    attrs = set()
    for _n, _e, r in rows:
        if r is None or r.record_status != ACTIVE:
            continue
        attrs.add(_lifecycle_attribute(model, r))
        state = _state(model, r)
        if state is not None:
            states[state] = states.get(state, 0) + 1
    if len(attrs) != 1 or None in attrs or count <= len(items):
        states = {}  # one lifecycle attribute, and only when some neighbours are not shown
    bound = _bound(model, candidate.relation, candidate.direction)
    return Group(
        candidate.relation,
        candidate.direction,
        count,
        items,
        dict(sorted(states.items())),
        spec.load_max if sampled else 0,
        inbound and sampled,
        max(0, bound.min - count),
    )


def _neighbour(
    spec: RelationsSpec,
    model: CompiledModel,
    neighbour: EntityId,
    edge: EdgeRecord,
    revision: Revision | None,
    label: str | None,
    with_type: bool,
) -> Neighbour:
    qualifiers = dict(edge.qualifiers)
    if revision is None:
        return Neighbour(str(neighbour), qualifiers=qualifiers, entity=neighbour)
    return Neighbour(
        revision.alias or str(revision.id),
        None if label is None else _cut(label, spec.label_chars)[0],
        str(revision.type) if with_type else None,
        _state(model, revision) if revision.record_status == ACTIVE else None,
        None if revision.record_status == ACTIVE else revision.record_status,
        qualifiers,
        revision.id,
    )


def _page(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _locator(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _files(
    spec: FilesSpec,
    entity: Revision,
    is_file: bool,
    citations: Sequence[_Candidate],
    snapshot: Snapshot,
    model: CompiledModel,
    language: str | None,
) -> FilesSection | CitedBySection | None:
    if is_file:
        return _cited_by(spec, citations, snapshot, model, language)
    edges = [(e.target, e) for e in entity.edges if str(e.rel) == FILES_MODULE_RELATION]
    if not edges:
        return None
    sample = edges[: spec.load_max]
    loaded = snapshot.get_many([target for target, _ in sample])
    rows: list[tuple[tuple[bool, str, str], int, int, str, FileRef]] = []
    for target, edge in sample:
        revision = loaded.get(target)
        title = None if revision is None else _main_label(model, revision, language)
        ref = FileRef(
            str(target) if revision is None else revision.alias or str(revision.id),
            None if title is None else _cut(title, spec.label_chars)[0],
            _page(edge.qualifiers.get("page")),
            _page(edge.qualifiers.get("last_page")),
            _locator(edge.qualifiers.get("locator")),
            target,
        )
        rows.append(
            (
                _label_key(title, target),
                ref.page or 0,
                ref.last_page or 0,
                ref.locator or "",
                ref,
            )
        )
    rows.sort(key=lambda row: row[:4])
    shown = tuple(row[4] for row in rows[: spec.max_items])
    return FilesSection(
        shown, max(0, len(edges) - len(shown)), spec.load_max if len(edges) > spec.load_max else 0
    )


def _merged_length(intervals: list[tuple[int, int]]) -> int:
    """The number of distinct pages the closed intervals cover (no page is enumerated)."""
    total = 0
    end = 0
    for low, high in sorted(intervals):
        low = max(low, end + 1)
        if high >= low:
            total += high - low + 1
            end = high
    return total


def _cited_by(
    spec: FilesSpec,
    citations: Sequence[_Candidate],
    snapshot: Snapshot,
    model: CompiledModel,
    language: str | None,
) -> CitedBySection | None:
    if not citations:
        return None
    edges = list(citations[0].edges)
    edges.sort(
        key=lambda pair: (
            _page(pair[1].qualifiers.get("page")) is None,
            _page(pair[1].qualifiers.get("page")) or 0,
            _page(pair[1].qualifiers.get("last_page")) or 0,
            str(pair[0]),
        )
    )
    load = spec.load_max
    sample = edges[:load]
    loaded = snapshot.get_many([source for source, _ in sample])
    rows = [
        (source, edge, revision)
        for source, edge in sample
        if (revision := loaded.get(source)) is not None and revision.record_status == ACTIVE
    ]
    sampled = len(edges) > load
    count = len(edges) if sampled else len(rows)
    if count == 0:
        return None
    # the page statistics: the active sources when all were read, else every edge (approximate)
    spans: list[tuple[int, int]] = []
    for _source, edge in edges if sampled else [(s, e) for s, e, _r in rows]:
        page = _page(edge.qualifiers.get("page"))
        if page is not None:
            last = _page(edge.qualifiers.get("last_page"))
            spans.append((page, max(page, last or page)))
    labelled = [(s, e, r, _main_label(model, r, language)) for s, e, r in rows]
    labelled.sort(
        key=lambda row: (
            _page(row[1].qualifiers.get("page")) is None,
            _page(row[1].qualifiers.get("page")) or 0,
            _label_key(row[3], row[0]),
        )
    )
    items = tuple(
        Citation(
            revision.alias or str(revision.id),
            None if label is None else _cut(label, spec.label_chars)[0],
            _page(edge.qualifiers.get("page")),
            _page(edge.qualifiers.get("last_page")),
            revision.id,
        )
        for _s, edge, revision, label in labelled[: spec.max_items]
    )
    return CitedBySection(
        count,
        items,
        min((low for low, _ in spans), default=None),
        max((high for _, high in spans), default=None),
        _merged_length(spans),
        load if sampled else 0,
        sampled,
    )


# ---- the byte guard --------------------------------------------------------------------------


def _guard(card: ViewModel, limit: int) -> ViewModel:
    """Trim in a fixed order until the JSON fits ``limit``; header, label, state and the
    affordance summary are never trimmed. The same input gives the same bytes."""
    if card.size() <= limit:
        return card
    card = replace(card, cut=True)

    def fits() -> bool:
        return card.size() <= limit

    def swap(kind: type, new: Section) -> None:
        nonlocal card
        card = replace(
            card, sections=tuple(new if isinstance(s, kind) else s for s in card.sections)
        )

    relations = next((s for s in card.sections if isinstance(s, RelationsSection)), None)
    if relations is not None:
        groups = list(relations.groups)
        for index in reversed(range(len(groups))):  # items per group to 1, last group first
            if len(groups[index].items) > 1:
                groups[index] = replace(groups[index], items=groups[index].items[:1])
                swap(RelationsSection, replace(relations, groups=tuple(groups)))
                if fits():
                    return card
        moved: list[str] = []
        while groups and not fits():  # then the trailing groups move to ``more_groups``
            dropped = groups.pop()
            if dropped.relation not in moved:
                moved.insert(0, dropped.relation)
            names = [*moved, *(n for n in relations.more_groups if n not in moved)]
            swap(
                RelationsSection, replace(relations, groups=tuple(groups), more_groups=tuple(names))
            )
        if fits():
            return card
    fields = next((s for s in card.sections if isinstance(s, FieldsSection)), None)
    if fields is not None:
        shortened = tuple(_shorten(item) for item in fields.items)  # then values to 40 characters
        swap(FieldsSection, replace(fields, items=shortened))
        if fits():
            return card
        items = list(shortened)
        moved_names: list[str] = []
        while not fits():  # then trailing fields move to ``other``
            last = next(
                (i for i in reversed(range(len(items))) if items[i].role not in ("label", "state")),
                None,
            )
            if last is None:
                break
            moved_names.append(items.pop(last).name)
            every = sorted({*fields.other, *moved_names})
            swap(
                FieldsSection,
                replace(
                    fields,
                    items=tuple(items),
                    other=tuple(every[:OTHER_MAX]),
                    other_more=fields.other_more + max(0, len(every) - OTHER_MAX),
                ),
            )
        if fits():
            return card
    for kind in (FilesSection, CitedBySection):  # last: file references, to one, then none
        section = next((s for s in card.sections if isinstance(s, kind)), None)
        if section is None:
            continue
        for keep in (1, 0):
            if len(section.items) > keep:
                if isinstance(section, FilesSection):
                    trimmed: Section = replace(
                        section,
                        items=section.items[:keep],
                        more=section.more + len(section.items) - keep,
                    )
                else:
                    trimmed = replace(section, items=section.items[:keep])
                swap(kind, trimmed)
                if fits():
                    return card
    return card


def _shorten(item: FieldItem) -> FieldItem:
    if item.role in ("label", "state") or not isinstance(item.value, str):
        return item
    if len(item.value) <= GUARD_VALUE_CHARS + 1:
        return item
    text, _ = _cut(item.value.removesuffix("…"), GUARD_VALUE_CHARS)
    return replace(item, value=text, chars=item.chars or len(item.value))
