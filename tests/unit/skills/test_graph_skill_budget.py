"""Level 0 budget of generated graph skills: ``SKILL.md`` of a modular graph stays under
``L0_BUDGET`` on a large synthetic ontology (50 modules, 1250 types), and its size does not
depend on how many types a module holds; the types are on level 1 (module pages,
``describe module``). ``measure`` holds every graph to the budget, single-module graphs
included, and the curated text to ``CURATED_BUDGET``."""

import json
from typing import Any

import pytest
from support.fixtures import FIXTURES, TOY_ONTOLOGIES, compiled_model
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.skills.graph_skill import (
    ACTIONS_FEATURED_LIMIT,
    CURATED_BUDGET,
    FEATURED_LIMIT,
    L0_ACTIONS,
    L0_BASE,
    L0_BUDGET,
    L0_FUNCTIONS,
    measure,
    render_graph_skill,
)
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import MAX_TOTAL_BYTES, SKILL_FILE, read_tree

# The curated template of the modular fixture: start, modules, names, model.
TEMPLATE = FIXTURES / "skills" / "company" / GRAPH_SKILL_NAME
BLOCKS = FIXTURES / "skills" / "blocks" / GRAPH_SKILL_NAME  # every block, single-module toys
MAIN = "synthetic"
LINKS = 3  # each module links to up to this many earlier modules, one relation each
SUMMARY_100 = (
    "People holding a position in a unit or in any unit below it, with the title of a position"
    " they hold."
)
assert len(SUMMARY_100) == 100
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


def catalog(modules: int, per_module: int, featured: int = FEATURED_LIMIT) -> dict[str, Any]:
    """A compiled catalog of ``per_module`` functions in each of the first ``modules`` areas;
    the first ``featured`` of them (by module, then name) are featured. Summaries and
    signatures have the length the format allows or a real function would have."""
    entries = []
    for index in range(modules):
        for number in range(per_module):
            entries.append(
                {
                    "name": f"function_{index:02d}_{number:03d}",
                    "module": _module_id(index),
                    "summary": SUMMARY_100,
                    "status": "active",
                    "featured": len(entries) < featured,
                    "params": [
                        {"name": "unit", "type": "ref", "class": "a00:Thing000", "many": False}
                        | {"enum": None, "default": None, "summary": "The unit to list."},
                        {"name": "since", "type": "date", "class": None, "many": False}
                        | {"enum": None, "default": "2000-01-01", "summary": "From when."},
                    ],
                }
            )
    return {"schema": "graph_cli.functions/1", "functions": entries}


def action_catalog(
    modules: int, per_module: int, featured: int = ACTIONS_FEATURED_LIMIT
) -> dict[str, Any]:
    """A compiled action catalog of ``per_module`` actions in each of the first ``modules``
    areas, the first ``featured`` of them featured, with the longest summary the format
    allows and a signature of two parameters."""
    entries = []
    for index in range(modules):
        for number in range(per_module):
            entries.append(
                {
                    "name": f"action_{index:02d}_{number:03d}",
                    "module": _module_id(index),
                    "summary": SUMMARY_100,
                    "status": "active",
                    "featured": len(entries) < featured,
                    "params": [
                        {"name": "unit", "type": "ref", "class": "a00:Thing000", "many": False}
                        | {"enum": None, "default": None, "summary": "The unit."},
                        {"name": "since", "type": "date", "class": None, "many": False}
                        | {"enum": None, "default": None, "summary": "From when."},
                    ],
                }
            )
    return {"schema": "graph_cli.action_catalog/1", "actions": entries}


def skill_md(model: CompiledModel, functions: dict[str, Any] | None = None) -> bytes:
    """``SKILL.md`` alone: the reference pages of a model this large go over the size limit of
    a whole skill tree, which ``render_checked`` would report."""
    return rendered(model, functions)[SKILL_FILE].encode()


def rendered(
    model: CompiledModel,
    functions: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
) -> dict[str, str]:
    files, problems = read_tree(TEMPLATE)
    assert problems == []
    template, problems = parse_template(files)
    assert problems == [] and template is not None
    documents, graph = render_inputs(model, functions=functions, actions=actions)
    return render_graph_skill(template, documents, graph)


@pytest.mark.parametrize("modules", [2, 50])
def test_skill_md_stays_under_the_budget(modules: int) -> None:
    size = len(skill_md(synthetic_model(modules, 25)))
    assert size <= L0_BASE, f"{modules} modules: SKILL.md has {size} bytes"


def test_skill_md_does_not_grow_with_the_types() -> None:
    small = skill_md(synthetic_model(5, 10))
    large = skill_md(synthetic_model(5, 99))
    assert len(small) == len(large)  # only the type counts in the module table differ
    assert b"Thing050" not in large


def test_skill_md_with_functions_stays_under_the_budget() -> None:
    """50 modules x 20 functions, 12 featured: the template of the fixture has no functions
    block, so the block (twelve lines and two pointers) is added to it here."""
    size = len(with_block(synthetic_model(50, 25), catalog(50, 20)))
    assert size <= L0_BUDGET, f"SKILL.md has {size} bytes"


def with_block(
    model: CompiledModel,
    functions: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
) -> bytes:
    files, _ = read_tree(TEMPLATE)
    if functions is not None:
        files[SKILL_FILE] += b"\n## Functions\n\n<!-- graph_cli:block functions -->\n"
    if actions is not None:
        files[SKILL_FILE] += b"\n## Actions\n\n<!-- graph_cli:block actions -->\n"
    template, problems = parse_template(files)
    assert problems == [] and template is not None
    documents, graph = render_inputs(model, functions=functions, actions=actions)
    return render_graph_skill(template, documents, graph)[SKILL_FILE].encode()


def test_skill_md_with_functions_and_actions_stays_under_the_budget() -> None:
    """50 modules x 20 functions x 10 actions, 12 and 8 featured: both blocks and both
    columns on top of the modular base."""
    size = len(with_block(synthetic_model(50, 25), catalog(50, 20), action_catalog(50, 10)))
    assert size <= L0_BUDGET, f"SKILL.md has {size} bytes"


def test_skill_md_does_not_grow_with_the_actions() -> None:
    model = synthetic_model(5, 10)
    small = with_block(model, actions=action_catalog(5, 10))
    large = with_block(model, actions=action_catalog(5, 19))
    assert len(small) == len(large)  # the same eight featured, counts of equal width
    assert b"action_04_018" not in large  # not featured: on the module page only


def test_skill_md_does_not_grow_with_the_functions() -> None:
    model = synthetic_model(5, 10)
    small = with_block(model, catalog(5, 10))
    large = with_block(model, catalog(5, 19))
    assert len(small) == len(large)  # the same twelve featured, counts of equal width
    assert b"function_04_018" not in large  # not featured: on the module page only


def test_the_reference_tree_of_a_thousand_actions_fits() -> None:
    """1,000 actions (20 modules x 50) on the pages of a model of 20 modules, as for functions.
    Both catalogs at their limit together do not fit the tree limit (about 400 KB of 256 KB);
    a graph in that range needs a reference page below the module pages."""
    files = rendered(synthetic_model(20, 3), actions=action_catalog(20, 50))
    total = sum(len(text.encode()) for text in files.values())
    assert total <= MAX_TOTAL_BYTES, f"{total} bytes of {MAX_TOTAL_BYTES}"
    assert files["references/modules/area_07.md"].count("| `action_07_") == 50


def test_the_reference_tree_of_a_thousand_functions_fits() -> None:
    """1,000 functions with the longest summary (20 modules x 50) on the pages of a model of
    20 modules with 3 types each. A function costs about 180 bytes on its page, so a thousand
    take about 70 percent of the tree limit; the model pages share the rest."""
    files = rendered(synthetic_model(20, 3), catalog(20, 50))
    total = sum(len(text.encode()) for text in files.values())
    assert total <= MAX_TOTAL_BYTES, f"{total} bytes of {MAX_TOTAL_BYTES}"
    page = files["references/modules/area_07.md"]
    assert page.count("| `function_07_") == 50


# ---- measure: every graph, and the curated text ---------------------------------------------


def template_of(directory: Any, extra: bytes = b"") -> Any:
    files, problems = read_tree(directory)
    assert problems == []
    files[SKILL_FILE] += extra
    template, problems = parse_template(files)
    assert problems == [] and template is not None
    return template


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_single_module_toys_stay_under_both_budgets(ontology: str) -> None:
    documents, graph = render_inputs(compiled_model(ontology))
    size = measure(template_of(BLOCKS), documents, graph)
    assert size.budget == L0_BASE  # no functions, no actions
    assert size.skill <= size.budget and size.curated <= CURATED_BUDGET and not size.over
    rendered = render_graph_skill(template_of(BLOCKS), documents, graph)[SKILL_FILE]
    assert size.skill == len(rendered.encode())


def test_measure_counts_curated_text_outside_frontmatter_and_markers() -> None:
    documents, graph = render_inputs(compiled_model("inventory"))
    plain = measure(template_of(BLOCKS), documents, graph)
    longer = measure(template_of(BLOCKS, b"\n" + "ü".encode() * 10 + b"\n"), documents, graph)
    assert 20 < longer.curated - plain.curated < 25  # ten two-byte letters and line ends
    files, _ = read_tree(BLOCKS)
    body = files[SKILL_FILE].decode().split("\n---\n", 1)[1]
    markers = [line for line in body.splitlines() if line.startswith("<!-- graph_cli:block")]
    assert plain.curated < len(body.encode()) - sum(len(m) for m in markers) + 1
    assert set(plain.blocks) == {line.split()[2] for line in markers}
    assert sum(plain.blocks.values()) < plain.skill


def test_curated_text_over_its_budget_is_over() -> None:
    documents, graph = render_inputs(compiled_model("inventory"))
    size = measure(template_of(BLOCKS, b"x" * CURATED_BUDGET), documents, graph)
    assert size.curated > CURATED_BUDGET and size.over
    assert size.to_json()["curated_budget"] == CURATED_BUDGET
    blocks = list(size.to_json()["blocks"])  # largest first
    assert blocks[0] == max(size.blocks, key=lambda name: size.blocks[name])


def test_a_large_single_module_graph_is_over_the_budget() -> None:
    """The budget is the signal to split a growing model into modules."""
    documents, graph = render_inputs(compiled_model("inventory"))
    item = documents["type"]["inv:Item"]
    for n in range(80):
        curie = f"inv:Synthetic{n}"
        documents["type"][curie] = {**item, "type": {**item["type"], "curie": curie}}
        documents["types"]["types"].append({"curie": curie, "abstract": False})
        graph["alias_prefixes"][curie] = f"S{n}"
    size = measure(template_of(BLOCKS), documents, graph)
    assert size.skill > L0_BASE and size.over
    assert next(iter(size.to_json()["blocks"])) == "types"


def test_functions_and_actions_raise_the_budget() -> None:
    model = synthetic_model(2, 3)
    template = template_of(
        TEMPLATE,
        b"\n<!-- graph_cli:block functions -->\n\n<!-- graph_cli:block actions -->\n",
    )
    documents, graph = render_inputs(model, functions=catalog(2, 2), actions=action_catalog(2, 2))
    assert measure(template, documents, graph).budget == L0_BASE + L0_FUNCTIONS + L0_ACTIONS
    documents, graph = render_inputs(model, functions=catalog(2, 2))
    assert measure(template, documents, graph).budget == L0_BASE + L0_FUNCTIONS
