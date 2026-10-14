"""Schema-directed conversion of strict YAML nodes into typed values.

Every problem becomes a finding instead of an exception, so one run reports
all structural defects of a file. Readers return ``None`` for a value they
could not accept.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.ontology_layer.report import Report
from graph_cli.ontology_layer.yaml_strict import YamlMap, YamlNode, YamlScalar, YamlSeq


class Reader:
    def __init__(self, report: Report, module: str) -> None:
        self.report = report
        self.module = module

    def invalid(self, path: str, message: str) -> None:
        self.report.add("model.structural.invalid_value", self.module, path, message)

    def mapping(
        self, node: YamlNode | None, path: str, allowed: Iterable[str]
    ) -> dict[str, YamlNode] | None:
        """A mapping with known keys only; unknown keys are reported and dropped."""
        if node is None or (isinstance(node, YamlScalar) and node.is_null()):
            return {}
        if not isinstance(node, YamlMap):
            self.invalid(path, "expected a mapping")
            return None
        allowed_keys = frozenset(allowed)
        result: dict[str, YamlNode] = {}
        for key, value in node.items.items():
            if key in allowed_keys:
                result[key] = value
            else:
                self.report.add(
                    "model.structural.unknown_key",
                    self.module,
                    _join(path, key),
                    f"unknown key {key!r}",
                    expected=sorted(allowed_keys),
                    actual=key,
                )
        return result

    def entries(self, node: YamlNode | None, path: str) -> dict[str, YamlNode] | None:
        """A mapping of user-chosen names (terms, values, states)."""
        if node is None or (isinstance(node, YamlScalar) and node.is_null()):
            return {}
        if not isinstance(node, YamlMap):
            self.invalid(path, "expected a mapping")
            return None
        return dict(node.items)

    def sequence(self, node: YamlNode | None, path: str) -> tuple[YamlNode, ...] | None:
        if node is None:
            return ()
        if not isinstance(node, YamlSeq):
            self.invalid(path, "expected a list")
            return None
        return node.items

    def string(self, node: YamlNode | None, path: str) -> str | None:
        if node is None:
            return None
        if not isinstance(node, YamlScalar) or node.is_null():
            self.invalid(path, "expected a string")
            return None
        return node.text

    def name(self, node: YamlNode | None, path: str, grammar: re.Pattern[str]) -> str | None:
        text = self.string(node, path)
        if text is not None and not grammar.match(text):
            self.invalid(path, f"{text!r} does not match {grammar.pattern}")
            return None
        return text

    def check_name(self, text: str, path: str, grammar: re.Pattern[str]) -> bool:
        if grammar.match(text):
            return True
        self.invalid(path, f"{text!r} does not match {grammar.pattern}")
        return False

    def choice(self, node: YamlNode | None, path: str, allowed: Iterable[str]) -> str | None:
        text = self.string(node, path)
        options = tuple(allowed)
        if text is not None and text not in options:
            self.invalid(path, f"{text!r} is not one of {', '.join(options)}")
            return None
        return text

    def boolean(self, node: YamlNode | None, path: str, default: bool = False) -> bool:
        if node is None:
            return default
        value = node.as_bool() if isinstance(node, YamlScalar) else None
        if value is None:
            self.invalid(path, "expected true or false")
            return default
        return value

    def integer(self, node: YamlNode | None, path: str, *, minimum: int = 0) -> int | None:
        if node is None:
            return None
        value = node.as_int() if isinstance(node, YamlScalar) else None
        if value is None or value < minimum:
            self.invalid(path, f"expected an integer >= {minimum}")
            return None
        return value

    def names(
        self, node: YamlNode | None, path: str, grammar: re.Pattern[str] | None = None
    ) -> tuple[str, ...] | None:
        """A single string or a list of strings."""
        if node is None:
            return ()
        items = node.items if isinstance(node, YamlSeq) else (node,)
        result = []
        for index, item in enumerate(items):
            item_path = f"{path}[{index}]" if isinstance(node, YamlSeq) else path
            text = self.name(item, item_path, grammar) if grammar else self.string(item, item_path)
            if text is None:
                return None
            result.append(text)
        return tuple(result)

    def free_json(self, node: YamlNode | None, path: str) -> JsonValue:
        """Uninterpreted payload (annotations, extensions) with YAML 1.2 core typing."""
        if node is None:
            return None
        if isinstance(node, YamlMap):
            return {
                key: self.free_json(value, _join(path, key)) for key, value in node.items.items()
            }
        if isinstance(node, YamlSeq):
            return [self.free_json(item, f"{path}[{i}]") for i, item in enumerate(node.items)]
        if node.is_null():
            return None
        boolean = node.as_bool()
        if boolean is not None:
            return boolean
        number = node.as_int()
        if number is not None and abs(number) <= MAX_SAFE_INT:
            return number
        return node.text


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


join = _join
