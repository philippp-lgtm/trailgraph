"""Level 0 budget of generated graph skills: ``SKILL.md`` of a modular graph stays under
``L0_BUDGET`` on a large synthetic ontology (50 modules, 1250 types), and its size does not
depend on how many types a module holds; the types are on level 1 (module pages,
``describe module``)."""

import json
from typing import Any

import pytest
from support.fixtures import FIXTURES
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.skills.graph_skill import L0_BUDGET, render_graph_skill
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import SKILL_FILE, read_tree

# The curated template of the modular fixture: start, modules, names, model.
TEMPLATE = FIXTURES / "skills" / "company" / GRAPH_SKILL_NAME
MAIN = "synthetic"
LINKS = 3  # each module links to up to this many earlier modules, one relation each
SUMMARY = "A subject area of the synthetic graph that holds a part of what the graph knows."


def _module_id(index: int) -> str:
    return f"area_{index:02d}"


def _module(index: int, types: int) -> dict[str, Any]:
    """Module ``index`` with ``types`` named types, a chain of relations inside it and one
    relation to each of the ``LINKS`` modules before it."""
    earlier = [_module_id(i) for i in range(max(0, index - LINKS), index)]
    prefix = f"a{index:02d}"
    classes = {
        f"Thing{t:03d}": {
            "summary": f"Synthetic entity type {t} of the module.",
            "display_prefix": f"A{index:02d}T{t:03d}",
            "attributes": {
                "name": {
                    "summary": "Main name.",
                    "type": "string",
                    "required": True,
                    "role": "label",
                },
                "alt_labels": {
                    "summary": "Other names.",
                    "type": "string",
                    "many": True,
                    "role": "alt_label",
                },
                "note": {"summary": "Free text.", "type": "string"},
            },
        }
        for t in range(types)
    }
    relations = {
        f"next_{t:03d}": {
            "summary": "The next thing in the module.",
            "domain": f"Thing{t:03d}",
            "range": f"Thing{t + 1:03d}",
        }
        for t in range(types - 1)
    }
    for other in earlier:
        other_prefix = "a" + other.removeprefix("area_")
        relations[f"refers_to_{other}"] = {
            "summary": "A thing of the module refers to a thing of an earlier module.",
            "domain": "Thing000",
            "range": f"{other_prefix}:Thing000",
        }
    return {
        "format": "graph_cli.ir/1",
        "module": {
            "id": _module_id(index),
            "prefix": prefix,
            "version": "1.0.0",
            "summary": SUMMARY,
            "imports": [{"module": other, "version": "^1.0"} for other in earlier],
        },
        "classes": classes,
        "relations": relations,
    }


def synthetic_model(modules: int, types: int) -> CompiledModel:
    """A main module importing ``modules`` subject areas of ``types`` types each."""
    files = {_module_id(i): json.dumps(_module(i, types)).encode() for i in range(modules)}
    files[MAIN] = json.dumps(
        {
            "format": "graph_cli.ir/1",
            "module": {
                "id": MAIN,
                "prefix": "syn",
                "version": "1.0.0",
                "summary": "The main module; it holds no terms of its own.",
                "imports": [{"module": _module_id(i), "version": "^1.0"} for i in range(modules)],
            },
        }
    ).encode()
    outcome = compile_bundle(MAIN, LayeredSource(MappingSource(files)))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def skill_md(model: CompiledModel) -> bytes:
    """``SKILL.md`` alone: the reference pages of a model this large go over the size limit of
    a whole skill tree, which ``render_checked`` would report."""
    files, problems = read_tree(TEMPLATE)
    assert problems == []
    template, problems = parse_template(files)
    assert problems == [] and template is not None
    documents, graph = render_inputs(model)
    return render_graph_skill(template, documents, graph)[SKILL_FILE].encode()


@pytest.mark.parametrize("modules", [2, 50])
def test_skill_md_stays_under_the_budget(modules: int) -> None:
    size = len(skill_md(synthetic_model(modules, 25)))
    assert size <= L0_BUDGET, f"{modules} modules: SKILL.md has {size} bytes"


def test_skill_md_does_not_grow_with_the_types() -> None:
    small = skill_md(synthetic_model(5, 10))
    large = skill_md(synthetic_model(5, 99))
    assert len(small) == len(large)  # only the type counts in the module table differ
    assert b"Thing050" not in large
