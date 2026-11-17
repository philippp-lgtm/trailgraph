"""How a value given in a change set becomes the value stored (contracts B3.16, Q18).

``normalize`` applies these when it writes; the reconcile planner applies them to the value
an item states before it compares it with what is stored, so an item that restates the stored
value is judged unchanged by the very rules that would store it again.

- A text for an attribute is coerced per the base type of the attribute (``values.coerce_text``);
  a text that does not coerce, or an unknown attribute, stays the given string for the validator
  to judge; a ``many`` attribute becomes a one-element list.
- Language-tagged text without a tag takes the graph language, and a tag gets canonical case
  (``values.with_language``).
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel, QualifierDef
from graph_cli.ontology_layer.values import coerce_text, with_language
from graph_cli.ontology_layer.vocabulary import LANG_BASES


def is_tagged(model: CompiledModel, field: AttributeDef | QualifierDef | None) -> bool:
    """Whether ``field`` holds language-tagged text."""
    return field is not None and model.value_base(field.kind, field.type) in LANG_BASES


def language_tagged(
    model: CompiledModel,
    field: AttributeDef | QualifierDef | None,
    value: JsonValue,
    language: str | None,
) -> JsonValue:
    """A value of ``field`` with the graph ``language`` on untagged language-tagged text."""
    if not is_tagged(model, field):
        return value
    if isinstance(value, list | tuple):
        return [with_language(item, language) for item in value]
    return with_language(value, language)


def coerce_attribute_item(
    model: CompiledModel, attribute: AttributeDef | None, text: str
) -> JsonValue:
    """One value of the attribute from text; the text itself if it does not coerce."""
    if attribute is None:
        return text
    try:
        return coerce_text(model.value_base(attribute.kind, attribute.type), text)
    except ValueError:
        return text


def coerce_attribute(model: CompiledModel, attribute: AttributeDef | None, text: str) -> JsonValue:
    """The value of the attribute from text, a one-element list for a ``many`` attribute."""
    value = coerce_attribute_item(model, attribute, text)
    return [value] if attribute is not None and attribute.many else value
