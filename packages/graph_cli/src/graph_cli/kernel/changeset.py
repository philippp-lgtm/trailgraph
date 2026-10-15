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

Input readers parse JSON floats into ``FloatLiteral`` so that a float in an
attribute or qualifier value reaches the validator as
``data.structural.float_not_allowed`` (C.1); a float anywhere else is
``changeset.malformed``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar, Final

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

CHANGESET_SCHEMA: Final = "graph_cli.changeset/1"
MAX_OPS: Final = 1000
MAX_BYTES: Final = 8 * 1024 * 1024
MAX_REASON: Final = 2000

NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # attributes, qualifiers, states, locals
IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SCHEMA_RE: Final = re.compile(r"^graph_cli\.changeset/[0-9]+$")

EMPTY_TEXT: Final[Mapping[str, str]] = MappingProxyType({})


class FloatLiteral(str):
    """A JSON number with a fraction or exponent, kept as its source text.

    Only input readers create it. It never reaches a persisted document:
    the validator refuses it wherever a value may appear.
    """

    __slots__ = ()


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
class ChangeSet:
    ops: tuple[Op, ...]
    reason: str | None = None
    idempotency_key: str | None = None
    expect_seq: int | None = None


def op_action(op: Op) -> Action:
    """The action an op is authorized and audited as (B3 op catalog)."""
    return OP_ACTIONS[op.OP]


@dataclass(frozen=True, slots=True)
class OpEffect:
    """What one op did to the working copy; the validator judges it (W4).

    Produced by ``temporal_knowledge.normalize``, read by the data validator;
    it lives here because the validator sits below temporal_knowledge.

    ``entity`` is the op's subject (``by`` for supersede). ``applied`` is
    false when normalize skipped the op (a ``data.temporal.*`` finding) or
    could not apply it (``problem``: transition without a lifecycle
    attribute, unlink without exactly one matching edge).
    """

    index: int
    op: str
    entity: EntityId
    applied: bool = True
    names: tuple[str, ...] = ()  # create/update: attributes given; update: set and unset
    attr: str | None = None  # transition
    from_state: JsonValue = None
    to_state: str | None = None
    initial: tuple[tuple[str, JsonValue], ...] = ()  # create: lifecycle values after the op
    edge_id: EdgeId | None = None  # link: added; unlink: removed
    rel: TermRef | None = None
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


def _keys(
    obj: Mapping[str, JsonValue], path: str, *, required: Sequence[str], optional: Sequence[str]
) -> None:
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
    _keys(
        obj,
        path,
        required=("op", "type"),
        optional=("local", "attrs", "attrs_text", "edges", "valid_from", "valid_to"),
    )
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
        _keys(
            edge_obj, edge_path, required=("rel", "target"), optional=("qualifiers", "target_rev")
        )
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


def _update(obj: Mapping[str, JsonValue], path: str) -> UpdateOp:
    _keys(obj, path, required=("op", "id"), optional=("set", "set_text", "unset", "expect_rev"))
    values = _values(obj.get("set", {}), _join(path, "set"))
    texts = _texts(obj.get("set_text", {}), _join(path, "set_text"))
    _disjoint(values, texts, _join(path, "set_text"), "set and set_text")
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
    if not (values or texts or unset):
        raise _malformed(path, "an update needs set, set_text or unset")
    return UpdateOp(
        id=_ref(obj["id"], _join(path, "id")),
        set=values,
        set_text=texts,
        unset=tuple(unset),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _link(obj: Mapping[str, JsonValue], path: str) -> LinkOp:
    _keys(
        obj,
        path,
        required=("op", "id", "rel", "target"),
        optional=("qualifiers", "target_rev", "expect_rev"),
    )
    return LinkOp(
        id=_ref(obj["id"], _join(path, "id")),
        edge=_edge(obj, path),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _unlink(obj: Mapping[str, JsonValue], path: str) -> UnlinkOp:
    _keys(obj, path, required=("op", "id"), optional=("edge_id", "rel", "target", "expect_rev"))
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
    _keys(obj, path, required=("op", "id", "to"), optional=("attr", "expect_rev"))
    return TransitionOp(
        id=_ref(obj["id"], _join(path, "id")),
        to=_name(obj["to"], _join(path, "to")),
        attr=None if "attr" not in obj else _name(obj["attr"], _join(path, "attr")),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _supersede(obj: Mapping[str, JsonValue], path: str) -> SupersedeOp:
    _keys(obj, path, required=("op", "by", "targets"), optional=())
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
    _keys(obj, path, required=("op", "id"), optional=("by", "expect_rev"))
    return RetractOp(
        id=_ref(obj["id"], _join(path, "id")),
        by=None if "by" not in obj else _ref(obj["by"], _join(path, "by")),
        expect_rev=_opt_int(obj, "expect_rev", path, minimum=1),
    )


def _set_alias(obj: Mapping[str, JsonValue], path: str) -> SetAliasOp:
    _keys(obj, path, required=("op", "id", "alias"), optional=())
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


def parse_changeset(doc: JsonValue, *, max_ops: int = MAX_OPS) -> ChangeSet:
    """Check a parsed change set document strictly (W.1)."""
    obj = _object(doc, "")
    _keys(
        obj, "", required=("ops",), optional=("schema", "reason", "idempotency_key", "expect_seq")
    )
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
    raw_ops = obj["ops"]
    if not isinstance(raw_ops, list | tuple) or not raw_ops:
        raise _malformed("ops", "expected a non-empty array")
    if len(raw_ops) > max_ops:
        raise GraphCliError(
            "changeset.too_large",
            f"the change set has {len(raw_ops)} ops, the limit is {max_ops}",
            details={"limit_ops": max_ops, "actual_ops": len(raw_ops)},
        )
    return ChangeSet(
        ops=tuple(_op(item, _join("ops", index)) for index, item in enumerate(raw_ops)),
        reason=reason,
        idempotency_key=key,
        expect_seq=_opt_int(obj, "expect_seq", "", minimum=0),
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


def changeset_to_json(cs: ChangeSet) -> dict[str, JsonValue]:
    """The canonical form (C.6): what the tx stores and what is hashed."""
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
