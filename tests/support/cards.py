"""Helpers for the entity card tests: a model with more attributes, spec edits, a loads counter."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kinetic.card import (
    AffordancesSpec,
    FieldsSpec,
    FilesSpec,
    RelationsSpec,
    ViewSpec,
)
from graph_cli.ontology_layer.model import CompiledModel
from support.snapshots import DictSnapshot


def with_attribute(
    model: CompiledModel, cls: str, name: str, like: str, **overrides: Any
) -> CompiledModel:
    """``model`` with one more attribute on ``cls``, a copy of ``like`` with ``overrides``."""
    definition = model.classes[cls]
    added = replace(definition.attributes[like], **overrides)
    classes = {
        **model.classes,
        cls: replace(definition, attributes={**definition.attributes, name: added}),
    }
    return replace(model, classes=classes)


def with_sections(spec: ViewSpec, **changes: Any) -> ViewSpec:
    """The spec with the section of each given kind replaced through ``replace(**changes)``."""
    kinds = {
        "fields": FieldsSpec,
        "relations": RelationsSpec,
        "files": FilesSpec,
        "affordances": AffordancesSpec,
    }
    sections = []
    for s in spec.sections:
        for kind, cls in kinds.items():
            if isinstance(s, cls) and kind in changes:
                s = replace(s, **changes[kind])
        sections.append(s)
    return replace(spec, sections=tuple(sections))


class CountingSnapshot:
    """A ``Snapshot`` that counts the revisions the card loads (``get`` and ``get_many``)."""

    def __init__(self, inner: DictSnapshot) -> None:
        self.inner = inner
        self.loaded: list[EntityId] = []

    @property
    def seq(self) -> int:
        return self.inner.seq

    @property
    def warnings(self) -> tuple[Any, ...]:
        return self.inner.warnings

    def get(self, entity: EntityId) -> Revision | None:
        self.loaded.append(entity)
        return self.inner.get(entity)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        self.loaded.extend(entities)
        return self.inner.get_many(entities)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self.inner.incoming(entity, rel)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def as_snapshot(counting: CountingSnapshot) -> Snapshot:
    """The double as the port type (the double implements what the card uses)."""
    return counting
