"""Authored format ``graph_cli.function/1``: typed function model and its parser.

Parsing checks the shape of one file (keys, value types, grammars, lengths);
everything that needs the ontology is checked by ``compile``. Every defect of a
file becomes a ``Problem`` instead of an exception, so one run reports all of
them. A definition also reads back from a compiled catalog entry
(``FunctionDef.from_json``), which is how a published catalog is checked again
against another model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import EMPTY
from graph_cli.kernel.json_access import (
    as_object,
    opt_str,
    req_bool,
    req_list,
    req_obj,
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

FUNCTION_SCHEMA: Final = "graph_cli.function/1"
CATALOG_SCHEMA: Final = "graph_cli.functions/1"
FILE_SUFFIX: Final = ".yaml"

NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
PARAM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MODULE_REF_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

MAX_SUMMARY: Final = 100
MAX_USE_WHEN: Final = 600
MAX_DETAIL: Final = 200  # parameter and column summaries, example questions

PARAM_TYPES: Final = (
    "string",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "ref",
)
STATUSES: Final = ("active", "deprecated")

_TOP_KEYS: Final = (
    "schema",
    "name",
    "module",
    "summary",
    "use_when",
    "params",
    "query",
    "returns",
    "examples",
    "featured",
    "status",
    "replaced_by",
)
_PARAM_KEYS: Final = ("type", "class", "enum", "many", "default", "summary")


@dataclass(frozen=True, slots=True)
class Problem:
    """One broken rule of one function. ``code`` is a ``function.*`` code or the code of
    the query error the engine raised (``query.syntax``, ``term.unknown``, ...)."""

    function: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    details: Mapping[str, JsonValue] = field(default=EMPTY)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "function": self.function,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ParamDef:
    name: str
    type: str  # one of PARAM_TYPES
    summary: str
    of_class: str | None = None  # ``class:``, only with type ref
    enum: str | None = None  # only with type enum
    many: bool = False
    default: JsonValue = None  # None = no default, the parameter is required

    @property
    def required(self) -> bool:
        return self.default is None

    @property
    def type_text(self) -> str:
        """The type as people read it: ``string``, ``ref org:OrgUnit``, ``list of date``."""
        kind = {"ref": f"ref {self.of_class}", "enum": f"enum {self.enum}"}.get(
            self.type, self.type
        )
        return f"list of {kind}" if self.many else kind

    @property
    def signature(self) -> str:
        """``name: type``; ``name?: type`` for an optional parameter."""
        return f"{self.name}{'' if self.required else '?'}: {self.type_text}"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "type": self.type,
            "class": self.of_class,
            "enum": self.enum,
            "many": self.many,
            "default": self.default,
            "summary": self.summary,
        }

    @classmethod
    def from_json(cls, data: JsonObject, path: str) -> ParamDef:
        return cls(
            name=req_str(data, "name", path),
            type=req_str(data, "type", path),
            summary=req_str(data, "summary", path),
            of_class=opt_str(data, "class", path),
            enum=opt_str(data, "enum", path),
            many=req_bool(data, "many", path),
            default=data.get("default"),
        )


@dataclass(frozen=True, slots=True)
class ReturnDef:
    column: str
    summary: str


@dataclass(frozen=True, slots=True)
class ExampleDef:
    question: str
    params: Mapping[str, JsonValue] = field(default=EMPTY)


@dataclass(frozen=True, slots=True)
class FunctionDef:
    name: str
    module: str | None  # module id or prefix as authored
    summary: str
    use_when: str
    params: tuple[ParamDef, ...]
    query: str
    returns: tuple[ReturnDef, ...]
    examples: tuple[ExampleDef, ...]
    featured: bool = False
    status: str = "active"  # one of STATUSES
    replaced_by: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """The declaration without the query text (the catalog entry adds both forms)."""
        return {
            "name": self.name,
            "module": self.module,
            "summary": self.summary,
            "use_when": self.use_when,
            "status": self.status,
            "replaced_by": self.replaced_by,
            "featured": self.featured,
            "params": [p.to_json() for p in self.params],
            "returns": [{"column": r.column, "summary": r.summary} for r in self.returns],
            "examples": [{"question": e.question, "params": dict(e.params)} for e in self.examples],
        }

    @classmethod
    def from_json(cls, data: JsonObject, query: str) -> FunctionDef:
        """Read back ``to_json``; ``JsonShapeError`` for a document of another shape."""
        path = f"{data.get('name')}."
        return cls(
            name=req_str(data, "name"),
            module=opt_str(data, "module", path),
            summary=req_str(data, "summary", path),
            use_when=req_str(data, "use_when", path),
            params=tuple(
                ParamDef.from_json(as_object(p, f"{path}params"), f"{path}params.")
                for p in req_list(data, "params", path)
            ),
            query=query,
            returns=tuple(
                ReturnDef(req_str(o, "column", path), req_str(o, "summary", path))
                for o in (as_object(r, f"{path}returns") for r in req_list(data, "returns", path))
            ),
            examples=tuple(
                ExampleDef(req_str(o, "question", path), req_obj(o, "params", path))
                for o in (as_object(e, f"{path}examples") for e in req_list(data, "examples", path))
            ),
            featured=req_bool(data, "featured", path),
            status=req_str(data, "status", path),
            replaced_by=opt_str(data, "replaced_by", path),
        )


def function_name(file_name: str) -> str:
    """The name a file stands for: its name without the ``.yaml`` suffix."""
    return file_name.removesuffix(FILE_SUFFIX)


def load_function(file_name: str, content: str | bytes) -> tuple[FunctionDef | None, list[Problem]]:
    """Parse one function file. The definition is ``None`` when any problem was found."""
    parse = _Parse(function_name(file_name))
    try:
        root = load_strict(content)
    except YamlError as exc:
        parse.add("function.invalid_yaml", None, str(exc), line=exc.line)
        return None, parse.problems
    if root is None:
        parse.add("function.invalid_yaml", None, "the file is empty")
        return None, parse.problems
    defn = parse.read(root, file_name)
    return (defn if not parse.problems else None), parse.problems


class _Parse:
    """Collects the problems of one file; the readers return ``None`` for a bad value."""

    def __init__(self, function: str) -> None:
        self.function = function
        self.problems: list[Problem] = []

    def add(
        self,
        code: str,
        path: str | None,
        message: str,
        *,
        line: int | None = None,
        **details: JsonValue,
    ) -> None:
        self.problems.append(Problem(self.function, code, message, path, line, details))

    def invalid(self, path: str, message: str, node: YamlNode | None = None) -> None:
        self.add("function.invalid_value", path, message, line=node.line if node else None)

    # Readers

    def mapping(
        self, node: YamlNode | None, path: str, allowed: Iterable[str]
    ) -> dict[str, YamlNode] | None:
        """A mapping with known keys only; unknown keys are reported and dropped."""
        if not isinstance(node, YamlMap):
            self.invalid(path or "(file)", "expected a mapping", node)
            return None
        keys = tuple(allowed)
        for key in node.items:
            if key not in keys:
                self.add(
                    "function.unknown_key",
                    _join(path, key),
                    f"unknown key {key!r}",
                    line=node.key_lines[key],
                    expected=list(keys),
                )
        return {k: v for k, v in node.items.items() if k in keys}

    def require(self, found: Mapping[str, YamlNode], key: str, path: str, line: int) -> bool:
        if key in found:
            return True
        self.add("function.missing_key", _join(path, key), f"missing key {key!r}", line=line)
        return False

    def text(
        self, node: YamlNode | None, path: str, *, limit: int | None = None, one_line: bool = False
    ) -> str | None:
        if node is None:  # absent: reported as a missing key
            return None
        if not isinstance(node, YamlScalar) or node.is_null():
            self.invalid(path, "expected a string", node)
            return None
        value = node.text.strip()
        if not value:
            self.invalid(path, "must not be empty", node)
            return None
        if limit is not None and len(value) > limit:
            self.invalid(path, f"is {len(value)} characters; at most {limit}", node)
            return None
        if one_line and "\n" in value:
            self.invalid(path, "must be one line", node)
            return None
        return value

    def name(self, node: YamlNode | None, path: str, grammar: re.Pattern[str]) -> str | None:
        value = self.text(node, path)
        if value is not None and not grammar.match(value):
            self.invalid(path, f"{value!r} does not match {grammar.pattern}", node)
            return None
        return value

    def boolean(self, node: YamlNode | None, path: str) -> bool | None:
        value = node.as_bool() if isinstance(node, YamlScalar) else None
        if value is None:
            self.invalid(path, "expected true or false", node)
        return value

    def choice(self, node: YamlNode | None, path: str, options: tuple[str, ...]) -> str | None:
        value = self.text(node, path)
        if value is not None and value not in options:
            self.invalid(path, f"{value!r} is not one of {', '.join(options)}", node)
            return None
        return value

    def sequence(self, node: YamlNode | None, path: str) -> tuple[YamlNode, ...] | None:
        if node is None:
            return None
        if not isinstance(node, YamlSeq):
            self.invalid(path, "expected a list", node)
            return None
        if not node.items:
            self.invalid(path, "must not be empty", node)
            return None
        return node.items

    def literal(self, node: YamlNode, path: str) -> JsonValue:
        """A default or example value: a scalar or a list of scalars (YAML 1.2 core typing)."""
        if isinstance(node, YamlSeq):
            items = [self.literal(item, f"{path}[{i}]") for i, item in enumerate(node.items)]
            return None if any(i is None for i in items) else items
        if isinstance(node, YamlMap) or _is_null(node):
            self.invalid(path, "expected a string, number, boolean or a list of them", node)
            return None
        boolean = node.as_bool()
        if boolean is not None:
            return boolean
        number = node.as_int()
        return node.text if number is None else number

    # Parts

    def read(self, root: YamlNode, file_name: str) -> FunctionDef | None:
        top = self.mapping(root, "", _TOP_KEYS)
        if top is None:
            return None
        line = root.line
        for key in ("schema", "name", "summary", "use_when", "query", "returns", "examples"):
            self.require(top, key, "", line)
        if "schema" in top:
            schema = self.text(top["schema"], "schema")
            if schema is not None and schema != FUNCTION_SCHEMA:
                self.add(
                    "function.unsupported_schema",
                    "schema",
                    f"{schema!r} is not {FUNCTION_SCHEMA}",
                    line=top["schema"].line,
                )
        name = self.name(top.get("name"), "name", NAME_RE) if "name" in top else None
        if name is not None and file_name != name + FILE_SUFFIX:
            self.add(
                "function.name_mismatch",
                "name",
                f"name {name!r} needs the file name {name + FILE_SUFFIX}, not {file_name}",
                line=top["name"].line,
            )
        module = None
        if "module" in top:
            module = self.name(top["module"], "module", MODULE_REF_RE)
        summary = self.text(top.get("summary"), "summary", limit=MAX_SUMMARY, one_line=True)
        use_when = self.text(top.get("use_when"), "use_when", limit=MAX_USE_WHEN)
        params = self.params(top["params"]) if "params" in top else ()
        query = self.text(top.get("query"), "query")
        returns = self.returns(top["returns"]) if "returns" in top else None
        examples = self.examples(top["examples"]) if "examples" in top else None
        featured = self.boolean(top["featured"], "featured") if "featured" in top else False
        status = self.choice(top["status"], "status", STATUSES) if "status" in top else "active"
        replaced_by = None
        if "replaced_by" in top and not _is_null(top["replaced_by"]):
            replaced_by = self.name(top["replaced_by"], "replaced_by", NAME_RE)
        if replaced_by is not None and status != "deprecated":
            self.invalid("replaced_by", "is only allowed with status: deprecated")
        if replaced_by is not None and replaced_by == name:
            self.invalid("replaced_by", "a function cannot replace itself")
        if self.problems:
            return None
        assert name and summary and use_when and query and returns and examples
        assert params is not None and featured is not None and status is not None
        return FunctionDef(
            name=name,
            module=module,
            summary=summary,
            use_when=use_when,
            params=params,
            query=query,
            returns=returns,
            examples=examples,
            featured=featured,
            status=status,
            replaced_by=replaced_by,
        )

    def params(self, node: YamlNode) -> tuple[ParamDef, ...] | None:
        if not isinstance(node, YamlMap):
            if _is_null(node):
                return ()
            self.invalid("params", "expected a mapping", node)
            return None
        result: list[ParamDef] = []
        for key, value in node.items.items():
            path = f"params.{key}"
            if not PARAM_RE.match(key):
                self.add(
                    "function.invalid_value",
                    path,
                    f"parameter name {key!r} does not match {PARAM_RE.pattern}",
                    line=node.key_lines[key],
                )
                continue
            found = self.param(key, value, path)
            if found is not None:
                result.append(found)
        return tuple(result)

    def param(self, name: str, node: YamlNode, path: str) -> ParamDef | None:
        body = self.mapping(node, path, _PARAM_KEYS)
        if body is None:
            return None
        self.require(body, "type", path, node.line)
        self.require(body, "summary", path, node.line)
        kind = (
            self.choice(body.get("type"), f"{path}.type", PARAM_TYPES) if "type" in body else None
        )
        summary = self.text(body.get("summary"), f"{path}.summary", limit=MAX_DETAIL)
        of_class = enum = None
        if "class" in body:
            of_class = self.text(body["class"], f"{path}.class")
        if "enum" in body:
            enum = self.text(body["enum"], f"{path}.enum")
        many = self.boolean(body["many"], f"{path}.many") if "many" in body else False
        default = None
        if "default" in body:
            default = self.literal(body["default"], f"{path}.default")
        if kind == "ref" and "class" not in body:
            self.add("function.missing_key", f"{path}.class", "a ref needs a class", line=node.line)
        if kind == "enum" and "enum" not in body:
            self.add("function.missing_key", f"{path}.enum", "an enum needs enum:", line=node.line)
        if kind is not None and kind != "ref" and "class" in body:
            self.invalid(f"{path}.class", "only a ref has a class", body["class"])
        if kind is not None and kind != "enum" and "enum" in body:
            self.invalid(f"{path}.enum", "only an enum has enum:", body["enum"])
        if kind is None or summary is None or many is None:
            return None
        return ParamDef(name, kind, summary, of_class, enum, many, default)

    def returns(self, node: YamlNode) -> tuple[ReturnDef, ...] | None:
        items = self.sequence(node, "returns")
        if items is None:
            return None
        result: list[ReturnDef] = []
        for index, item in enumerate(items):
            path = f"returns[{index}]"
            body = self.mapping(item, path, ("column", "summary"))
            if body is None:
                continue
            self.require(body, "column", path, item.line)
            self.require(body, "summary", path, item.line)
            column = self.text(body.get("column"), f"{path}.column", limit=MAX_DETAIL)
            summary = self.text(body.get("summary"), f"{path}.summary", limit=MAX_DETAIL)
            if column is not None and summary is not None:
                result.append(ReturnDef(column, summary))
        return tuple(result)

    def examples(self, node: YamlNode) -> tuple[ExampleDef, ...] | None:
        items = self.sequence(node, "examples")
        if items is None:
            return None
        result: list[ExampleDef] = []
        for index, item in enumerate(items):
            path = f"examples[{index}]"
            body = self.mapping(item, path, ("question", "params"))
            if body is None:
                continue
            self.require(body, "question", path, item.line)
            question = self.text(body.get("question"), f"{path}.question", limit=MAX_DETAIL)
            values: dict[str, JsonValue] = {}
            given = body.get("params")
            if isinstance(given, YamlMap):
                values = {k: self.literal(v, f"{path}.params.{k}") for k, v in given.items.items()}
            elif given is not None:
                self.invalid(f"{path}.params", "expected a mapping", given)
            if question is not None:
                result.append(ExampleDef(question, values))
        return tuple(result)


def _is_null(node: YamlNode) -> bool:
    return isinstance(node, YamlScalar) and node.is_null()


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key
