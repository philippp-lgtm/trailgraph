"""Type descriptors of the parameters of a function, an action or a view, for tool schemas.

A tool manifest (``services.describe_tools``) tells an agent harness what each parameter takes. The
facts it needs live next to the binder in ``kinetic.params``: the JSON type of one value, the
grammar of the text of a date, a decimal or a reference, the range of an integer, the default as
JSON, whether the parameter is required and whether it takes a list. This module names them as
data (``ParamType``); it builds no JSON Schema, which belongs to the layers above (the output
schemas of the commands live there).

A text that matches ``pattern`` always binds (``bind_json``): the patterns are the ones of
``kernel.clock`` and ``kernel.ids`` for dates, timestamps and references and the decimal grammar
of the ontology layer, each stricter than the parser it describes (a test sweeps them against the
real parsers). Enum values are not part of a descriptor: they come from the model the caller
sees, which is the caller's to filter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.clock import DATE_PATTERN, TIMESTAMP_PATTERN
from graph_cli.kernel.ids import ENTITY_REF_PATTERN
from graph_cli.kinetic.params import ParamDef
from graph_cli.ontology_layer.values import DECIMAL_RE

# The JSON type of one value of a parameter type; every type not listed is a string.
_JSON_TYPES: Final = {"integer": "integer", "boolean": "boolean"}
# The grammar of the text of the types that read one (a decimal and an integer travel as they are
# in JSON: a decimal as text so that no precision is lost, an integer as a number).
_PATTERNS: Final = {
    "decimal": DECIMAL_RE.pattern,
    "date": DATE_PATTERN,
    "datetime": TIMESTAMP_PATTERN,
    "ref": ENTITY_REF_PATTERN,
}


@dataclass(frozen=True, slots=True)
class ParamType:
    """One parameter as a harness sees it: ``kind`` is the declared type (``params.PARAM_TYPES``),
    ``json_type`` the JSON type of one value, ``many`` a list of at least one. ``default`` is
    the JSON value a missing argument takes (``None``: none); ``required`` a parameter without
    default that is not optional. ``pattern`` is the grammar of a string value, ``minimum`` and
    ``maximum`` the range of an integer; ``of_class`` the class of a ``ref`` and ``enum`` the
    enum of an ``enum``."""

    name: str
    kind: str
    summary: str
    many: bool = False
    required: bool = True
    default: JsonValue = None
    of_class: str | None = None
    enum: str | None = None
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None

    @property
    def json_type(self) -> str:
        return _JSON_TYPES.get(self.kind, "string")


def _default_value(kind: str, value: JsonValue) -> JsonValue:
    """The default as the JSON of the input: a decimal is text there, whatever the file wrote
    (``5`` and ``"5"`` bind to the same decimal)."""
    if isinstance(value, list):
        return [_default_value(kind, v) for v in value]
    if kind == "decimal" and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


def param_type(param: ParamDef) -> ParamType:
    """The descriptor of one declared parameter."""
    integer = param.type == "integer"
    return ParamType(
        name=param.name,
        kind=param.type,
        summary=param.summary,
        many=param.many,
        required=param.required,
        default=_default_value(param.type, param.default),
        of_class=param.of_class,
        enum=param.enum,
        pattern=_PATTERNS.get(param.type),
        minimum=-MAX_SAFE_INT if integer else None,
        maximum=MAX_SAFE_INT if integer else None,
    )


def param_types(params: Sequence[ParamDef]) -> tuple[ParamType, ...]:
    """The descriptors of the parameters of an entry, in declaration order."""
    return tuple(param_type(p) for p in params)


def ref_type(name: str, summary: str, of_class: str) -> ParamType:
    """The descriptor of a required reference that is no declared parameter (the entity an
    entity view takes)."""
    return ParamType(name, "ref", summary, of_class=of_class, pattern=_PATTERNS["ref"])
