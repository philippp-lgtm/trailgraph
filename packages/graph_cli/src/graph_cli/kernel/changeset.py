"""Change set documents ``graph_cli.changeset/1`` (contracts B3, W.1).

``parse_changeset`` checks a parsed document strictly: unknown keys, wrong
types, bad grammar and limits are ``changeset.malformed`` or
``changeset.too_large`` with ``details.path``. Ontology semantics (unknown
types, attributes, datatypes) are not checked here; the validator reports
them as findings.

``changeset_to_json`` is the canonical form: defaults are omitted (C.6),
references and timestamps are normalized. The tx stores it, and
``changeset_hash`` is C.2 over it, so two documents that differ only in key
order or explicit defaults hash the same (idempotent retries, B3.12).

A document may also carry a ``source`` (the source run it comes from) and a ``reconcile``
part (the desired state of declared scopes, reconcile plan R-K1 to R-K5). Both are parsed
here; the write service turns the reconcile part into ops under the graph lock, so the
tx records the expanded ops only (``changeset_to_json`` leaves both out) and the source in
``request.source``. ``document_to_json`` is the canonical form of the whole document, the
input a request digest of a reconcile document covers.

Input readers parse JSON floats into ``FloatLiteral`` so that a float in an
attribute or qualifier value reaches the validator as
``data.structural.float_not_allowed`` (C.1); a float anywhere else is
``changeset.malformed``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Final

from graph_cli.kernel.actions import Action
from graph_cli.kernel.canonical import CanonicalError, JsonValue, hash_value, parse_json
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import EMPTY, GraphCliError
from graph_cli.kernel.ids import (
    ABBREV_RE,
    ALIAS_RE,
    TERM_REF_RE,
    AbbrevRef,
    EdgeId,
    EntityId,
    EntityRef,
    TermRef,
    parse_entity_ref,
    parse_id,
    ref_text,
)
from graph_cli.kernel.records import (
    SOURCE_NAME_RE,
    EdgeRecord,
    SourceRef,
    is_source_text,
)

CHANGESET_SCHEMA: Final = "graph_cli.changeset/1"
# The changeset of a migration tx (plan M4): no ops, a ``migration`` record; not an input document.
MIGRATION_CHANGESET_SCHEMA: Final = "graph_cli.migration/1"
MAX_OPS: Final = 1000
MAX_BYTES: Final = 8 * 1024 * 1024
MAX_REASON: Final = 2000
MAX_RECONCILE_SCOPES: Final = 32
# Ops of a document after its reconcile part is expanded, its own ops included (R-K13).
MAX_RECONCILE_OPS: Final = 5000
MAX_KEY_BYTES: Final = 512  # an item key, ``under`` or a ``keep``/``moves`` key, in UTF-8

NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # attributes, qualifiers, states, locals
IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SCHEMA_RE: Final = re.compile(r"^graph_cli\.changeset/[0-9]+$")

EMPTY_TEXT: Final[Mapping[str, str]] = MappingProxyType({})
NO_VALUES: Final[Mapping[str, tuple[JsonValue, ...]]] = MappingProxyType({})
NO_TEXTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({})

# (required, optional) keys of the document, an edge and each op; the parser and
# skill 1's change set reference read them, so the two cannot drift.
Keys = tuple[tuple[str, ...], tuple[str, ...]]
# ``ops`` is optional because a document may carry ``reconcile`` instead (at least one of the
# two); the parser enforces that.
DOCUMENT_KEYS: Final[Keys] = (
    (),
    ("ops", "schema", "reason", "idempotency_key", "expect_seq", "source", "reconcile"),
)
SOURCE_KEYS: Final[Keys] = (("name",), ("version", "run"))
SCOPE_KEYS: Final[Keys] = (
    ("type", "key", "items"),
    (
        "under",
        "complete",
        "fields",
        "edges",
        "retire",
        "allow_retire",
        "allow_empty",
        "keep",
        "moves",
        "expect",
    ),
)
ITEM_KEYS: Final[Keys] = (
    ("key",),
    ("type", "local", "attrs", "attrs_text", "edges", "valid_from", "valid_to"),
)
ITEM_EDGE_KEYS: Final[Keys] = (("rel", "target"), ("qualifiers",))
VALUE_REF_KEYS: Final[Keys] = (("type", "attr", "value"), ())
RETIRE_KEYS: Final[Keys] = (("transition",), ("attr", "revive"))
EXPECT_KEYS: Final[Keys] = ((), ("min_items", "max_items"))
# A move names the entity by its old key (``from``) or by a reference (``id``, an adoption).
MOVE_KEYS: Final[Mapping[str, Keys]] = MappingProxyType(
    {"from": (("from", "to"), ()), "id": (("id", "to"), ())}
)
EDGE_KEYS: Final[Keys] = (("rel", "target"), ("qualifiers", "target_rev"))
OP_KEYS: Final[Mapping[str, Keys]] = MappingProxyType(
    {
        "create": (
            ("op", "type"),
            ("local", "attrs", "attrs_text", "edges", "valid_from", "valid_to"),
        ),
        "update": (
            ("op", "id"),
            ("set", "set_text", "unset", "add", "add_text", "remove", "remove_text", "expect_rev"),
        ),
        "link": (("op", "id", "rel", "target"), ("qualifiers", "target_rev", "expect_rev")),
        "unlink": (("op", "id"), ("edge_id", "rel", "target", "expect_rev")),
        "transition": (("op", "id", "to"), ("attr", "expect_rev")),
        "supersede": (("op", "by", "targets"), ()),
        "retract": (("op", "id"), ("by", "expect_rev")),
        "set_alias": (("op", "id", "alias"), ()),
    }
)


class FloatLiteral(str):
    """A JSON number with a fraction or exponent, kept as its source text.

    Only input readers create it. It never reaches a persisted document:
    the validator refuses it wherever a value may appear.
    """

    __slots__ = ()


def float_numbers(value: JsonValue) -> Any:
    """``value`` with every ``FloatLiteral`` a Python float again, for a reader that refuses
    a float by its JSON type (a request body outside its ``args``)."""
    if isinstance(value, FloatLiteral):
        return float(value)
    if isinstance(value, Mapping):
        return {key: float_numbers(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [float_numbers(item) for item in value]
    return value


def contains_float(value: JsonValue) -> bool:
    if isinstance(value, FloatLiteral):
        return True
    if isinstance(value, Mapping):
        return any(contains_float(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(contains_float(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    rel: TermRef
    target: EntityRef
    qualifiers: Mapping[str, JsonValue] = field(default=EMPTY)
    target_rev: int | None = None


@dataclass(frozen=True, slots=True)
class CreateOp:
    OP: ClassVar[str] = "create"
    type: TermRef
    attrs: Mapping[str, JsonValue] = field(default=EMPTY)
    attrs_text: Mapping[str, str] = field(default=EMPTY_TEXT)
    local: str | None = None
    edges: tuple[EdgeSpec, ...] = ()
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateOp:
    OP: ClassVar[str] = "update"
    id: EntityRef
    set: Mapping[str, JsonValue] = field(default=EMPTY)
    set_text: Mapping[str, str] = field(default=EMPTY_TEXT)
    unset: tuple[str, ...] = ()
    expect_rev: int | None = None
    # list attributes (many: true): values appended if absent, or dropped
    add: Mapping[str, tuple[JsonValue, ...]] = field(default=NO_VALUES)
    add_text: Mapping[str, tuple[str, ...]] = field(default=NO_TEXTS)
    remove: Mapping[str, tuple[JsonValue, ...]] = field(default=NO_VALUES)
    remove_text: Mapping[str, tuple[str, ...]] = field(default=NO_TEXTS)

    def list_names(self) -> tuple[str, ...]:
        """The attributes of ``add``, ``add_text``, ``remove`` and ``remove_text``."""
        return tuple(sorted({*self.add, *self.add_text, *self.remove, *self.remove_text}))


@dataclass(frozen=True, slots=True)
class LinkOp:
    OP: ClassVar[str] = "link"
    id: EntityRef
    edge: EdgeSpec
    expect_rev: int | None = None


@dataclass(frozen=True, slots=True)
class UnlinkOp:
    """By ``edge_id``, or by ``rel`` + ``target`` when exactly one edge matches."""

    OP: ClassVar[str] = "unlink"
    id: EntityRef
    edge_id: EdgeId | None = None
    rel: TermRef | None = None
    target: EntityRef | None = None
    expect_rev: int | None = None


@dataclass(frozen=True, slots=True)
class TransitionOp:
    OP: ClassVar[str] = "transition"
    id: EntityRef
    to: str
    attr: str | None = None
    expect_rev: int | None = None


@dataclass(frozen=True, slots=True)
class SupersedeOp:
    OP: ClassVar[str] = "supersede"
    by: EntityRef
    targets: tuple[EntityRef, ...]


@dataclass(frozen=True, slots=True)
class RetractOp:
    OP: ClassVar[str] = "retract"
    id: EntityRef
    by: EntityRef | None = None
    expect_rev: int | None = None


@dataclass(frozen=True, slots=True)
class SetAliasOp:
    OP: ClassVar[str] = "set_alias"
    id: EntityRef
    alias: str


Op = CreateOp | UpdateOp | LinkOp | UnlinkOp | TransitionOp | SupersedeOp | RetractOp | SetAliasOp

OP_ACTIONS: Final[Mapping[str, Action]] = MappingProxyType(
    {
        "create": Action("write.create"),
        "update": Action("write.update"),
        "link": Action("write.link"),
        "unlink": Action("write.unlink"),
        "transition": Action("write.transition"),
        "supersede": Action("write.supersede"),
        "retract": Action("write.retract"),
        "set_alias": Action("write.alias"),
    }
)


@dataclass(frozen=True, slots=True)
class ValueRef:
    """An edge target of a reconcile item given by a unique value: the active entity of the
    ``type`` family whose unique attribute ``attr`` holds ``value`` (reconcile plan R-K5)."""

    type: TermRef
    attr: str
    value: str | int


@dataclass(frozen=True, slots=True)
class ReconcileEdge:
    """One desired outgoing edge of a reconcile item; ``rel`` is one of the scope's ``edges``."""

    rel: TermRef
    target: EntityRef | ValueRef
    qualifiers: Mapping[str, JsonValue] = field(default=EMPTY)


@dataclass(frozen=True, slots=True)
class ReconcileItem:
    """One desired entity of a scope. ``key`` is the item key, without the source name that
    the stored key value is prefixed with. Attributes are limited to the scope's ``fields``,
    relations to its ``edges``."""

    key: str
    type: TermRef | None = None  # a concrete subtype of the scope type
    local: str | None = None
    attrs: Mapping[str, JsonValue] = field(default=EMPTY)
    attrs_text: Mapping[str, str] = field(default=EMPTY_TEXT)
    edges: tuple[ReconcileEdge, ...] = ()
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass(frozen=True, slots=True)
class MoveSpec:
    """An entity that keeps its identity under a new key: the one holding ``old_key``, or the
    keyless one ``id`` names (an adoption). Exactly one of the two is set."""

    to: str
    old_key: str | None = None
    id: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class RetireSpec:
    """What happens to an entity that left a complete scope: a retract (no ``transition``) or a
    lifecycle transition to ``transition`` of ``attr`` (default: the type's lifecycle), and
    with ``revive`` a transition back when its item returns."""

    transition: str | None = None
    attr: str | None = None
    revive: str | None = None


RETRACT: Final = RetireSpec()


@dataclass(frozen=True, slots=True)
class ExpectSpec:
    """Plausible item counts of a scope (``reconcile.shape`` when the items fall outside)."""

    min_items: int | None = None
    max_items: int | None = None


@dataclass(frozen=True, slots=True)
class ReconcileScope:
    """The desired state of the entities one entry speaks for: active entities of ``type``
    (and its subtypes) whose ``key`` attribute starts with ``<source>/<under>``. With
    ``complete`` the items are the whole scope and absent entities retire; otherwise the scope
    is partial (create and update only). ``fields`` and ``edges``, sorted, are all the
    attributes and relations the source sets; ``keep`` lists item keys left as they are."""

    type: TermRef
    key: str  # the key attribute of ``type``
    items: tuple[ReconcileItem, ...]
    under: str = ""
    complete: bool = False
    fields: tuple[str, ...] = ()
    edges: tuple[TermRef, ...] = ()
    retire: RetireSpec = RETRACT
    allow_retire: int | None = None
    allow_empty: bool = False
    keep: tuple[str, ...] = ()
    moves: tuple[MoveSpec, ...] = ()
    expect: ExpectSpec | None = None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """A parsed document: the frame (``reason``, key, ``expect_seq``) with its ``ops``, and
    separately the ``source`` claim and the ``reconcile`` part. The write service expands the
    reconcile part into more ops under the graph lock and records the expanded change set
    (``reconcile=()``); ``ops`` of a document with a reconcile part run first."""

    ops: tuple[Op, ...]
    reason: str | None = None
    idempotency_key: str | None = None
    expect_seq: int | None = None
    source: SourceRef | None = None
    reconcile: tuple[ReconcileScope, ...] = ()


def op_action(op: Op) -> Action:
    """The action an op is authorized and audited as (B3 op catalog)."""
    return OP_ACTIONS[op.OP]


# The maps of an op whose keys name attributes (``unset`` lists names): what a write is
# decided on below the type (W4). ``op_attribute_names`` reads them from a parsed op or
# from an action's op template alike, so the write path and the action needs agree.
ATTRIBUTE_MAPS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "create": ("attrs", "attrs_text"),
        "update": ("set", "set_text", "add", "add_text", "remove", "remove_text"),
    }
)


def op_attribute_names(op: Op | Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The attribute names a create or update names, sorted and unique; none for another op."""
    kind = op.get("op") if isinstance(op, Mapping) else op.OP
    names: set[str] = set()
    for key in ATTRIBUTE_MAPS.get(str(kind), ()):
        found = op.get(key) if isinstance(op, Mapping) else getattr(op, key, None)
        if isinstance(found, Mapping):
            names.update(str(name) for name in found)
    if kind == "update":
        unset = op.get("unset") if isinstance(op, Mapping) else getattr(op, "unset", None)
        if isinstance(unset, list | tuple):
            names.update(item for item in unset if isinstance(item, str))
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class OpEffect:
    """What one op did to the working copy; the validator judges it (W4).

    Produced by ``temporal_knowledge.normalize``, read by the data validator;
    it lives here because the validator sits below temporal_knowledge.

    ``entity`` is the op's subject (``by`` for supersede). ``applied`` is
    false when normalize skipped the op (a ``data.temporal.*`` finding) or
    could not apply it (``problem``: transition without a lifecycle
    attribute, unlink without exactly one matching edge). ``edge`` is the edge an
    unlink removed, so that an edge added and removed in one change set is judged too.
    """

    index: int
    op: str
    entity: EntityId
    applied: bool = True
    names: tuple[str, ...] = ()  # create/update: attributes given; update: every key
    lists: tuple[str, ...] = ()  # update: attributes of add/remove (judged for many: true)
    array_texts: tuple[str, ...] = ()  # create/update: many attributes given a JSON array as text
    attr: str | None = None  # transition
    from_state: JsonValue = None
    to_state: str | None = None
    initial: tuple[tuple[str, JsonValue], ...] = ()  # create: lifecycle values after the op
    edge_id: EdgeId | None = None  # link: added; unlink: removed
    rel: TermRef | None = None
    edge: EdgeRecord | None = None  # unlink: the removed edge
    targets: tuple[EntityId, ...] = ()  # other entities touched: supersede targets, retract by
    alias: str | None = None  # set_alias: the requested alias
    problem: str | None = None


# ---- reading input -------------------------------------------------------------------------


def load_document(data: bytes | str, *, max_bytes: int = MAX_BYTES) -> JsonValue:
    """Parse one input document; floats become ``FloatLiteral``.

    Bad JSON, bad UTF-8, NaN/Infinity, duplicate keys, integers outside the
    safe range, lone surrogates and NUL are ``usage.invalid_input`` (C.5);
    more than ``max_bytes`` is ``changeset.too_large`` (B3.13).
    """
    raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
    if len(raw) > max_bytes:
        raise GraphCliError(
            "changeset.too_large",
            f"the document has {len(raw)} bytes, the limit is {max_bytes}",
            details={"limit_bytes": max_bytes, "actual_bytes": len(raw)},
        )
    try:
        return parse_json(raw, floats=FloatLiteral)
    except CanonicalError as exc:
        raise GraphCliError("usage.invalid_input", f"invalid JSON input: {exc}") from exc


def read_documents(data: bytes, *, max_bytes: int = MAX_BYTES) -> list[JsonValue]:
    """One JSON document, or JSONL with one document per line (B3.9).

    JSONL is recognized when the first non-blank line is a complete document.
    Every line is parsed before anything is applied, so broken input is a
    usage error with ``details.line`` and nothing happens.
    """
    lines = [(n, line) for n, line in enumerate(data.split(b"\n"), 1) if line.strip()]
    if not lines:
        raise GraphCliError("usage.invalid_input", "the input contains no document")
    first_number, first = lines[0]
    try:
        first_doc = load_document(first, max_bytes=max_bytes)
    except GraphCliError as exc:
        if len(lines) == 1:
            raise
        if exc.code == "usage.invalid_input":  # one pretty-printed document
            return [load_document(data, max_bytes=max_bytes)]
        raise _at_line(exc, first_number) from None
    documents = [first_doc]
    for number, line in lines[1:]:
        try:
            documents.append(load_document(line, max_bytes=max_bytes))
        except GraphCliError as exc:
            raise _at_line(exc, number) from None
    return documents


def _at_line(exc: GraphCliError, line: int) -> GraphCliError:
    return GraphCliError(
        exc.code, f"line {line}: {exc.message}", details={**exc.details, "line": line}
    )


# ---- parsing -------------------------------------------------------------------------------


def _malformed(path: str, message: str, **details: JsonValue) -> GraphCliError:
    where = path or "document"
    return GraphCliError(
        "changeset.malformed", f"{where}: {message}", details={"path": path, **details}
    )


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _object(value: JsonValue, path: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise _malformed(path, "expected an object")
    return value


def _keys(obj: Mapping[str, JsonValue], path: str, keys: Keys) -> None:
    required, optional = keys
    allowed = (*required, *optional)
    for key in obj:
        if key not in allowed:
            raise _malformed(_join(path, key), "unknown key", expected=sorted(allowed))
    for key in required:
        if key not in obj:
            raise _malformed(_join(path, key), "missing")


def _str(value: JsonValue, path: str) -> str:
    if not isinstance(value, str) or isinstance(value, FloatLiteral):
        raise _malformed(path, "expected a string")
    return value


def _grammar(value: JsonValue, path: str, pattern: re.Pattern[str], expected: str) -> str:
    text = _str(value, path)
    if not pattern.match(text):
        raise _malformed(path, f"expected {expected}", expected=expected, actual=text)
    return text


def _name(value: JsonValue, path: str) -> str:
    return _grammar(value, path, NAME_RE, "a lowercase name ([a-z][a-z0-9_]*)")


def _term(value: JsonValue, path: str) -> TermRef:
    return TermRef(_grammar(value, path, TERM_REF_RE, "a CURIE <prefix>:<Term>"))


def _int(value: JsonValue, path: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(path, "expected an integer")
    if value < minimum:
        raise _malformed(path, f"expected an integer >= {minimum}", actual=value)
    return value


def _opt_int(obj: Mapping[str, JsonValue], key: str, path: str, *, minimum: int) -> int | None:
    return None if key not in obj else _int(obj[key], _join(path, key), minimum=minimum)


def _ref(value: JsonValue, path: str) -> EntityRef:
    text = _str(value, path)
    ref = parse_entity_ref(text, allow_local=True)
    if ref is not None:
        return ref
    if ABBREV_RE.match(text.lower()):
        raise _malformed(path, "abbreviated ids are not allowed in change sets (B1.9)", actual=text)
    raise _malformed(
        path, "expected a full id, an alias or @local", expected="UUID | ALIAS-0001 | @name"
    )


def _values(value: JsonValue, path: str) -> dict[str, JsonValue]:
    """Attribute or qualifier values: any JSON value per valid name."""
    obj = _object(value, path)
    for key in obj:
        _name(key, _join(path, key))
    return dict(obj)


def _texts(value: JsonValue, path: str) -> dict[str, str]:
    obj = _object(value, path)
    result = {}
    for key, item in obj.items():
        _name(key, _join(path, key))
        result[key] = _str(item, _join(path, key))
    return result


def _disjoint(
    left: Mapping[str, object], right: Mapping[str, object], path: str, what: str
) -> None:
    for key in left:
        if key in right:
            raise _malformed(_join(path, key), f"given in both {what}")


def normalize_valid_time(text: str) -> str:
    """A date stays ``YYYY-MM-DD``, a timestamp becomes T.1 (B8.5).

    Anything else is ``usage.invalid_argument``.
    """
    if len(text) == 10:
        return parse_date(text).isoformat()
    return format_timestamp(parse_timestamp(text))


def _valid_time(value: JsonValue, path: str) -> str:
    text = _str(value, path)
    try:
        return normalize_valid_time(text)
    except GraphCliError as exc:
        raise _malformed(path, exc.message, expected="YYYY-MM-DD or RFC 3339") from None


def _edge(obj: Mapping[str, JsonValue], path: str) -> EdgeSpec:
    return EdgeSpec(
        rel=_term(obj["rel"], _join(path, "rel")),
        target=_ref(obj["target"], _join(path, "target")),
        qualifiers=_values(obj.get("qualifiers", {}), _join(path, "qualifiers")),
        target_rev=_opt_int(obj, "target_rev", path, minimum=1),
    )


def _create(obj: Mapping[str, JsonValue], path: str) -> CreateOp:
    if "id" in obj:
        raise _malformed(_join(path, "id"), "a create carries no id; ids are minted (B1.2)")
    _keys(obj, path, OP_KEYS["create"])
    attrs = _values(obj.get("attrs", {}), _join(path, "attrs"))
    attrs_text = _texts(obj.get("attrs_text", {}), _join(path, "attrs_text"))
    _disjoint(attrs, attrs_text, _join(path, "attrs_text"), "attrs and attrs_text")
    edges_path = _join(path, "edges")
    raw_edges = obj.get("edges", [])
    if not isinstance(raw_edges, list | tuple):
        raise _malformed(edges_path, "expected an array")
    edges = []
    for index, item in enumerate(raw_edges):
        edge_path = _join(edges_path, index)
        edge_obj = _object(item, edge_path)
        _keys(edge_obj, edge_path, EDGE_KEYS)
        edges.append(_edge(edge_obj, edge_path))
    local = obj.get("local")
    return CreateOp(
        type=_term(obj["type"], _join(path, "type")),
        attrs=attrs,
        attrs_text=attrs_text,
        local=None if local is None else _name(local, _join(path, "local")),
        edges=tuple(edges),
        valid_from=None
        if "valid_from" not in obj
        else _valid_time(obj["valid_from"], _join(path, "valid_from")),
        valid_to=None
        if "valid_to" not in obj
        else _valid_time(obj["valid_to"], _join(path, "valid_to")),
    )


def _arrays(value: JsonValue, path: str) -> dict[str, tuple[JsonValue, ...]]:
    """``{attr: [value, ...]}`` of add/remove: a non-empty array per valid name."""
    obj = _object(value, path)
    result: dict[str, tuple[JsonValue, ...]] = {}
    for key, items in obj.items():
        _name(key, _join(path, key))
        if not isinstance(items, list | tuple) or not items:
            raise _malformed(_join(path, key), "expected a non-empty array of values")
        result[key] = tuple(items)
    return result


def _text_arrays(value: JsonValue, path: str) -> dict[str, tuple[str, ...]]:
    return {
        key: tuple(_str(item, _join(_join(path, key), i)) for i, item in enumerate(items))
        for key, items in _arrays(value, path).items()
    }


_UPDATE_PARTS: Final = ("set", "set_text", "unset", "add", "add_text", "remove", "remove_text")


def _update(obj: Mapping[str, JsonValue], path: str) -> UpdateOp:
    _keys(obj, path, OP_KEYS["update"])
    values = _values(obj.get("set", {}), _join(path, "set"))
    texts = _texts(obj.get("set_text", {}), _join(path, "set_text"))
    unset_path = _join(path, "unset")
    raw_unset = obj.get("unset", [])
    if not isinstance(raw_unset, list | tuple):
        raise _malformed(unset_path, "expected an array")
    unset: list[str] = []
    for index, item in enumerate(raw_unset):
        name = _name(item, _join(unset_path, index))
        if name in unset:
            raise _malformed(_join(unset_path, index), "listed twice")
        if name in values or name in texts:
            raise _malformed(_join(unset_path, index), "both set and unset")
        unset.append(name)
    add = _arrays(obj.get("add", {}), _join(path, "add"))
    add_text = _text_arrays(obj.get("add_text", {}), _join(path, "add_text"))
    remove = _arrays(obj.get("remove", {}), _join(path, "remove"))
    remove_text = _text_arrays(obj.get("remove_text", {}), _join(path, "remove_text"))
    parts: dict[str, Mapping[str, object]] = {
        "set": values,
        "set_text": texts,
        "unset": dict.fromkeys(unset),
        "add": add,
        "add_text": add_text,
        "remove": remove,
        "remove_text": remove_text,
    }
    for later_index, later in enumerate(_UPDATE_PARTS):
        for earlier in _UPDATE_PARTS[:later_index]:
            if later == "unset" and earlier in ("set", "set_text"):
                continue  # reported above, with the index in unset
            _disjoint(parts[later], parts[earlier], _join(path, later), f"{earlier} and {later}")
    if not any(parts.values()):
        raise _malformed(path, "an update needs set, set_text, unset, add or remove")
    return UpdateOp(
        id=_ref(obj["id"], _join(path, "id")),
        set=values,
        set_text=texts,
        unset=tuple(unset),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
        add=add,
        add_text=add_text,
        remove=remove,
        remove_text=remove_text,
    )


def _link(obj: Mapping[str, JsonValue], path: str) -> LinkOp:
    _keys(obj, path, OP_KEYS["link"])
    return LinkOp(
        id=_ref(obj["id"], _join(path, "id")),
        edge=_edge(obj, path),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _unlink(obj: Mapping[str, JsonValue], path: str) -> UnlinkOp:
    _keys(obj, path, OP_KEYS["unlink"])
    by_id = "edge_id" in obj
    by_rel = "rel" in obj or "target" in obj
    if by_id == by_rel or (by_rel and not ("rel" in obj and "target" in obj)):
        raise _malformed(path, "an unlink needs edge_id, or rel and target")
    edge_id = None
    if by_id:
        edge_path = _join(path, "edge_id")
        try:
            edge_id = EdgeId(parse_id(_str(obj["edge_id"], edge_path)))
        except GraphCliError as exc:
            if exc.code != "usage.invalid_argument":
                raise
            raise _malformed(edge_path, "expected a full edge id") from None
    return UnlinkOp(
        id=_ref(obj["id"], _join(path, "id")),
        edge_id=edge_id,
        rel=_term(obj["rel"], _join(path, "rel")) if by_rel else None,
        target=_ref(obj["target"], _join(path, "target")) if by_rel else None,
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _transition(obj: Mapping[str, JsonValue], path: str) -> TransitionOp:
    _keys(obj, path, OP_KEYS["transition"])
    return TransitionOp(
        id=_ref(obj["id"], _join(path, "id")),
        to=_name(obj["to"], _join(path, "to")),
        attr=None if "attr" not in obj else _name(obj["attr"], _join(path, "attr")),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _supersede(obj: Mapping[str, JsonValue], path: str) -> SupersedeOp:
    _keys(obj, path, OP_KEYS["supersede"])
    targets_path = _join(path, "targets")
    raw = obj["targets"]
    if not isinstance(raw, list | tuple) or not raw:
        raise _malformed(targets_path, "expected a non-empty array")
    targets: list[EntityRef] = []
    for index, item in enumerate(raw):
        ref = _ref(item, _join(targets_path, index))
        if ref in targets:
            raise _malformed(_join(targets_path, index), "listed twice")
        targets.append(ref)
    return SupersedeOp(by=_ref(obj["by"], _join(path, "by")), targets=tuple(targets))


def _retract(obj: Mapping[str, JsonValue], path: str) -> RetractOp:
    _keys(obj, path, OP_KEYS["retract"])
    return RetractOp(
        id=_ref(obj["id"], _join(path, "id")),
        by=None if "by" not in obj else _ref(obj["by"], _join(path, "by")),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _set_alias(obj: Mapping[str, JsonValue], path: str) -> SetAliasOp:
    _keys(obj, path, OP_KEYS["set_alias"])
    return SetAliasOp(
        id=_ref(obj["id"], _join(path, "id")),
        alias=_grammar(obj["alias"], _join(path, "alias"), ALIAS_RE, "an alias PREFIX-0001"),
    )


_PARSERS: Final[Mapping[str, Callable[[Mapping[str, JsonValue], str], Op]]] = {
    "create": _create,
    "update": _update,
    "link": _link,
    "unlink": _unlink,
    "transition": _transition,
    "supersede": _supersede,
    "retract": _retract,
    "set_alias": _set_alias,
}


def _op(value: JsonValue, path: str) -> Op:
    obj = _object(value, path)
    if "op" not in obj:
        raise _malformed(_join(path, "op"), "missing", expected=sorted(_PARSERS))
    kind = _str(obj["op"], _join(path, "op"))
    parser = _PARSERS.get(kind)
    if parser is None:
        raise _malformed(_join(path, "op"), f"unknown op {kind!r}", expected=sorted(_PARSERS))
    return parser(obj, path)


# ---- source and reconcile -----------------------------------------------------------------


def _flag(value: JsonValue, path: str) -> bool:
    if not isinstance(value, bool):
        raise _malformed(path, "expected true or false")
    return value


def _array(value: JsonValue, path: str) -> list[JsonValue] | tuple[JsonValue, ...]:
    if not isinstance(value, list | tuple):
        raise _malformed(path, "expected an array")
    return value


def _key_text(value: JsonValue, path: str, *, minimum: int) -> str:
    """A source key (or ``under``): ``minimum`` to ``MAX_KEY_BYTES`` bytes of printable text,
    no blank at either end (a text value may be trimmed on storage, and the key would then
    never match again) and no leading ``/`` (the stored value is ``<source>/<key>``)."""
    text = _str(value, path)
    if (
        not text.isprintable()
        or text != text.strip()
        or text.startswith("/")
        or not minimum <= len(text.encode("utf-8")) <= MAX_KEY_BYTES
    ):
        raise _malformed(
            path,
            f"expected {minimum} to {MAX_KEY_BYTES} bytes of printable text without a leading"
            " '/' or a blank at either end",
        )
    return text


def _scoped_key(value: JsonValue, path: str, under: str) -> str:
    """An item, ``keep`` or move key: a key that lies inside the scope's ``under``."""
    text = _key_text(value, path, minimum=1)
    if not text.startswith(under):
        raise _malformed(path, "the key lies outside the scope's under", under=under)
    return text


def _source(value: JsonValue) -> SourceRef:
    obj = _object(value, "source")
    if "kind" in obj:
        raise _malformed("source.kind", "set by the program for its own writers, not by a document")
    _keys(obj, "source", SOURCE_KEYS)
    texts: dict[str, str | None] = {}
    for key in ("version", "run"):
        item = None
        if key in obj:
            item = _str(obj[key], f"source.{key}")
            if not is_source_text(item):
                raise _malformed(
                    f"source.{key}",
                    "expected 1 to 128 printable characters without a blank at either end",
                )
        texts[key] = item
    return SourceRef(
        name=_grammar(obj["name"], "source.name", SOURCE_NAME_RE, "a lowercase name"),
        version=texts["version"],
        run=texts["run"],
    )


def _unique_names(
    value: JsonValue, path: str, parse: Callable[[JsonValue, str], str]
) -> tuple[str, ...]:
    """A set of names (or CURIEs) given as an array: valid, listed once, sorted."""
    names: list[str] = []
    for index, item in enumerate(_array(value, path)):
        name = parse(item, _join(path, index))
        if name in names:
            raise _malformed(_join(path, index), "listed twice")
        names.append(name)
    return tuple(sorted(names))


def _value_ref(obj: Mapping[str, JsonValue], path: str) -> ValueRef:
    _keys(obj, path, VALUE_REF_KEYS)
    value = obj["value"]
    value_path = _join(path, "value")
    if isinstance(value, bool | FloatLiteral) or not isinstance(value, str | int):
        raise _malformed(value_path, "expected a string or an integer")
    return ValueRef(
        type=_term(obj["type"], _join(path, "type")),
        attr=_name(obj["attr"], _join(path, "attr")),
        value=value,
    )


def _reconcile_edge(value: JsonValue, path: str, rels: tuple[TermRef, ...]) -> ReconcileEdge:
    obj = _object(value, path)
    _keys(obj, path, ITEM_EDGE_KEYS)
    rel = _term(obj["rel"], _join(path, "rel"))
    if rel not in rels:
        raise _malformed(
            _join(path, "rel"),
            "the relation is not listed in the scope's edges",
            expected=list(rels),
        )
    raw = obj["target"]
    target_path = _join(path, "target")
    target: EntityRef | ValueRef = (
        _value_ref(raw, target_path) if isinstance(raw, Mapping) else _ref(raw, target_path)
    )
    return ReconcileEdge(
        rel=rel,
        target=target,
        qualifiers=_values(obj.get("qualifiers", {}), _join(path, "qualifiers")),
    )


def _reconcile_item(
    value: JsonValue,
    path: str,
    *,
    under: str,
    key_attr: str,
    fields: tuple[str, ...],
    rels: tuple[TermRef, ...],
) -> ReconcileItem:
    obj = _object(value, path)
    _keys(obj, path, ITEM_KEYS)
    attrs = _values(obj.get("attrs", {}), _join(path, "attrs"))
    attrs_text = _texts(obj.get("attrs_text", {}), _join(path, "attrs_text"))
    _disjoint(attrs, attrs_text, _join(path, "attrs_text"), "attrs and attrs_text")
    for part, given in (("attrs", attrs), ("attrs_text", attrs_text)):
        for name in given:
            if name not in fields:
                where = _join(_join(path, part), name)
                if name == key_attr:
                    raise _malformed(where, "the key attribute is set from the key, not as a field")
                raise _malformed(
                    where,
                    "the attribute is not listed in the scope's fields",
                    expected=list(fields),
                )
    edges_path = _join(path, "edges")
    edges: list[ReconcileEdge] = []
    for index, raw in enumerate(_array(obj.get("edges", []), edges_path)):
        edge = _reconcile_edge(raw, _join(edges_path, index), rels)
        if edge in edges:
            raise _malformed(_join(edges_path, index), "listed twice")
        edges.append(edge)
    local = obj.get("local")
    return ReconcileItem(
        key=_scoped_key(obj["key"], _join(path, "key"), under),
        type=None if "type" not in obj else _term(obj["type"], _join(path, "type")),
        local=None if local is None else _name(local, _join(path, "local")),
        attrs=attrs,
        attrs_text=attrs_text,
        edges=tuple(edges),
        valid_from=None
        if "valid_from" not in obj
        else _valid_time(obj["valid_from"], _join(path, "valid_from")),
        valid_to=None
        if "valid_to" not in obj
        else _valid_time(obj["valid_to"], _join(path, "valid_to")),
    )


def _retire(value: JsonValue, path: str) -> RetireSpec:
    if isinstance(value, str) and not isinstance(value, FloatLiteral):
        if value != "retract":
            raise _malformed(path, 'expected "retract" or {transition, attr?, revive?}')
        return RETRACT
    obj = _object(value, path)
    _keys(obj, path, RETIRE_KEYS)
    spec = RetireSpec(
        transition=_name(obj["transition"], _join(path, "transition")),
        attr=None if "attr" not in obj else _name(obj["attr"], _join(path, "attr")),
        revive=None if "revive" not in obj else _name(obj["revive"], _join(path, "revive")),
    )
    if spec.revive == spec.transition:
        raise _malformed(_join(path, "revive"), "the revive state is the retire state")
    return spec


def _move(value: JsonValue, path: str, under: str) -> MoveSpec:
    obj = _object(value, path)
    if "from" in obj and "id" in obj:
        raise _malformed(path, "a move names from or id, not both")
    form = "from" if "from" in obj else "id" if "id" in obj else None
    if form is None:
        raise _malformed(path, "a move needs from (an old key) or id (an adoption)")
    _keys(obj, path, MOVE_KEYS[form])
    to = _scoped_key(obj["to"], _join(path, "to"), under)
    if form == "id":
        return MoveSpec(to=to, id=_ref(obj["id"], _join(path, "id")))
    old = _scoped_key(obj["from"], _join(path, "from"), under)
    if old == to:
        raise _malformed(path, "from and to are the same key")
    return MoveSpec(to=to, old_key=old)


def _expect(value: JsonValue, path: str) -> ExpectSpec:
    obj = _object(value, path)
    _keys(obj, path, EXPECT_KEYS)
    spec = ExpectSpec(
        _opt_int(obj, "min_items", path, minimum=0), _opt_int(obj, "max_items", path, minimum=0)
    )
    if spec.min_items is None and spec.max_items is None:
        raise _malformed(path, "expected min_items or max_items")
    if (
        spec.min_items is not None
        and spec.max_items is not None
        and spec.min_items > spec.max_items
    ):
        raise _malformed(path, "min_items is above max_items")
    return spec


def _reconcile_scope(value: JsonValue, path: str) -> ReconcileScope:
    obj = _object(value, path)
    _keys(obj, path, SCOPE_KEYS)
    scope_type = _term(obj["type"], _join(path, "type"))
    key_attr = _name(obj["key"], _join(path, "key"))
    under = "" if "under" not in obj else _key_text(obj["under"], _join(path, "under"), minimum=0)
    fields = _unique_names(obj["fields"], _join(path, "fields"), _name) if "fields" in obj else ()
    if key_attr in fields:
        raise _malformed(
            _join(path, "fields"), "the key attribute is set from the key, not as a field"
        )
    rels = (
        tuple(TermRef(r) for r in _unique_names(obj["edges"], _join(path, "edges"), _term))
        if "edges" in obj
        else ()
    )
    keep_path = _join(path, "keep")
    keep: list[str] = []
    for index, raw in enumerate(_array(obj.get("keep", []), keep_path)):
        kept = _scoped_key(raw, _join(keep_path, index), under)
        if kept in keep:
            raise _malformed(_join(keep_path, index), "listed twice")
        keep.append(kept)
    moves_path = _join(path, "moves")
    moves = tuple(
        _move(raw, _join(moves_path, index), under)
        for index, raw in enumerate(_array(obj.get("moves", []), moves_path))
    )
    items_path = _join(path, "items")
    items: list[ReconcileItem] = []
    first_at: dict[str, str] = {}
    for index, raw in enumerate(_array(obj["items"], items_path)):
        item_path = _join(items_path, index)
        item = _reconcile_item(
            raw, item_path, under=under, key_attr=key_attr, fields=fields, rels=rels
        )
        if item.key in first_at:
            raise _malformed(
                _join(item_path, "key"),
                "the key is used by an earlier item",
                first_path=_join(first_at[item.key], "key"),
            )
        first_at[item.key] = item_path
        if item.key in keep:
            raise _malformed(
                _join(item_path, "key"), "the key is also listed in keep", keep=_join(path, "keep")
            )
        items.append(item)
    return ReconcileScope(
        type=scope_type,
        key=key_attr,
        items=tuple(items),
        under=under,
        complete=False
        if "complete" not in obj
        else _flag(obj["complete"], _join(path, "complete")),
        fields=fields,
        edges=rels,
        retire=RETRACT if "retire" not in obj else _retire(obj["retire"], _join(path, "retire")),
        allow_retire=_opt_int(obj, "allow_retire", path, minimum=0),
        allow_empty=False
        if "allow_empty" not in obj
        else _flag(obj["allow_empty"], _join(path, "allow_empty")),
        keep=tuple(sorted(keep)),
        moves=moves,
        expect=None if "expect" not in obj else _expect(obj["expect"], _join(path, "expect")),
    )


def _reconcile(value: JsonValue) -> tuple[ReconcileScope, ...]:
    raw = _array(value, "reconcile")
    if not raw:
        raise _malformed("reconcile", "expected a non-empty array")
    if len(raw) > MAX_RECONCILE_SCOPES:
        raise GraphCliError(
            "changeset.too_large",
            f"the document has {len(raw)} reconcile scopes, the limit is {MAX_RECONCILE_SCOPES}",
            details={"limit_scopes": MAX_RECONCILE_SCOPES, "actual_scopes": len(raw)},
        )
    return tuple(
        _reconcile_scope(item, _join("reconcile", index)) for index, item in enumerate(raw)
    )


def parse_changeset(doc: JsonValue, *, max_ops: int = MAX_OPS) -> ChangeSet:
    """Check a parsed change set document strictly (W.1)."""
    obj = _object(doc, "")
    _keys(obj, "", DOCUMENT_KEYS)
    if "schema" in obj:
        schema = _str(obj["schema"], "schema")
        if schema != CHANGESET_SCHEMA:
            if _SCHEMA_RE.match(schema):
                raise GraphCliError(
                    "format.unsupported_version",
                    f"unsupported change set schema {schema!r}",
                    details={"expected": CHANGESET_SCHEMA, "actual": schema},
                )
            raise _malformed("schema", "unknown schema", expected=CHANGESET_SCHEMA, actual=schema)
    reason = None
    if "reason" in obj:
        reason = _str(obj["reason"], "reason")
        if not reason.strip() or len(reason) > MAX_REASON:
            raise _malformed("reason", f"expected 1 to {MAX_REASON} characters")
    key = None
    if "idempotency_key" in obj:
        key = _grammar(
            obj["idempotency_key"], "idempotency_key", IDEMPOTENCY_KEY_RE, "[A-Za-z0-9._:-]{1,128}"
        )
    if "ops" not in obj and "reconcile" not in obj:
        raise _malformed("ops", "missing: a document needs ops, reconcile or both")
    source = None
    if "source" in obj:
        source = _source(obj["source"])
    elif "reconcile" in obj:
        raise _malformed("source", "missing: a document with reconcile names its source")
    ops: tuple[Op, ...] = ()
    if "ops" in obj:
        raw_ops = obj["ops"]
        if not isinstance(raw_ops, list | tuple) or not raw_ops:
            raise _malformed("ops", "expected a non-empty array")
        if len(raw_ops) > max_ops:
            raise GraphCliError(
                "changeset.too_large",
                f"the change set has {len(raw_ops)} ops, the limit is {max_ops}",
                details={"limit_ops": max_ops, "actual_ops": len(raw_ops)},
            )
        ops = tuple(_op(item, _join("ops", index)) for index, item in enumerate(raw_ops))
    return ChangeSet(
        ops=ops,
        reason=reason,
        idempotency_key=key,
        expect_seq=_opt_int(obj, "expect_seq", "", minimum=0),
        source=source,
        reconcile=_reconcile(obj["reconcile"]) if "reconcile" in obj else (),
    )


# ---- canonical form ------------------------------------------------------------------------


def _ref_json(ref: EntityRef) -> str:
    if isinstance(ref, AbbrevRef):
        raise GraphCliError("internal.error", "an abbreviation cannot appear in a change set")
    return ref_text(ref)


def _edge_json(edge: EdgeSpec) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"rel": edge.rel, "target": _ref_json(edge.target)}
    if edge.qualifiers:
        data["qualifiers"] = dict(edge.qualifiers)
    if edge.target_rev is not None:
        data["target_rev"] = edge.target_rev
    return data


def _put(data: dict[str, JsonValue], key: str, value: JsonValue) -> None:
    if value is not None and value != {} and value != []:
        data[key] = value


def op_to_json(op: Op) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"op": op.OP}
    match op:
        case CreateOp():
            data["type"] = op.type
            _put(data, "local", op.local)
            _put(data, "attrs", dict(op.attrs))
            _put(data, "attrs_text", dict(op.attrs_text))
            _put(data, "edges", [_edge_json(e) for e in op.edges])
            _put(data, "valid_from", op.valid_from)
            _put(data, "valid_to", op.valid_to)
        case UpdateOp():
            data["id"] = _ref_json(op.id)
            _put(data, "set", dict(op.set))
            _put(data, "set_text", dict(op.set_text))
            _put(data, "unset", list(op.unset))
            for key in ("add", "add_text", "remove", "remove_text"):
                _put(data, key, {name: list(items) for name, items in getattr(op, key).items()})
            _put(data, "expect_rev", op.expect_rev)
        case LinkOp():
            data["id"] = _ref_json(op.id)
            data.update(_edge_json(op.edge))
            _put(data, "expect_rev", op.expect_rev)
        case UnlinkOp():
            data["id"] = _ref_json(op.id)
            _put(data, "edge_id", None if op.edge_id is None else str(op.edge_id))
            _put(data, "rel", op.rel)
            _put(data, "target", None if op.target is None else _ref_json(op.target))
            _put(data, "expect_rev", op.expect_rev)
        case TransitionOp():
            data["id"] = _ref_json(op.id)
            data["to"] = op.to
            _put(data, "attr", op.attr)
            _put(data, "expect_rev", op.expect_rev)
        case SupersedeOp():
            data["by"] = _ref_json(op.by)
            data["targets"] = [_ref_json(t) for t in op.targets]
        case RetractOp():
            data["id"] = _ref_json(op.id)
            _put(data, "by", None if op.by is None else _ref_json(op.by))
            _put(data, "expect_rev", op.expect_rev)
        case SetAliasOp():
            data["id"] = _ref_json(op.id)
            data["alias"] = op.alias
    return data


def _target_json(target: EntityRef | ValueRef) -> JsonValue:
    if isinstance(target, ValueRef):
        return {"type": target.type, "attr": target.attr, "value": target.value}
    return _ref_json(target)


def _reconcile_edge_json(edge: ReconcileEdge) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"rel": edge.rel, "target": _target_json(edge.target)}
    _put(data, "qualifiers", dict(edge.qualifiers))
    return data


def _item_json(item: ReconcileItem) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"key": item.key}
    _put(data, "type", item.type)
    _put(data, "local", item.local)
    _put(data, "attrs", dict(item.attrs))
    _put(data, "attrs_text", dict(item.attrs_text))
    _put(data, "edges", [_reconcile_edge_json(e) for e in item.edges])
    _put(data, "valid_from", item.valid_from)
    _put(data, "valid_to", item.valid_to)
    return data


def _move_json(move: MoveSpec) -> dict[str, JsonValue]:
    if move.id is not None:
        return {"id": _ref_json(move.id), "to": move.to}
    assert move.old_key is not None
    return {"from": move.old_key, "to": move.to}


def scope_to_json(scope: ReconcileScope) -> dict[str, JsonValue]:
    """The canonical form of one scope: defaults omitted, sets sorted, items and moves in the
    order given (the plan orders its ops by them)."""
    data: dict[str, JsonValue] = {"type": scope.type, "key": scope.key}
    if scope.under:
        data["under"] = scope.under
    if scope.complete:
        data["complete"] = True
    _put(data, "fields", list(scope.fields))
    _put(data, "edges", list(scope.edges))
    if scope.retire != RETRACT:
        retire: dict[str, JsonValue] = {"transition": scope.retire.transition}
        _put(retire, "attr", scope.retire.attr)
        _put(retire, "revive", scope.retire.revive)
        data["retire"] = retire
    _put(data, "allow_retire", scope.allow_retire)
    if scope.allow_empty:
        data["allow_empty"] = True
    _put(data, "keep", list(scope.keep))
    _put(data, "moves", [_move_json(m) for m in scope.moves])
    if scope.expect is not None:
        expect: dict[str, JsonValue] = {}
        _put(expect, "min_items", scope.expect.min_items)
        _put(expect, "max_items", scope.expect.max_items)
        data["expect"] = expect
    data["items"] = [_item_json(i) for i in scope.items]
    return data


def changeset_to_json(cs: ChangeSet) -> dict[str, JsonValue]:
    """The canonical form (C.6) of the frame and its ops: what the tx stores and what is
    hashed. The ``source`` and the ``reconcile`` part are not in it: the tx records the ops a
    reconcile expanded to, and the source in ``request.source``."""
    data: dict[str, JsonValue] = {
        "schema": CHANGESET_SCHEMA,
        "ops": [op_to_json(op) for op in cs.ops],
    }
    _put(data, "reason", cs.reason)
    _put(data, "idempotency_key", cs.idempotency_key)
    _put(data, "expect_seq", cs.expect_seq)
    return data


def changeset_hash(cs: ChangeSet) -> str:
    """C.2 over the canonical form (B4.4)."""
    return hash_value(changeset_to_json(cs))


def document_to_json(cs: ChangeSet) -> dict[str, JsonValue]:
    """The canonical form of the whole document: ``changeset_to_json`` plus ``source`` and
    ``reconcile``, each only when present, so a document without them has the bytes of its
    change set. Parsing it gives ``cs`` again."""
    data = changeset_to_json(cs)
    if not cs.ops:
        del data["ops"]  # a reconcile-only document has no ops key
    if cs.source is not None:
        data["source"] = cs.source.to_json()
    if cs.reconcile:
        data["reconcile"] = [scope_to_json(s) for s in cs.reconcile]
    return data


def document_hash(cs: ChangeSet) -> str:
    """C.2 over ``document_to_json``: what a request digest of a reconcile document covers
    (C.10)."""
    return hash_value(document_to_json(cs))
