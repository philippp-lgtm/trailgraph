"""Text and value helpers the card and the row builder of views share (pure).

A label is the main label in the graph's language, a state the value of the one lifecycle
attribute of a class, a value as a view shows it is cut to a length and a list to a count (the
length and the count are reported, nothing is dropped silently).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import Revision
from graph_cli.kernel.text import normalize_label
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.lookup import label_text


def cut(text: str, limit: int) -> tuple[str, int | None]:
    """``text`` cut to ``limit`` characters with an ellipsis, and its length when it was cut."""
    if len(text) <= limit:
        return text, None
    return text[:limit] + "…", len(text)


def has_value(value: JsonValue) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def field_value(
    value: JsonValue, value_chars: int, list_items: int
) -> tuple[JsonValue, int | None, int]:
    """The value as shown, the length of a string that was cut, the elements of a list cut."""
    if isinstance(value, str):
        text, chars = cut(value, value_chars)
        return text, chars, 0
    if isinstance(value, Mapping):
        text_raw = value.get("value")
        if isinstance(text_raw, str) and value.keys() == {"value", "lang"}:  # language-tagged
            text, chars = cut(text_raw, value_chars)
            return {"value": text, "lang": value["lang"]}, chars, 0
        return value, None, 0
    if isinstance(value, Sequence):
        shown: list[JsonValue] = []
        for element in value[:list_items]:
            shown.append(field_value(element, value_chars, list_items)[0])
        return shown, None, max(0, len(value) - list_items)
    return value, None, 0


def main_label(model: CompiledModel, revision: Revision, language: str | None) -> str | None:
    if str(revision.type) not in model.classes:
        return None
    for attr, role in model.label_attributes(str(revision.type)):
        if role == "label":
            return label_text(revision.attrs.get(attr), (language,))
    return None


def label_key(label: str | None, entity: EntityId) -> tuple[bool, str, str]:
    """The order of neighbours: named ones first by normalized label, then by id."""
    return (label is None, "" if label is None else normalize_label(label), str(entity))


def lifecycle_attribute(model: CompiledModel, revision: Revision) -> str | None:
    return model.lifecycle_attribute(str(revision.type))


def state_of(model: CompiledModel, revision: Revision) -> str | None:
    attr = lifecycle_attribute(model, revision)
    value = None if attr is None else revision.attrs.get(attr)
    return value if isinstance(value, str) else None
