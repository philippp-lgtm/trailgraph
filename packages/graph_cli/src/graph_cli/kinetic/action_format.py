"""Authored format ``graph_cli.action/1``: typed action model and its parser.

An action is a named write recipe: typed parameters, preconditions on the
current state of the entities it is given, and a template of change-set ops
(``graph_cli.changeset/1``) where a parameter stands for a reference or a
value. Parsing checks the shape of one file (keys, value types, grammars,
lengths, where a placeholder may stand, that every entity reference is a
placeholder or an ``@local``); everything that needs the ontology is checked by
``action_compile``. Every defect of a file becomes a ``Problem`` instead of an
exception, so one run reports all of them. A definition also reads back from a
compiled catalog entry (``ActionDef.from_json``), which is how a published
catalog is checked again against another model.

Placeholders are YAML strings that are exactly ``$<param>``. They may stand
where a whole reference or a whole value goes (``Use.role``); never in a type,
relation, attribute name, state or map key. ``expect_rev``, ``target_rev`` and
``edge_id`` depend on the state and cannot appear in a template. The one
dynamic form is ``{op: unlink, id: $p, rel: <relation>, direction: out|in,
all: true}``: ``expand`` replaces it by one exact unlink per current edge.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonObject, JsonValue
from graph_cli.kernel.changeset import OP_KEYS
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import LOCAL_RE
from graph_cli.kernel.json_access import (
    as_object,
    opt_str,
    req_bool,
    req_list,
    req_str,
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
)
from graph_cli.kinetic.params import (
    MAX_DETAIL,
    PARAM_RE,
    ParamDef,
    Parser,
    Problem,
    is_null,
)
from graph_cli.ontology_layer.expressions import Cond, parse_conditions
from graph_cli.ontology_layer.reader import Reader
from graph_cli.ontology_layer.report import Report

ACTION_SCHEMA: Final = "graph_cli.action/1"
CATALOG_SCHEMA: Final = "graph_cli.action_catalog/1"

REASONS: Final = ("optional", "required")
PLACEHOLDER_RE: Final = re.compile(r"^\$[a-z][a-z0-9_]{0,31}$")
PRECONDITION_ID_RE: Final = PARAM_RE
MAX_PRECONDITION_SUMMARY: Final = 200
DIRECTIONS: Final = ("out", "in")
EDGE_TARGET: Final = "edges.target"  # ``Use.field`` of the target of an edge a create writes

_TOP_KEYS: Final = (
    "schema",
    "name",
    "module",
    "summary",
    "use_when",
    "params",
    "preconditions",
    "ops",
    "reason",
    "examples",
    "featured",
    "status",
    "replaced_by",
)
_PRECONDITION_KEYS: Final = ("id", "on", "summary", "require")
# Keys of the change-set grammar that depend on the state at the time of the write, and the
# keys of the document around the ops: neither can be part of a template.
_STATE_KEYS: Final = ("expect_rev", "target_rev", "edge_id")
_DOCUMENT_KEYS: Final = ("reason", "idempotency_key", "expect_seq", "schema")
_DYNAMIC_KEYS: Final = ("op", "id", "rel", "direction", "all")

Path = tuple[str | int, ...]


def path_text(path: Path) -> str:
    """``("ops", 0, "qualifiers", "since")`` as ``ops[0].qualifiers.since``."""
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text


@dataclass(frozen=True, slots=True)
class Use:
    """A placeholder at a legal position of an op template.

    ``role``: ``entity`` (one reference), ``entities`` (a whole list of references:
    ``targets``), ``value`` (one whole value, a list value for a ``many`` attribute),
    ``item`` (one element of an ``add``/``remove`` list), ``items`` (the whole list),
    ``time`` (``valid_from``/``valid_to``), ``alias``. ``field`` is the key of the op
    it stands in; ``key`` the attribute or qualifier; ``rel`` the authored relation of
    the edge a qualifier or target belongs to; ``text`` is true in the ``*_text`` forms.
    ``droppable``: the position is a map entry that an absent optional parameter removes.
    """

    path: Path
    name: str
    role: str
    field: str
    key: str | None = None
    rel: str | None = None
    text: bool = False
    droppable: bool = False


@dataclass(frozen=True, slots=True)
class OpScan:
    """What a walk over one op template found."""

    uses: tuple[Use, ...]
    locals_used: tuple[tuple[Path, str], ...]  # (path, local name) of ``@local`` references
    literals: tuple[tuple[Path, str], ...]  # entity references that are neither
    misplaced: tuple[Path, ...]  # ``$...`` strings (or keys) at positions that take none
    shape: tuple[tuple[Path, str], ...]  # (path, message): a value of the wrong kind


@dataclass(frozen=True, slots=True)
class PreconditionDef:
    id: str
    on: str  # a ref parameter
    summary: str
    require: JsonValue  # the authored condition list as JSON
    conds: tuple[Cond, ...] = field(default=(), compare=False)  # parsed, not stored

    def source_json(self) -> dict[str, JsonValue]:
        return {"id": self.id, "on": self.on, "summary": self.summary, "require": self.require}


@dataclass(frozen=True, slots=True)
class ExampleDef:
    request: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"request": self.request}


@dataclass(frozen=True, slots=True)
class ActionDef:
    name: str
    module: str | None  # module id or prefix as authored
    summary: str
    use_when: str
    params: tuple[ParamDef, ...]
    preconditions: tuple[PreconditionDef, ...]
    ops: tuple[JsonObject, ...]  # the authored template, JSON-like
    examples: tuple[ExampleDef, ...]
    reason: str = "optional"  # one of REASONS
    featured: bool = False
    status: str = "active"  # one of STATUSES
    replaced_by: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """The declaration without ops and preconditions (the catalog entry adds both)."""
        return {
            "name": self.name,
            "module": self.module,
            "summary": self.summary,
            "use_when": self.use_when,
            "status": self.status,
            "replaced_by": self.replaced_by,
            "featured": self.featured,
            "reason": self.reason,
            "params": [p.to_json() for p in self.params],
            "examples": [e.to_json() for e in self.examples],
        }

    def source_json(self) -> dict[str, JsonValue]:
        """The authored ops and preconditions: enough to compile the action again."""
        return {
            "ops": [dict(op) for op in self.ops],
            "preconditions": [p.source_json() for p in self.preconditions],
        }

    @classmethod
    def from_json(cls, data: JsonObject, source: JsonObject) -> ActionDef:
        """Read back ``to_json`` and ``source_json``; ``JsonShapeError`` for a document of
        another shape, ``GraphCliError`` (``action.invalid_condition``) for stored
        conditions that no longer parse."""
        path = f"{data.get('name')}."
        preconditions = []
        for index, item in enumerate(req_list(source, "preconditions", path)):
            body = as_object(item, f"{path}preconditions")
            require = body.get("require")
            conds, problems = parse_require(
                json_node(require), f"preconditions[{index}].require", defn_name(data), 0
            )
            if conds is None:
                raise GraphCliError("action.invalid_condition", problems[0].message)
            preconditions.append(
                PreconditionDef(
                    req_str(body, "id", path),
                    req_str(body, "on", path),
                    req_str(body, "summary", path),
                    require,
                    conds,
                )
            )
        return cls(
            name=req_str(data, "name"),
            module=opt_str(data, "module", path),
            summary=req_str(data, "summary", path),
            use_when=req_str(data, "use_when", path),
            params=tuple(
                ParamDef.from_json(as_object(p, f"{path}params"), f"{path}params.")
                for p in req_list(data, "params", path)
            ),
            preconditions=tuple(preconditions),
            ops=tuple(as_object(o, f"{path}ops") for o in req_list(source, "ops", path)),
            examples=tuple(
                ExampleDef(req_str(o, "request", path))
                for o in (as_object(e, f"{path}examples") for e in req_list(data, "examples", path))
            ),
            reason=req_str(data, "reason", path),
            featured=req_bool(data, "featured", path),
            status=req_str(data, "status", path),
            replaced_by=opt_str(data, "replaced_by", path),
        )


def defn_name(data: JsonObject) -> str:
    name = data.get("name")
    return name if isinstance(name, str) else "(stored)"


def action_name(file_name: str) -> str:
    """The name a file stands for: its name without the ``.yaml`` suffix."""
    return file_name.removesuffix(FILE_SUFFIX)


def load_action(file_name: str, content: str | bytes) -> tuple[ActionDef | None, list[Problem]]:
    """Parse one action file. The definition is ``None`` when any problem was found."""
    parse = _Parse(action_name(file_name))
    try:
        root = load_strict(content)
    except YamlError as exc:
        parse.add("action.invalid_yaml", None, str(exc), line=exc.line)
        return None, parse.problems
    if root is None:
        parse.add("action.invalid_yaml", None, "the file is empty")
        return None, parse.problems
    defn = parse.read(root, file_name)
    return (defn if not parse.problems else None), parse.problems


# ---- YAML nodes and JSON ---------------------------------------------------------------


def node_json(node: YamlNode) -> JsonValue:
    """A YAML node as JSON (YAML 1.2 core typing; a quoted scalar stays a string)."""
    if isinstance(node, YamlMap):
        return {key: node_json(value) for key, value in node.items.items()}
    if isinstance(node, YamlSeq):
        return [node_json(item) for item in node.items]
    if node.is_null():
        return None
    boolean = node.as_bool()
    if boolean is not None:
        return boolean
    number = node.as_int()
    if number is not None and abs(number) <= MAX_SAFE_INT:
        return number
    return node.text


def json_node(value: JsonValue) -> YamlNode:
    """The inverse of ``node_json`` for the values it produces."""
    if isinstance(value, Mapping):
        items = {str(k): json_node(v) for k, v in value.items()}
        return YamlMap(items, dict.fromkeys(items, 0), 0)
    if isinstance(value, list | tuple):
        return YamlSeq(tuple(json_node(v) for v in value), 0)
    if value is None:
        return YamlScalar("null", True, 0)
    if isinstance(value, bool):
        return YamlScalar("true" if value else "false", True, 0)
    if isinstance(value, int):
        return YamlScalar(str(value), True, 0)
    return YamlScalar(str(value), False, 0)


def parse_require(
    node: YamlNode | None, path: str, name: str, line: int
) -> tuple[tuple[Cond, ...] | None, list[Problem]]:
    """The conditions of a precondition through the ontology layer's parser; every
    finding becomes an ``action.invalid_condition`` problem."""
    report = Report()
    conds = parse_conditions(Reader(report, name), node, path)
    problems = [
        Problem(
            name,
            "action.invalid_condition",
            f"{finding.target['path']}: {finding.message}",
            str(finding.target["path"]),
            line or None,
            kind="action",
        )
        for finding in report.findings
    ]
    return (conds if not problems else None), problems


# ---- op templates ----------------------------------------------------------------------


def placeholder_name(token: str) -> str:
    """The parameter a placeholder string names (``$person`` is ``person``)."""
    return token[1:]


class _Scan:
    """Walks one op template and records placeholders, references and shape defects."""

    def __init__(self, op: Mapping[str, JsonValue], index: int) -> None:
        self.op = op
        self.base: Path = ("ops", index)
        self.uses: list[Use] = []
        self.locals_used: list[tuple[Path, str]] = []
        self.literals: list[tuple[Path, str]] = []
        self.shape: list[tuple[Path, str]] = []
        self.rel: str | None = None

    def result(self) -> OpScan:
        placed = {u.path for u in self.uses}
        misplaced = tuple(p for p in _dollars(self.op, self.base) if p not in placed)
        return OpScan(
            tuple(self.uses),
            tuple(self.locals_used),
            tuple(self.literals),
            misplaced,
            tuple(self.shape),
        )

    def bad(self, path: Path, message: str) -> None:
        self.shape.append((path, message))

    def run(self) -> OpScan:
        kind = self.op.get("op")
        op = self.op
        if kind == "create":
            self.values("attrs")
            self.values("attrs_text", text=True)
            self.edges()
            for key in ("valid_from", "valid_to"):
                self.single(key, "time", droppable=True)
        elif kind == "update":
            self.entity("id")
            for key in ("set", "set_text"):
                self.values(key, text=key.endswith("_text"))
            for key in ("add", "remove", "add_text", "remove_text"):
                self.lists(key)
        elif kind == "link":
            self.rel = _rel_text(op)
            self.entity("id")
            self.entity("target")
            self.values("qualifiers", rel=self.rel)
        elif kind == "unlink":
            self.entity("id")
            if "target" in op:
                self.entity("target")
        elif kind == "transition" or kind == "retract":
            self.entity("id")
            if "by" in op:
                self.entity("by")
        elif kind == "supersede":
            self.entity("by")
            self.entity_list("targets")
        elif kind == "set_alias":
            self.entity("id")
            self.single("alias", "alias", droppable=False)
        return self.result()

    # Positions

    def at(self, key: str) -> Path:
        return (*self.base, key)

    def entity(self, key: str, *, container: Mapping[str, JsonValue] | None = None) -> None:
        self._entity((container if container is not None else self.op).get(key), self.at(key), key)

    def _entity(self, value: JsonValue, path: Path, key: str, role: str = "entity") -> None:
        if not isinstance(value, str):
            self.bad(path, "expected a string: a $parameter or an @local")
        elif value.startswith("$"):
            self.uses.append(Use(path, placeholder_name(value), role, key, rel=self.rel))
        elif LOCAL_RE.match(value):
            self.locals_used.append((path, value[1:]))
        else:
            self.literals.append((path, value))

    def entity_list(self, key: str) -> None:
        value = self.op.get(key)
        path = self.at(key)
        if isinstance(value, str) and value.startswith("$"):
            self.uses.append(Use(path, placeholder_name(value), "entities", key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._entity(item, (*path, i), key)
        else:
            self.bad(path, "expected a list of references or a $parameter that takes a list")

    def single(self, key: str, role: str, *, droppable: bool) -> None:
        value = self.op.get(key)
        if isinstance(value, str) and value.startswith("$"):
            self.uses.append(
                Use(self.at(key), placeholder_name(value), role, key, droppable=droppable)
            )

    def values(
        self,
        key: str,
        *,
        text: bool = False,
        rel: str | None = None,
        container: Mapping[str, JsonValue] | None = None,
        base: Path | None = None,
    ) -> None:
        source = container if container is not None else self.op
        path = (*(base if base is not None else self.base), key)
        value = source.get(key)
        if value is None:
            return
        if not isinstance(value, Mapping):
            self.bad(path, "expected a mapping")
            return
        for name, item in value.items():
            if isinstance(item, str) and item.startswith("$"):
                self.uses.append(
                    Use(
                        (*path, name),
                        placeholder_name(item),
                        "value",
                        key,
                        key=name,
                        rel=rel,
                        text=text,
                        droppable=True,
                    )
                )

    def lists(self, key: str) -> None:
        value = self.op.get(key)
        path = self.at(key)
        if value is None:
            return
        if not isinstance(value, Mapping):
            self.bad(path, "expected a mapping")
            return
        text = key.endswith("_text")
        for name, items in value.items():
            where = (*path, name)
            if isinstance(items, str) and items.startswith("$"):
                self.uses.append(
                    Use(
                        where,
                        placeholder_name(items),
                        "items",
                        key,
                        key=name,
                        text=text,
                        droppable=True,
                    )
                )
            elif isinstance(items, list):
                for i, item in enumerate(items):
                    if isinstance(item, str) and item.startswith("$"):
                        self.uses.append(
                            Use(
                                (*where, i),
                                placeholder_name(item),
                                "item",
                                key,
                                key=name,
                                text=text,
                            )
                        )
            else:
                self.bad(where, "expected a list of values or a $parameter that takes a list")

    def edges(self) -> None:
        value = self.op.get("edges")
        path = self.at("edges")
        if value is None:
            return
        if not isinstance(value, list):
            self.bad(path, "expected a list")
            return
        for i, edge in enumerate(value):
            where = (*path, i)
            if not isinstance(edge, Mapping):
                self.bad(where, "expected a mapping")
                continue
            rel = edge.get("rel")
            self.rel = rel if isinstance(rel, str) else None
            if "target" in edge:
                self._entity(edge["target"], (*where, "target"), EDGE_TARGET)
            self.values("qualifiers", rel=self.rel, container=edge, base=where)
            self.rel = None


def _rel_text(op: Mapping[str, JsonValue]) -> str | None:
    rel = op.get("rel")
    return rel if isinstance(rel, str) else None


def _dollars(value: JsonValue, path: Path) -> Iterator[Path]:
    """The paths of every string (or map key) that starts with ``$``."""
    if isinstance(value, str):
        if value.startswith("$"):
            yield path
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key.startswith("$"):
                yield (*path, key)
            yield from _dollars(item, (*path, key))
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _dollars(item, (*path, i))


def scan_op(op: Mapping[str, JsonValue], index: int) -> OpScan:
    """Placeholders, references and defects of one op template (known ``op`` kind)."""
    return _Scan(op, index).run()


def is_dynamic_unlink(op: Mapping[str, JsonValue]) -> bool:
    return op.get("op") == "unlink" and "all" in op


def walk_path(root: JsonValue, path: Sequence[str | int]) -> JsonValue:
    """The value at ``path`` below ``root``; ``KeyError`` when there is none."""
    value = root
    for part in path:
        if isinstance(value, Mapping) and isinstance(part, str):  # noqa: SIM114
            value = value[part]
        elif isinstance(value, list) and isinstance(part, int):
            value = value[part]
        else:
            raise KeyError(part)
    return value


class Absent:
    """The value of an optional parameter that was not given (see ``substitute``)."""

    __slots__ = ()


ABSENT: Final = Absent()


def substitute(
    op: Mapping[str, JsonValue],
    uses: Sequence[Use],
    value_of: Callable[[Use], JsonValue | Absent],
) -> JsonObject:
    """A copy of ``op`` with each placeholder replaced by ``value_of(use)``.

    ``ABSENT`` removes the map entry the placeholder stands in (only a
    ``droppable`` position has one; anything else is a defect of the caller).
    """
    out = copy.deepcopy(dict(op))
    for use in uses:
        value = value_of(use)
        parent = walk_path(out, use.path[2:-1])
        last = use.path[-1]
        if isinstance(value, Absent):
            if not (use.droppable and isinstance(parent, dict) and isinstance(last, str)):
                raise GraphCliError("internal.error", f"{path_text(use.path)} cannot be dropped")
            del parent[last]
        elif isinstance(parent, dict) and isinstance(last, str):  # noqa: SIM114
            parent[last] = value
        elif isinstance(parent, list) and isinstance(last, int):
            parent[last] = value
        else:
            raise GraphCliError("internal.error", f"{path_text(use.path)} is not a position")
    return out


def spread_edges(instance: JsonObject) -> JsonObject:
    """The op with every ``edges[].target`` that holds a list (a ``many`` parameter) replaced by
    one edge per value: same ``rel`` and ``qualifiers``, values in order, each once. Only a
    ``create`` has such edges; any other op, or one without a list target, is returned as is.
    An empty list writes no edge."""
    edges = instance.get("edges")
    if instance.get("op") != "create" or not isinstance(edges, list):
        return instance
    if not any(isinstance(e, dict) and isinstance(e.get("target"), list) for e in edges):
        return instance
    spread: list[JsonValue] = []
    for edge in edges:
        target = edge.get("target") if isinstance(edge, dict) else None
        if not isinstance(edge, dict) or not isinstance(target, list):
            spread.append(edge)
            continue
        for value in dict.fromkeys(str(v) for v in target):
            spread.append({**copy.deepcopy(edge), "target": value})
    return {**instance, "edges": spread}


# ---- the parser ------------------------------------------------------------------------


class _Parse(Parser):
    """Collects the problems of one action file."""

    def __init__(self, action: str) -> None:
        super().__init__("action", action)

    def read(self, root: YamlNode, file_name: str) -> ActionDef | None:
        top = self.mapping(root, "", _TOP_KEYS)
        if top is None:
            return None
        line = root.line
        for key in ("schema", "name", "summary", "use_when", "ops", "examples"):
            self.require(top, key, "", line)
        if "schema" in top:
            schema = self.text(top["schema"], "schema")
            if schema is not None and schema != ACTION_SCHEMA:
                self.add(
                    "action.unsupported_schema",
                    "schema",
                    f"{schema!r} is not {ACTION_SCHEMA}",
                    line=top["schema"].line,
                )
        name = self.name_of(top.get("name"), "name", NAME_RE) if "name" in top else None
        if name is not None and file_name != name + FILE_SUFFIX:
            self.add(
                "action.name_mismatch",
                "name",
                f"name {name!r} needs the file name {name + FILE_SUFFIX}, not {file_name}",
                line=top["name"].line,
            )
        module = None
        if "module" in top:
            module = self.name_of(top["module"], "module", MODULE_REF_RE)
        summary = self.text(top.get("summary"), "summary", limit=MAX_SUMMARY, one_line=True)
        use_when = self.text(top.get("use_when"), "use_when", limit=MAX_USE_WHEN)
        params = self.params(top["params"]) if "params" in top else ()
        preconditions = self.preconditions(top["preconditions"]) if "preconditions" in top else ()
        ops = self.ops(top["ops"]) if "ops" in top else None
        reason = self.choice(top["reason"], "reason", REASONS) if "reason" in top else "optional"
        examples = self.examples(top["examples"]) if "examples" in top else None
        featured = self.boolean(top["featured"], "featured") if "featured" in top else False
        status = self.choice(top["status"], "status", STATUSES) if "status" in top else "active"
        replaced_by = None
        if "replaced_by" in top and not is_null(top["replaced_by"]):
            replaced_by = self.name_of(top["replaced_by"], "replaced_by", NAME_RE)
        if replaced_by is not None and status != "deprecated":
            self.invalid("replaced_by", "is only allowed with status: deprecated")
        if replaced_by is not None and replaced_by == name:
            self.invalid("replaced_by", "an action cannot replace itself")
        if self.problems:
            return None
        assert name and summary and use_when and ops and examples and reason
        assert params is not None and preconditions is not None
        assert featured is not None and status is not None
        return ActionDef(
            name=name,
            module=module,
            summary=summary,
            use_when=use_when,
            params=params,
            preconditions=preconditions,
            ops=ops,
            examples=examples,
            reason=reason,
            featured=featured,
            status=status,
            replaced_by=replaced_by,
        )

    # Preconditions

    def preconditions(self, node: YamlNode) -> tuple[PreconditionDef, ...] | None:
        if is_null(node):
            return ()
        items = self.sequence(node, "preconditions")
        if items is None:
            return None
        result: list[PreconditionDef] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            path = f"preconditions[{index}]"
            body = self.mapping(item, path, _PRECONDITION_KEYS)
            if body is None:
                continue
            for key in _PRECONDITION_KEYS:
                self.require(body, key, path, item.line)
            ident = self.name_of(body.get("id"), f"{path}.id", PRECONDITION_ID_RE)
            if ident is not None and ident in seen:
                self.invalid(f"{path}.id", f"id {ident!r} is used twice", body["id"])
                ident = None
            if ident is not None:
                seen.add(ident)
            on = self.name_of(body.get("on"), f"{path}.on", PARAM_RE)
            summary = self.text(
                body.get("summary"),
                f"{path}.summary",
                limit=MAX_PRECONDITION_SUMMARY,
                one_line=True,
            )
            require = body.get("require")
            conds = None
            if require is not None:
                conds, found = parse_require(require, f"{path}.require", self.name, require.line)
                self.problems.extend(found)
            if ident and on and summary and conds is not None and require is not None:
                result.append(PreconditionDef(ident, on, summary, node_json(require), conds))
        return tuple(result)

    # Ops

    def ops(self, node: YamlNode) -> tuple[JsonObject, ...] | None:
        items = self.sequence(node, "ops")
        if items is None:
            return None
        result: list[JsonObject] = []
        for index, item in enumerate(items):
            op = self.op(item, index)
            if op is not None:
                result.append(op)
        return tuple(result)

    def op(self, node: YamlNode, index: int) -> JsonObject | None:
        path = f"ops[{index}]"
        if not isinstance(node, YamlMap):
            self.invalid(path, "expected a mapping", node)
            return None
        value = node_json(node)
        assert isinstance(value, dict)
        kind = value.get("op")
        if not isinstance(kind, str) or kind not in OP_KEYS:
            self.invalid(
                f"{path}.op",
                f"expected one of {', '.join(sorted(OP_KEYS))}",
                node.items.get("op", node),
            )
            return None
        before = len(self.problems)
        self.op_keys(node, value, kind, path)
        if len(self.problems) > before:
            return None
        scan = scan_op(value, index)
        for where, message in scan.shape:
            self.invalid(path_text(where), message, node)
        for where in scan.misplaced:
            self.add(
                "action.placeholder_position",
                path_text(where),
                f"{_at(value, where)!r} stands where no parameter may: a parameter replaces a "
                "whole reference or value, never a type, relation, attribute name, state or key",
                line=node.line,
            )
        for where, text in scan.literals:
            self.add(
                "action.literal_reference",
                path_text(where),
                f"{text!r} is a literal entity reference; ids go stale. Use a $parameter of "
                "type ref or an @local created in this action",
                line=node.line,
            )
        if len(self.problems) > before:
            return None
        return value

    def op_keys(self, node: YamlMap, value: JsonObject, kind: str, path: str) -> None:
        dynamic = is_dynamic_unlink(value) or (kind == "unlink" and "direction" in value)
        if dynamic:
            allowed: Sequence[str] = _DYNAMIC_KEYS
            required: Sequence[str] = ("op", "id", "rel", "all")
        else:
            needed, optional = OP_KEYS[kind]
            allowed = (*needed, *optional)
            required = needed
        for key in value:
            if key in _STATE_KEYS or key in _DOCUMENT_KEYS:
                why = (
                    "depends on the state when the action runs"
                    if key in _STATE_KEYS
                    else "belongs to the document around the ops, not to one op"
                )
                self.add(
                    "action.invalid_op",
                    f"{path}.{key}",
                    f"{key!r} cannot be part of an action template: it {why}",
                    line=node.key_lines[key],
                )
            elif key not in allowed:
                self.add(
                    "action.unknown_key",
                    f"{path}.{key}",
                    f"unknown key {key!r}",
                    line=node.key_lines[key],
                    expected=list(allowed),
                )
        for key in required:
            if key not in value:
                self.add(
                    "action.missing_key", f"{path}.{key}", f"missing key {key!r}", line=node.line
                )
        if dynamic:
            if "all" in value and value["all"] is not True:
                self.invalid(f"{path}.all", "expected true", node.items.get("all", node))
            if "direction" in value and value["direction"] not in DIRECTIONS:
                self.invalid(
                    f"{path}.direction",
                    f"{value['direction']!r} is not one of {', '.join(DIRECTIONS)}",
                    node.items["direction"],
                )
        elif kind == "unlink" and not ("rel" in value and "target" in value):
            self.add(
                "action.invalid_op",
                path,
                "an unlink needs rel and target, or the form id, rel, direction, all: true",
                line=node.line,
            )

    def examples(self, node: YamlNode) -> tuple[ExampleDef, ...] | None:
        items = self.sequence(node, "examples")
        if items is None:
            return None
        result: list[ExampleDef] = []
        for index, item in enumerate(items):
            path = f"examples[{index}]"
            body = self.mapping(item, path, ("request",))
            if body is None:
                continue
            self.require(body, "request", path, item.line)
            request = self.text(body.get("request"), f"{path}.request", limit=MAX_DETAIL)
            if request is not None:
                result.append(ExampleDef(request))
        return tuple(result)


def _at(value: JsonValue, path: Path) -> JsonValue:
    """The value (or key) at the end of ``path`` below the op (``path`` starts ``ops, i``)."""
    inner = path[2:]
    try:
        return walk_path(value, inner)
    except (KeyError, IndexError):
        return inner[-1] if inner else value
