"""The registry and the modules agree: every extractor names classes that exist (M6, M10)."""

from __future__ import annotations

import pytest

from graph_cli.extractors.model import Extractor
from graph_cli.extractors.registry import extractors
from graph_cli.ontology_layer.bundle import LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES


def _model(module_id: str) -> CompiledModel:
    model = compile_bundle(module_id, LayeredSource()).model
    assert model is not None
    return model


def _version(model: CompiledModel, module_id: str) -> tuple[int, ...]:
    version = next(m.version for m in model.modules if m.id == module_id)
    return tuple(int(part) for part in version.split("."))


def test_format_comes_first_and_every_name_is_unique() -> None:
    names = list(extractors())
    assert names[0] == "format"
    assert len(names) == len(set(names))


@pytest.mark.parametrize("extractor", list(extractors().values()), ids=lambda e: e.name)
def test_an_extractor_matches_its_module(extractor: Extractor) -> None:
    assert extractor.version >= 1
    assert extractor.module.id in BUILTIN_MODULES
    model = _model(extractor.module.id)
    minimum = tuple(int(part) for part in extractor.module.min_version.split("."))
    assert _version(model, extractor.module.id) >= minimum
    assert model.is_a(extractor.record_class, "files:Metadata")
    assert (extractor.part_class is None) == (extractor.part_relation is None)
    if extractor.part_class is not None and extractor.part_relation is not None:
        assert model.is_a(extractor.part_class, "files:MetadataPart")
        relation = model.relations[extractor.part_relation]
        assert relation.domain == (extractor.record_class,)
        assert relation.range == (extractor.part_class,)
