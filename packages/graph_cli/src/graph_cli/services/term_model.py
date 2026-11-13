"""A compiled model condensed to what term rules need (contracts B6.13, W8).

``access_control`` imports only the kernel and never sees a compiled ontology, so the
app side condenses a graph's pinned model into a ``TermModel`` (per type: its attributes,
the relations whose domain covers it, whether it is extended or a stored file). The policy
compiler checks ``writes`` grants and term deny rules against it, and an ontology release
checks the installed ones against the new model. Pure and total.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.access_control.terms import AttributeTerms, TermModel, TypeTerms
from graph_cli.ontology_layer.model import ClassDef, CompiledModel
from graph_cli.ontology_layer.vocabulary import FILE_TYPE

# What ``graph file add`` sets itself (services/files.py): a create list must name these.
FILE_ADD_ATTRIBUTES: Final = ("title", "content", "media_type", "file_name")


def extended_types(classes: Mapping[str, ClassDef]) -> frozenset[str]:
    """The types a rule on an exact type would miss entities of: abstract ones and those
    with subclasses."""
    return frozenset(
        name
        for name, cls in classes.items()
        if cls.abstract or any(name in other.ancestors for other in classes.values())
    )


def concrete_types(classes: Mapping[str, ClassDef]) -> frozenset[str]:
    """The types an entity can have (every class that is not abstract), whether or not it has
    subclasses: what ``access_control.effective.visibility`` probes."""
    return frozenset(name for name, cls in classes.items() if not cls.abstract)


def term_model(model: CompiledModel) -> TermModel:
    """What the policy compiler needs of ``model``: per type its attributes (is it a
    lifecycle; must a create give it), the relations whose domain covers it, and whether
    it is extended or a stored file. A stored file's create gives what ``graph file add``
    sets itself, so those attributes count as given by the writer."""
    extended = extended_types(model.classes)
    types: dict[str, TypeTerms] = {}
    for name, cls in model.classes.items():
        file = FILE_TYPE in model.classes and model.is_a(name, FILE_TYPE)
        attributes: dict[str, AttributeTerms] = {}
        for attribute, definition in cls.attributes.items():
            lifecycle = definition.kind == "lifecycle"
            if lifecycle:
                must_give = len(model.lifecycles[definition.type].initial) > 1
            else:
                must_give = definition.required and definition.default is None
            attributes[attribute] = AttributeTerms(
                lifecycle, must_give or (file and attribute in FILE_ADD_ATTRIBUTES)
            )
        types[name] = TypeTerms(
            name in extended,
            file,
            attributes,
            frozenset(r for r, rel in model.relations.items() if model.is_a_any(name, rel.domain)),
        )
    return TermModel(types)
