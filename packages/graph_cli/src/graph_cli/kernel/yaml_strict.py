"""Strict YAML loading for authored files (contracts C.7): ontologies and policies.

The kernel's only YAML import; the ontology layer and access control use it.

Builds a small node tree from the PyYAML event stream instead of letting
PyYAML construct Python objects:

- duplicate keys, anchors, aliases, explicit tags, complex keys and
  multi-document streams are errors
- an entry without a value in a flow mapping is an error: a comma ends a
  plain value there, so ``{summary: Used, still works.}`` would silently
  become a second key ``still works.``
- there is no implicit typing: every scalar keeps its source text and
  whether it was plain or quoted; the consumer converts it according to its
  schema (YAML 1.2 core rules for booleans, integers and null)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import yaml

_BOOL: Final = {
    **dict.fromkeys(("true", "True", "TRUE"), True),
    **dict.fromkeys(("false", "False", "FALSE"), False),
}
_NULL: Final = frozenset({"", "~", "null", "Null", "NULL"})
_INT_RE: Final = re.compile(r"^[-+]?[0-9]+$")


class YamlError(ValueError):
    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        where = "" if line is None else f"line {line}"
        if where and column is not None:
            where += f", column {column}"
        super().__init__(f"{where}: {message}" if where else message)
        self.line = line
        self.column = column


@dataclass(frozen=True, slots=True)
class YamlScalar:
    text: str
    plain: bool  # False for quoted and block scalars
    line: int

    def is_null(self) -> bool:
        return self.plain and self.text in _NULL

    def as_bool(self) -> bool | None:
        return _BOOL.get(self.text) if self.plain else None

    def as_int(self) -> int | None:
        return int(self.text) if self.plain and _INT_RE.match(self.text) else None


@dataclass(frozen=True, slots=True)
class YamlMap:
    items: dict[str, YamlNode]
    key_lines: dict[str, int]
    line: int


@dataclass(frozen=True, slots=True)
class YamlSeq:
    items: tuple[YamlNode, ...]
    line: int


YamlNode = YamlScalar | YamlMap | YamlSeq


def _line(event: yaml.Event) -> int:
    mark = event.start_mark
    return mark.line + 1 if mark is not None else 0


def _column(event: yaml.Event) -> int | None:
    mark = event.start_mark
    return mark.column + 1 if mark is not None else None


def _missing_flow_value(value: YamlNode) -> bool:
    """A flow mapping entry written without ``:`` and value (or with nothing after ``:``)."""
    return isinstance(value, YamlScalar) and value.plain and value.text == ""


class _Builder:
    def __init__(self, events: list[yaml.Event]) -> None:
        self._events = events
        self._pos = 0

    def _next(self) -> yaml.Event:
        event = self._events[self._pos]
        self._pos += 1
        return event

    def _check(self, event: yaml.Event) -> None:
        if isinstance(event, yaml.AliasEvent):
            raise YamlError("aliases are not allowed", _line(event))
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise YamlError("anchors are not allowed", _line(event))
        if isinstance(event, yaml.ScalarEvent | yaml.CollectionStartEvent) and event.tag:
            raise YamlError(f"explicit tags are not allowed: {event.tag}", _line(event))

    def node(self) -> YamlNode:
        event = self._next()
        self._check(event)
        if isinstance(event, yaml.ScalarEvent):
            return YamlScalar(event.value, event.style is None, _line(event))
        if isinstance(event, yaml.SequenceStartEvent):
            elements: list[YamlNode] = []
            while not isinstance(self._events[self._pos], yaml.SequenceEndEvent):
                elements.append(self.node())
            self._next()
            return YamlSeq(tuple(elements), _line(event))
        if isinstance(event, yaml.MappingStartEvent):
            items: dict[str, YamlNode] = {}
            key_lines: dict[str, int] = {}
            while not isinstance(self._events[self._pos], yaml.MappingEndEvent):
                key_event = self._events[self._pos]
                key = self.node()
                if not isinstance(key, YamlScalar):
                    raise YamlError("mapping keys must be scalars", _line(event))
                if key.text in items:
                    raise YamlError(f"duplicate key {key.text!r}", key.line)
                key_lines[key.text] = key.line
                value = self.node()
                if event.flow_style and _missing_flow_value(value):
                    raise YamlError(
                        f"flow mapping entry {key.text!r} has no value; a comma ends a"
                        " plain value inside {...}: quote the value or use block style",
                        key.line,
                        _column(key_event),
                    )
                items[key.text] = value
            self._next()
            return YamlMap(items, key_lines, _line(event))
        raise YamlError(f"unexpected YAML event {type(event).__name__}", _line(event))

    def document(self) -> YamlNode | None:
        assert isinstance(self._next(), yaml.StreamStartEvent)
        if isinstance(self._events[self._pos], yaml.StreamEndEvent):
            return None
        self._next()  # DocumentStartEvent
        root = self.node()
        self._next()  # DocumentEndEvent
        if not isinstance(self._events[self._pos], yaml.StreamEndEvent):
            raise YamlError("only one YAML document is allowed", _line(self._events[self._pos]))
        return root


def load_strict(text: str | bytes) -> YamlNode | None:
    """Parse one YAML document strictly. ``None`` for an empty stream."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YamlError("file is not valid UTF-8") from exc
    try:
        events = list(yaml.parse(text, Loader=yaml.SafeLoader))
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(exc.problem or str(exc), line) from exc
    except yaml.YAMLError as exc:
        raise YamlError(str(exc)) from exc
    return _Builder(events).document()
