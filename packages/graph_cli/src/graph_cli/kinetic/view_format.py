"""Authored format ``graph_cli.view/1``: typed view model and its parser.

A view is a named, typed read of a graph that answers ``graph_cli.view_model/1``. It is
either an *entity view* (``takes`` names the class it takes; called with a reference) or a
*set view* (no ``takes``; called without one). Its sections come in seven kinds: the card's
four (``fields``, ``relations``, ``files``, ``affordances``, now configurable), ``neighbors``
(one relation of the entity as rows), ``list`` (a type listing as rows, set views) and
``rows`` (the result of a published function).

Parsing checks the shape of one file (keys, value types, grammars, lengths, which keys and
section kinds the flavour allows); everything that needs the ontology or the function catalog
is checked by ``view_compile``. Every defect of a file becomes a ``Problem`` instead of an
exception, so one run reports all of them. A definition also reads back from its JSON form
(``ViewDef.from_json``), which is how a published catalog is checked again against another
model.

Placeholders are YAML strings that are exactly ``$<name>``: a declared parameter of the view or
``$entity``, the entity an entity view takes. They stand where a whole value goes: after the
``=`` of a ``where`` entry and as a value of ``args`` of a ``rows`` section.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Final, Literal

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import EMPTY
from graph_cli.kernel.json_access import (
    JsonShapeError,
    as_list,
    as_object,
    opt_int,
    opt_str,
    req_bool,
    req_int,
    req_list,
    req_obj,
    req_str,
    str_list,
)
from graph_cli.kernel.yaml_strict import (
    YamlError,
    YamlMap,
    YamlNode,
    YamlScalar,
    YamlSeq,
    load_strict,
)
from graph_cli.kinetic.format import (
    FILE_SUFFIX,
    MAX_SUMMARY,
    MAX_USE_WHEN,
    MODULE_REF_RE,
    NAME_RE,
    STATUSES,
    ExampleDef,
    parse_examples,
)
from graph_cli.kinetic.params import (
    PARAM_RE,
    ParamDef,
    Parser,
    Problem,
    is_null,
)
from graph_cli.ontology_layer.vocabulary import LOWER_NAME_RE

VIEW_SCHEMA: Final = "graph_cli.view/1"
CATALOG_SCHEMA: Final = "graph_cli.view_catalog/1"

RESERVED_NAME: Final = "card"  # the generated card; no authored view takes it
ENTITY: Final = "entity"  # the placeholder of the entity an entity view takes
DIRECTIONS: Final = ("out", "in")
ALL: Final = "all"

# Limits (plan V2). ``ENTITY_MAX_BYTES`` is the card's hard limit (``card.BYTE_LIMIT``; a test
# keeps them equal): this module stays below the card renderer in the import order.
ENTITY_MAX_BYTES: Final = 6144
MIN_BYTES: Final = 1024
PAGE_BYTES_MAX: Final = 262_144
MIN_SECTIONS: Final = 1
MAX_SECTIONS: Final = 32
MAX_TITLE: Final = 100
MAX_FIELD_NAMES: Final = 64  # attributes a row lists
MAX_TYPES: Final = 16  # classes a ``list`` section lists
MAX_CLAUSES: Final = 8  # where entries and sort keys per section
MAX_CLAUSE_CHARS: Final = 200
MAX_ARGS: Final = 32
MAX_ROW_RELATIONS: Final = 16
VALUE_CHARS_DEFAULT: Final = 100
VALUE_CHARS_MAX: Final = 2000
FIELDS_MAX_DEFAULT: Final = 8
FIELDS_MAX_MAX: Final = 32
GROUPS_MAX_DEFAULT: Final = 8
GROUPS_MAX_MAX: Final = 8
ITEMS_MAX_DEFAULT: Final = 3
ITEMS_MAX_MAX: Final = 10
NEIGHBORS_MAX_DEFAULT: Final = 10
NEIGHBORS_MAX_MAX: Final = 50
ROWS_MAX_DEFAULT: Final = 10
ROWS_MAX_MAX: Final = 50
ROW_RELATION_MAX_DEFAULT: Final = 50
ROW_RELATION_MAX_MAX: Final = 500

SECTION_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TERM_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}(:[A-Za-z][A-Za-z0-9_]{0,63})?$")
PLACEHOLDER_RE: Final = re.compile(r"^\$" + PARAM_RE.pattern[1:])
TOKEN_RE: Final = re.compile(r"\$[a-z][a-z0-9_]{0,31}")  # a placeholder anywhere in a text

ENTITY_KINDS: Final = ("fields", "relations", "files", "affordances", "neighbors", "rows")
SET_KINDS: Final = ("list", "rows")
KINDS: Final = ("fields", "relations", "files", "affordances", "neighbors", "list", "rows")

_TOP_KEYS: Final = (
    "schema",
    "name",
    "module",
    "summary",
    "use_when",
    "status",
    "replaced_by",
    "takes",
    "default",
    "params",
    "max_bytes",
    "page_bytes",
    "sections",
    "examples",
)
_COMMON_KEYS: Final = ("kind", "id", "title")
_SECTION_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "fields": (*_COMMON_KEYS, "fields", "value_chars", "max"),
    "relations": (*_COMMON_KEYS, "relations", "max_groups", "max_items"),
    "files": _COMMON_KEYS,
    "affordances": _COMMON_KEYS,
    "neighbors": (
        *_COMMON_KEYS,
        "relation",
        "direction",
        "where",
        "order",
        "fields",
        "relations",
        "max",
    ),
    "list": (*_COMMON_KEYS, "type", "where", "order", "fields", "relations"),
    "rows": (*_COMMON_KEYS, "function", "args", "max"),
}
_ROW_RELATION_KEYS: Final = ("relation", "direction", "fields", "where", "order", "max")

type FieldSel = tuple[str, ...] | Literal["all"] | None
"""A choice of attributes: the listed names, ``all`` or (left out) the section's default."""


# ---- the typed model --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationRef:
    """A relation of a ``relations`` section and the direction it is read in."""

    relation: str
    direction: str = "out"

    def to_json(self) -> dict[str, JsonValue]:
        return {"relation": self.relation, "direction": self.direction}


@dataclass(frozen=True, slots=True)
class RowRelation:
    """One per-row neighbour list of a ``list`` or ``neighbors`` section; ``relation`` is
    ``None`` for ``all`` (every out-relation the reader sees, keys only)."""

    relation: str | None = None
    direction: str = "out"
    fields: FieldSel = None
    where: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    max: int = ROW_RELATION_MAX_DEFAULT

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "relation": self.relation,
            "direction": self.direction,
            "fields": fields_json(self.fields),
            "where": list(self.where),
            "order": list(self.order),
            "max": self.max,
        }


@dataclass(frozen=True, slots=True)
class FieldsSection:
    id: str = "fields"
    title: str | None = None
    fields: FieldSel = None  # None: the generated choice of the card
    value_chars: int = VALUE_CHARS_DEFAULT
    max: int = FIELDS_MAX_DEFAULT
    kind: ClassVar[str] = "fields"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "fields": fields_json(self.fields),
            "value_chars": self.value_chars,
            "max": self.max,
        }


@dataclass(frozen=True, slots=True)
class RelationsSection:
    id: str = "relations"
    title: str | None = None
    relations: tuple[RelationRef, ...] | None = None  # None: every relation, the card's rule
    max_groups: int = GROUPS_MAX_DEFAULT
    max_items: int = ITEMS_MAX_DEFAULT
    kind: ClassVar[str] = "relations"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "relations": None if self.relations is None else [r.to_json() for r in self.relations],
            "max_groups": self.max_groups,
            "max_items": self.max_items,
        }


@dataclass(frozen=True, slots=True)
class FilesSection:
    id: str = "files"
    title: str | None = None
    kind: ClassVar[str] = "files"

    def to_json(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "id": self.id, "title": self.title}


@dataclass(frozen=True, slots=True)
class AffordancesSection:
    id: str = "affordances"
    title: str | None = None
    kind: ClassVar[str] = "affordances"

    def to_json(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "id": self.id, "title": self.title}


@dataclass(frozen=True, slots=True)
class NeighborsSection:
    id: str
    relation: str
    title: str | None = None
    direction: str = "out"
    where: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    fields: FieldSel = None  # None: no attributes, just keys and labels
    relations: tuple[RowRelation, ...] = ()
    max: int = NEIGHBORS_MAX_DEFAULT
    kind: ClassVar[str] = "neighbors"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "relation": self.relation,
            "direction": self.direction,
            "where": list(self.where),
            "order": list(self.order),
            "fields": fields_json(self.fields),
            "relations": [r.to_json() for r in self.relations],
            "max": self.max,
        }


@dataclass(frozen=True, slots=True)
class ListSection:
    id: str
    types: tuple[str, ...]
    title: str | None = None
    where: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    fields: FieldSel = None
    relations: tuple[RowRelation, ...] = ()
    kind: ClassVar[str] = "list"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "type": list(self.types),
            "where": list(self.where),
            "order": list(self.order),
            "fields": fields_json(self.fields),
            "relations": [r.to_json() for r in self.relations],
        }


@dataclass(frozen=True, slots=True)
class RowsSection:
    id: str
    function: str
    title: str | None = None
    args: Mapping[str, JsonValue] = field(default=EMPTY)
    max: int | None = None  # entity views: the rows shown; set views: paged, no cut
    kind: ClassVar[str] = "rows"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "function": self.function,
            "args": dict(self.args),
            "max": self.max,
        }


type SectionDef = (
    FieldsSection
    | RelationsSection
    | FilesSection
    | AffordancesSection
    | NeighborsSection
    | ListSection
    | RowsSection
)


@dataclass(frozen=True, slots=True)
class ViewDef:
    name: str
    module: str | None  # module id or prefix as authored
    summary: str
    use_when: str
    sections: tuple[SectionDef, ...]
    takes: str | None = None  # the class an entity view takes; None: a set view
    default: bool = False
    params: tuple[ParamDef, ...] = ()
    max_bytes: int | None = None  # entity views
    page_bytes: int | None = None  # set views
    examples: tuple[ExampleDef, ...] = ()
    status: str = "active"  # one of STATUSES
    replaced_by: str | None = None

    @property
    def entity(self) -> bool:
        """Whether the view takes an entity (an entity view) or none (a set view)."""
        return self.takes is not None

    def to_json(self) -> dict[str, JsonValue]:
        """The declaration with its sections (the compiled entry adds touches and the hash)."""
        return {
            "name": self.name,
            "module": self.module,
            "summary": self.summary,
            "use_when": self.use_when,
            "status": self.status,
            "replaced_by": self.replaced_by,
            "takes": self.takes,
            "default": self.default,
            "params": [p.to_json() for p in self.params],
            "max_bytes": self.max_bytes,
            "page_bytes": self.page_bytes,
            "sections": [s.to_json() for s in self.sections],
            "examples": [{"question": e.question, "params": dict(e.params)} for e in self.examples],
        }

    @classmethod
    def from_json(cls, data: JsonObject) -> ViewDef:
        """Read back ``to_json``; ``JsonShapeError`` for a document of another shape."""
        path = f"{data.get('name')}."
        return cls(
            name=req_str(data, "name"),
            module=opt_str(data, "module", path),
            summary=req_str(data, "summary", path),
            use_when=req_str(data, "use_when", path),
            sections=tuple(
                section_from_json(as_object(s, f"{path}sections"), f"{path}sections.")
                for s in req_list(data, "sections", path)
            ),
            takes=opt_str(data, "takes", path),
            default=req_bool(data, "default", path),
            params=tuple(
                ParamDef.from_json(as_object(p, f"{path}params"), f"{path}params.")
                for p in req_list(data, "params", path)
            ),
            max_bytes=opt_int(data, "max_bytes", path),
            page_bytes=opt_int(data, "page_bytes", path),
            examples=tuple(
                ExampleDef(req_str(o, "question", path), req_obj(o, "params", path))
                for o in (as_object(e, f"{path}examples") for e in req_list(data, "examples", path))
            ),
            status=req_str(data, "status", path),
            replaced_by=opt_str(data, "replaced_by", path),
        )


# ---- JSON form --------------------------------------------------------------------------------


def fields_json(sel: FieldSel) -> JsonValue:
    return list(sel) if isinstance(sel, tuple) else sel


def _fields_from(data: JsonObject, path: str) -> FieldSel:
    value = data.get("fields")
    if value is None or value == ALL:
        return value
    return str_list(data, "fields", path)


def _row_relation_from(data: JsonObject, path: str) -> RowRelation:
    return RowRelation(
        relation=opt_str(data, "relation", path),
        direction=req_str(data, "direction", path),
        fields=_fields_from(data, path),
        where=str_list(data, "where", path),
        order=str_list(data, "order", path),
        max=req_int(data, "max", path),
    )


def _row_relations_from(data: JsonObject, path: str) -> tuple[RowRelation, ...]:
    return tuple(
        _row_relation_from(as_object(r, f"{path}relations"), f"{path}relations.")
        for r in req_list(data, "relations", path)
    )


def section_from_json(data: JsonObject, path: str) -> SectionDef:
    """Read back ``to_json`` of one section."""
    kind = req_str(data, "kind", path)
    sid, title = req_str(data, "id", path), opt_str(data, "title", path)
    if kind == "fields":
        return FieldsSection(
            sid,
            title,
            _fields_from(data, path),
            req_int(data, "value_chars", path),
            req_int(data, "max", path),
        )
    if kind == "relations":
        given = data.get("relations")
        refs = (
            None
            if given is None
            else tuple(
                RelationRef(req_str(o, "relation", path), req_str(o, "direction", path))
                for o in (as_object(r, f"{path}relations") for r in as_list(given, path))
            )
        )
        return RelationsSection(
            sid, title, refs, req_int(data, "max_groups", path), req_int(data, "max_items", path)
        )
    if kind == "files":
        return FilesSection(sid, title)
    if kind == "affordances":
        return AffordancesSection(sid, title)
    if kind == "neighbors":
        return NeighborsSection(
            id=sid,
            relation=req_str(data, "relation", path),
            title=title,
            direction=req_str(data, "direction", path),
            where=str_list(data, "where", path),
            order=str_list(data, "order", path),
            fields=_fields_from(data, path),
            relations=_row_relations_from(data, path),
            max=req_int(data, "max", path),
        )
    if kind == "list":
        return ListSection(
            id=sid,
            types=str_list(data, "type", path),
            title=title,
            where=str_list(data, "where", path),
            order=str_list(data, "order", path),
            fields=_fields_from(data, path),
            relations=_row_relations_from(data, path),
        )
    if kind == "rows":
        return RowsSection(
            id=sid,
            function=req_str(data, "function", path),
            title=title,
            args=req_obj(data, "args", path),
            max=opt_int(data, "max", path),
        )
    raise JsonShapeError(f"{path}kind: unknown section kind {kind!r}")


# ---- placeholders -----------------------------------------------------------------------------


def placeholder_name(value: JsonValue) -> str | None:
    """The name of ``$name`` (``entity`` for ``$entity``); ``None`` for any other value."""
    if isinstance(value, str) and PLACEHOLDER_RE.match(value):
        return value[1:]
    return None


def where_parts(text: str) -> tuple[str, str | None]:
    """``(left, parameter)`` of a ``where`` entry ``attr[:op]=value``: the part before the first
    ``=`` and the name when the whole value is ``$name`` (else ``None``)."""
    left, sep, value = text.partition("=")
    return left, (placeholder_name(value) if sep else None)


# ---- parser -----------------------------------------------------------------------------------


def view_name(file_name: str) -> str:
    """The name a file stands for: its name without the ``.yaml`` suffix."""
    return file_name.removesuffix(FILE_SUFFIX)


def load_view(file_name: str, content: str | bytes) -> tuple[ViewDef | None, list[Problem]]:
    """Parse one view file. The definition is ``None`` when any problem was found."""
    parse = _Parse(view_name(file_name))
    try:
        root = load_strict(content)
    except YamlError as exc:
        parse.add("view.invalid_yaml", None, str(exc), line=exc.line)
        return None, parse.problems
    if root is None:
        parse.add("view.invalid_yaml", None, "the file is empty")
        return None, parse.problems
    defn = parse.read(root, file_name)
    return (defn if not parse.problems else None), parse.problems


class _Parse(Parser):
    """Collects the problems of one view file."""

    def __init__(self, view: str) -> None:
        super().__init__("view", view)

    def read(self, root: YamlNode, file_name: str) -> ViewDef | None:
        top = self.mapping(root, "", _TOP_KEYS)
        if top is None:
            return None
        line = root.line
        for key in ("schema", "name", "summary", "use_when", "sections"):
            self.require(top, key, "", line)
        if "schema" in top:
            schema = self.text(top["schema"], "schema")
            if schema is not None and schema != VIEW_SCHEMA:
                self.add(
                    "view.unsupported_schema",
                    "schema",
                    f"{schema!r} is not {VIEW_SCHEMA}",
                    line=top["schema"].line,
                )
        name = self.name_of(top.get("name"), "name", NAME_RE) if "name" in top else None
        if name == RESERVED_NAME:
            self.add(
                "view.reserved_name",
                "name",
                f"{RESERVED_NAME!r} is the generated card; choose another name",
                line=top["name"].line,
            )
        if name is not None and file_name != name + FILE_SUFFIX:
            self.add(
                "view.name_mismatch",
                "name",
                f"name {name!r} needs the file name {name + FILE_SUFFIX}, not {file_name}",
                line=top["name"].line,
            )
        module = None
        if "module" in top:
            module = self.name_of(top["module"], "module", MODULE_REF_RE)
        summary = self.text(top.get("summary"), "summary", limit=MAX_SUMMARY, one_line=True)
        use_when = self.text(top.get("use_when"), "use_when", limit=MAX_USE_WHEN)
        status = self.choice(top["status"], "status", STATUSES) if "status" in top else "active"
        replaced_by = None
        if "replaced_by" in top and not is_null(top["replaced_by"]):
            replaced_by = self.name_of(top["replaced_by"], "replaced_by", NAME_RE)
        if replaced_by is not None and status != "deprecated":
            self.invalid("replaced_by", "is only allowed with status: deprecated")
        if replaced_by is not None and replaced_by == name:
            self.invalid("replaced_by", "a view cannot replace itself")
        entity = "takes" in top
        takes = self.term(top["takes"], "takes") if entity else None
        default = self.default(top, entity)
        if status == "deprecated" and default:
            self.invalid("default", "a deprecated view cannot be the default view of its class")
        max_bytes = self.limit(top, "max_bytes", MIN_BYTES, ENTITY_MAX_BYTES, "entity", entity)
        page_bytes = self.limit(top, "page_bytes", MIN_BYTES, PAGE_BYTES_MAX, "set", entity)
        params = self.params(top["params"]) if "params" in top else ()
        if params is not None and any(p.name == ENTITY for p in params):
            self.invalid(f"params.{ENTITY}", f"{ENTITY!r} is reserved for the entity of the view")
        sections = self.sections(top["sections"], entity) if "sections" in top else None
        examples = self.examples(top["examples"]) if "examples" in top else ()
        if self.problems:
            return None
        assert name and summary and use_when and sections is not None
        assert status is not None and params is not None and examples is not None
        assert default is not None
        return ViewDef(
            name=name,
            module=module,
            summary=summary,
            use_when=use_when,
            sections=sections,
            takes=takes,
            default=default,
            params=params,
            max_bytes=max_bytes,
            page_bytes=page_bytes,
            examples=examples,
            status=status,
            replaced_by=replaced_by,
        )

    def examples(self, node: YamlNode) -> tuple[ExampleDef, ...] | None:
        return parse_examples(self, node)

    # Small readers

    def term(self, node: YamlNode | None, path: str) -> str | None:
        """A class or relation: its CURIE or local name (compile resolves it)."""
        return self.name_of(node, path, TERM_RE)

    def integer(self, node: YamlNode | None, path: str, low: int, high: int) -> int | None:
        value = node.as_int() if isinstance(node, YamlScalar) else None
        if value is None:
            self.invalid(path, "expected an integer", node)
            return None
        if not low <= value <= high:
            self.invalid(path, f"is {value}; expected {low} to {high}", node)
            return None
        return value

    def mismatch(self, path: str, what: str, owner: str, node: YamlNode | None = None) -> None:
        """A key or section kind that only the ``owner`` flavour (``entity`` or ``set``) has."""
        owns = "an entity view" if owner == "entity" else "a set view"
        this = (
            "a set view (it has no takes)" if owner == "entity" else "an entity view (it has takes)"
        )
        self.add(
            "view.flavour_mismatch",
            path,
            f"{what} belongs to {owns}; this is {this}",
            line=node.line if node else None,
            flavour=owner,
        )

    def default(self, top: Mapping[str, YamlNode], entity: bool) -> bool | None:
        if "default" not in top:
            return False
        if not entity:
            self.mismatch("default", "default", "entity", top["default"])
            return None
        return self.boolean(top["default"], "default")

    def limit(
        self, top: Mapping[str, YamlNode], key: str, low: int, high: int, owner: str, entity: bool
    ) -> int | None:
        """A byte limit that only the ``owner`` flavour has."""
        if key not in top:
            return None
        if (owner == "entity") != entity:
            self.mismatch(key, key, owner, top[key])
            return None
        return self.integer(top[key], key, low, high)

    def names(
        self, node: YamlNode, path: str, grammar: re.Pattern[str], limit: int
    ) -> tuple[str, ...] | None:
        """A non-empty list of distinct names."""
        items = self.sequence(node, path)
        if items is None:
            return None
        if len(items) > limit:
            self.invalid(path, f"has {len(items)} entries; at most {limit}", node)
            return None
        found: list[str] = []
        for index, item in enumerate(items):
            name = self.name_of(item, f"{path}[{index}]", grammar)
            if name is None:
                continue
            if name in found:
                self.invalid(f"{path}[{index}]", f"{name!r} is listed twice", item)
                continue
            found.append(name)
        return tuple(found) if len(found) == len(items) else None

    def field_names(self, node: YamlNode, path: str, limit: int = MAX_FIELD_NAMES) -> FieldSel:
        """``all`` or a list of attribute names."""
        if isinstance(node, YamlScalar) and node.plain and node.text == ALL:
            return ALL
        if isinstance(node, YamlScalar):
            self.invalid(path, "expected all or a list of attribute names", node)
            return None
        return self.names(node, path, LOWER_NAME_RE, limit)

    def clauses(self, node: YamlNode, path: str, *, where: bool = False) -> tuple[str, ...] | None:
        """``where`` or ``order`` entries: strings of the ``find`` flag grammar. A ``$name`` in a
        ``where`` entry is the whole value after the ``=`` or nothing."""
        items = self.sequence(node, path)
        if items is None:
            return None
        if len(items) > MAX_CLAUSES:
            self.invalid(path, f"has {len(items)} entries; at most {MAX_CLAUSES}", node)
            return None
        found = [
            self.text(item, f"{path}[{i}]", limit=MAX_CLAUSE_CHARS, one_line=True)
            for i, item in enumerate(items)
        ]
        if where:
            for index, entry in enumerate(found):
                if entry is not None and not self.whole_placeholder(entry, f"{path}[{index}]"):
                    return None
        return tuple(c for c in found if c is not None) if None not in found else None

    def whole_placeholder(self, entry: str, path: str) -> bool:
        """Whether ``entry`` (a where entry) holds no ``$name`` except as its whole value."""
        value = entry.partition("=")[2]
        if TOKEN_RE.search(value) is None or PLACEHOLDER_RE.match(value):
            return True
        self.add(
            "view.placeholder_position",
            path,
            "a $name stands for a whole value: write it alone after the =; a literal cannot "
            "contain $ followed by a lowercase name",
        )
        return False

    # Sections

    def sections(self, node: YamlNode, entity: bool) -> tuple[SectionDef, ...] | None:
        items = self.sequence(node, "sections")
        if items is None:
            return None
        if len(items) > MAX_SECTIONS:
            self.invalid("sections", f"has {len(items)} sections; at most {MAX_SECTIONS}", node)
            return None
        found: list[SectionDef] = []
        for index, item in enumerate(items):
            section = self.section(item, f"sections[{index}]", entity)
            if section is not None:
                found.append(section)
        seen: dict[str, int] = {}
        for index, section in enumerate(found):
            if section.id in seen:
                self.add(
                    "view.duplicate_section",
                    "sections",
                    f"two sections have the id {section.id!r}; give each section of one kind "
                    "its own id",
                    id=section.id,
                )
            seen.setdefault(section.id, index)
        return tuple(found)

    def section(self, node: YamlNode, path: str, entity: bool) -> SectionDef | None:
        if not isinstance(node, YamlMap):
            self.invalid(path, "expected a mapping", node)
            return None
        if "kind" not in node.items:
            self.add(self.code("missing_key"), f"{path}.kind", "missing key 'kind'", line=node.line)
            return None
        kind = self.choice(node.items["kind"], f"{path}.kind", KINDS)
        if kind is None:
            return None
        if kind not in (ENTITY_KINDS if entity else SET_KINDS):
            self.mismatch(f"{path}.kind", f"a {kind} section", "set" if entity else "entity", node)
            return None
        body = self.mapping(node, path, _SECTION_KEYS[kind])
        if body is None:
            return None
        sid = kind
        if "id" in body:
            sid = self.name_of(body["id"], f"{path}.id", SECTION_ID_RE) or kind
        title = None
        if "title" in body:
            title = self.text(body["title"], f"{path}.title", limit=MAX_TITLE, one_line=True)
        if kind == "fields":
            return self.fields_section(sid, title, body, path)
        if kind == "relations":
            return self.relations_section(sid, title, body, path)
        if kind == "files":
            return FilesSection(sid, title)
        if kind == "affordances":
            return AffordancesSection(sid, title)
        if kind == "neighbors":
            return self.neighbors_section(sid, title, body, path, node.line)
        if kind == "list":
            return self.list_section(sid, title, body, path, node.line)
        return self.rows_section(sid, title, body, path, node.line, entity)

    def fields_section(
        self, sid: str, title: str | None, body: dict[str, YamlNode], path: str
    ) -> SectionDef | None:
        fields = (
            self.field_names(body["fields"], f"{path}.fields", FIELDS_MAX_MAX)
            if "fields" in body
            else None
        )
        value_chars = self.number(
            body, "value_chars", path, 1, VALUE_CHARS_MAX, VALUE_CHARS_DEFAULT
        )
        # a list shows all its names unless max says less is wanted (then it is refused)
        default = len(fields) if isinstance(fields, tuple) else FIELDS_MAX_DEFAULT
        count = self.number(body, "max", path, 1, FIELDS_MAX_MAX, default)
        if isinstance(fields, tuple) and count is not None and count < len(fields):
            self.invalid(f"{path}.max", f"is {count}; the section lists {len(fields)} fields")
            return None
        if ("fields" in body and fields is None) or value_chars is None or count is None:
            return None
        return FieldsSection(sid, title, fields, value_chars, count)

    def number(
        self,
        body: Mapping[str, YamlNode],
        key: str,
        path: str,
        low: int,
        high: int,
        default: int,
    ) -> int | None:
        if key not in body:
            return default
        return self.integer(body[key], f"{path}.{key}", low, high)

    def relations_section(
        self, sid: str, title: str | None, body: dict[str, YamlNode], path: str
    ) -> SectionDef | None:
        refs = None
        listed = True
        if "relations" in body:
            refs = self.relation_refs(body["relations"], f"{path}.relations")
            listed = refs is not None
        groups = self.number(body, "max_groups", path, 1, GROUPS_MAX_MAX, GROUPS_MAX_DEFAULT)
        items = self.number(body, "max_items", path, 1, ITEMS_MAX_MAX, ITEMS_MAX_DEFAULT)
        if not listed or groups is None or items is None:
            return None
        return RelationsSection(sid, title, refs, groups, items)

    def relation_refs(self, node: YamlNode, path: str) -> tuple[RelationRef, ...] | None:
        items = self.sequence(node, path)
        if items is None:
            return None
        found: list[RelationRef] = []
        for index, item in enumerate(items):
            here = f"{path}[{index}]"
            if isinstance(item, YamlMap):
                body = self.mapping(item, here, ("relation", "direction"))
                if body is None:
                    continue
                if not self.require(body, "relation", here, item.line):
                    continue
                relation = self.term(body["relation"], f"{here}.relation")
                direction = "out"
                if "direction" in body:
                    direction = (
                        self.choice(body["direction"], f"{here}.direction", DIRECTIONS) or ""
                    )
                if relation is not None and direction:
                    found.append(RelationRef(relation, direction))
            else:
                relation = self.term(item, here)
                if relation is not None:
                    found.append(RelationRef(relation))
        if len(found) != len(items):
            return None
        if len({(r.relation, r.direction) for r in found}) != len(found):
            self.invalid(path, "lists a relation twice in the same direction", node)
            return None
        return tuple(found)

    def row_relations(self, node: YamlNode, path: str) -> tuple[RowRelation, ...] | None:
        """``all`` or a list of ``all`` and mappings: the per-row neighbour lists."""
        if isinstance(node, YamlScalar) and node.plain and node.text == ALL:
            return (RowRelation(),)
        items = self.sequence(node, path)
        if items is None:
            return None
        if len(items) > MAX_ROW_RELATIONS:
            self.invalid(path, f"has {len(items)} entries; at most {MAX_ROW_RELATIONS}", node)
            return None
        found: list[RowRelation] = []
        for index, item in enumerate(items):
            here = f"{path}[{index}]"
            if isinstance(item, YamlScalar) and item.plain and item.text == ALL:
                found.append(RowRelation())
                continue
            one = self.row_relation(item, here)
            if one is not None:
                found.append(one)
        if len(found) != len(items):
            return None
        if sum(1 for r in found if r.relation is None) > 1:
            self.invalid(path, "lists all twice", node)
            return None
        keys = [(r.relation, r.direction) for r in found if r.relation is not None]
        if len(set(keys)) != len(keys):
            self.invalid(path, "lists a relation twice in the same direction", node)
            return None
        return tuple(found)

    def row_relation(self, node: YamlNode, path: str) -> RowRelation | None:
        body = self.mapping(node, path, _ROW_RELATION_KEYS)
        if body is None:
            return None
        if not self.require(body, "relation", path, node.line):
            return None
        relation = self.term(body["relation"], f"{path}.relation")
        direction = "out"
        if "direction" in body:
            direction = self.choice(body["direction"], f"{path}.direction", DIRECTIONS) or ""
        fields = self.field_names(body["fields"], f"{path}.fields") if "fields" in body else None
        where = self.clauses(body["where"], f"{path}.where", where=True) if "where" in body else ()
        order = self.clauses(body["order"], f"{path}.order") if "order" in body else ()
        count = self.number(body, "max", path, 1, ROW_RELATION_MAX_MAX, ROW_RELATION_MAX_DEFAULT)
        broken = (
            relation is None
            or not direction
            or ("fields" in body and fields is None)
            or where is None
            or order is None
            or count is None
        )
        if broken:
            return None
        assert relation is not None and where is not None and order is not None
        assert count is not None
        return RowRelation(relation, direction, fields, where, order, count)

    def row_shape(
        self, body: dict[str, YamlNode], path: str
    ) -> tuple[FieldSel, tuple[str, ...], tuple[str, ...], tuple[RowRelation, ...]] | None:
        """The keys ``list`` and ``neighbors`` share: fields, where, order, relations."""
        fields = self.field_names(body["fields"], f"{path}.fields") if "fields" in body else None
        where = self.clauses(body["where"], f"{path}.where", where=True) if "where" in body else ()
        order = self.clauses(body["order"], f"{path}.order") if "order" in body else ()
        relations = (
            self.row_relations(body["relations"], f"{path}.relations")
            if "relations" in body
            else ()
        )
        if ("fields" in body and fields is None) or None in (where, order, relations):
            return None
        assert where is not None and order is not None and relations is not None
        return fields, where, order, relations

    def neighbors_section(
        self, sid: str, title: str | None, body: dict[str, YamlNode], path: str, line: int
    ) -> SectionDef | None:
        self.require(body, "relation", path, line)
        relation = self.term(body.get("relation"), f"{path}.relation")
        direction = "out"
        if "direction" in body:
            direction = self.choice(body["direction"], f"{path}.direction", DIRECTIONS) or ""
        shape = self.row_shape(body, path)
        count = self.number(body, "max", path, 1, NEIGHBORS_MAX_MAX, NEIGHBORS_MAX_DEFAULT)
        if relation is None or not direction or shape is None or count is None:
            return None
        fields, where, order, relations = shape
        return NeighborsSection(
            sid, relation, title, direction, where, order, fields, relations, count
        )

    def list_section(
        self, sid: str, title: str | None, body: dict[str, YamlNode], path: str, line: int
    ) -> SectionDef | None:
        self.require(body, "type", path, line)
        types = self.type_names(body.get("type"), f"{path}.type")
        shape = self.row_shape(body, path)
        if types is None or shape is None:
            return None
        fields, where, order, relations = shape
        return ListSection(sid, types, title, where, order, fields, relations)

    def type_names(self, node: YamlNode | None, path: str) -> tuple[str, ...] | None:
        """One class or a list of classes."""
        if node is None:
            return None
        if isinstance(node, YamlSeq):
            return self.names(node, path, TERM_RE, MAX_TYPES)
        found = self.term(node, path)
        return None if found is None else (found,)

    def rows_section(
        self,
        sid: str,
        title: str | None,
        body: dict[str, YamlNode],
        path: str,
        line: int,
        entity: bool,
    ) -> SectionDef | None:
        self.require(body, "function", path, line)
        function = self.name_of(body.get("function"), f"{path}.function", NAME_RE)
        args = self.args(body["args"], f"{path}.args") if "args" in body else {}
        count: int | None = None
        ok = True
        if "max" in body:
            if entity:
                count = self.integer(body["max"], f"{path}.max", 1, ROWS_MAX_MAX)
                ok = count is not None
            else:
                self.mismatch(f"{path}.max", "max of a rows section", "entity", body["max"])
                ok = False
        elif entity:
            count = ROWS_MAX_DEFAULT
        if function is None or args is None or not ok:
            return None
        return RowsSection(sid, function, title, args, count)

    def whole_argument(self, value: JsonValue, path: str) -> bool:
        """Whether a ``$name`` in an argument is the whole argument (never inside a text or a
        list)."""
        texts = value if isinstance(value, list) else [value]
        for text in texts:
            if not isinstance(text, str) or TOKEN_RE.search(text) is None:
                continue
            if isinstance(value, str) and PLACEHOLDER_RE.match(value):
                continue
            self.add(
                "view.placeholder_position",
                path,
                "a $name stands for a whole argument: write it alone, not inside a text or a "
                "list; a literal cannot contain $ followed by a lowercase name",
            )
            return False
        return True

    def args(self, node: YamlNode, path: str) -> dict[str, JsonValue] | None:
        if not isinstance(node, YamlMap):
            if is_null(node):
                return {}
            self.invalid(path, "expected a mapping", node)
            return None
        if len(node.items) > MAX_ARGS:
            self.invalid(path, f"has {len(node.items)} entries; at most {MAX_ARGS}", node)
            return None
        found: dict[str, JsonValue] = {}
        ok = True
        for key, value in node.items.items():
            here = f"{path}.{key}"
            if not PARAM_RE.match(key):
                self.add(
                    self.code("invalid_value"),
                    here,
                    f"argument name {key!r} does not match {PARAM_RE.pattern}",
                    line=node.key_lines[key],
                )
                ok = False
                continue
            literal = self.literal(value, here)
            if literal is None or not self.whole_argument(literal, here):
                ok = False
                continue
            found[key] = literal
        return found if ok else None
