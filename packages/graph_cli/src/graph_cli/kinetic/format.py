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
from collections.abc import Mapping
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
from graph_cli.kernel.yaml_strict import YamlError, YamlMap, YamlNode, load_strict
from graph_cli.kinetic.params import (
    MAX_DETAIL,
    PARAM_RE,
    PARAM_TYPES,
    ParamDef,
    Parser,
    Problem,
    is_null,
)

# The parameter grammar and ``Problem`` live in ``params`` (shared with actions); importers
# of this module keep using these names.
__all__ = ["MAX_DETAIL", "PARAM_RE", "PARAM_TYPES", "ParamDef", "Problem"]

FUNCTION_SCHEMA: Final = "graph_cli.function/1"
CATALOG_SCHEMA: Final = "graph_cli.functions/1"
FILE_SUFFIX: Final = ".yaml"

NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
MODULE_REF_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

MAX_SUMMARY: Final = 100
MAX_USE_WHEN: Final = 600

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


class _Parse(Parser):
    """Collects the problems of one function file."""

    def __init__(self, function: str) -> None:
        super().__init__("function", function)

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
        name = self.name_of(top.get("name"), "name", NAME_RE) if "name" in top else None
        if name is not None and file_name != name + FILE_SUFFIX:
            self.add(
                "function.name_mismatch",
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
        query = self.text(top.get("query"), "query")
        returns = self.returns(top["returns"]) if "returns" in top else None
        examples = self.examples(top["examples"]) if "examples" in top else None
        featured = self.boolean(top["featured"], "featured") if "featured" in top else False
        status = self.choice(top["status"], "status", STATUSES) if "status" in top else "active"
        replaced_by = None
        if "replaced_by" in top and not is_null(top["replaced_by"]):
            replaced_by = self.name_of(top["replaced_by"], "replaced_by", NAME_RE)
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
        return parse_examples(self, node)


def parse_examples(parser: Parser, node: YamlNode) -> tuple[ExampleDef, ...] | None:
    """The ``examples`` list of a function or view file: a question, optional parameter values."""
    items = parser.sequence(node, "examples")
    if items is None:
        return None
    result: list[ExampleDef] = []
    for index, item in enumerate(items):
        path = f"examples[{index}]"
        body = parser.mapping(item, path, ("question", "params"))
        if body is None:
            continue
        parser.require(body, "question", path, item.line)
        question = parser.text(body.get("question"), f"{path}.question", limit=MAX_DETAIL)
        values: dict[str, JsonValue] = {}
        given = body.get("params")
        if isinstance(given, YamlMap):
            values = {k: parser.literal(v, f"{path}.params.{k}") for k, v in given.items.items()}
        elif given is not None:
            parser.invalid(f"{path}.params", "expected a mapping", given)
        if question is not None:
            result.append(ExampleDef(question, values))
    return tuple(result)
