"""Generated graph skills with a function catalog (plan K12): the ``functions`` block, the
``Functions`` column of the modules table, the ``Functions`` section of module pages and
``references/functions.md`` of a single-module graph; names checks (K12) and the unchanged
output of a graph without functions."""

import re
from typing import Any

import pytest
from support.fixtures import FIXTURES, GOLDENS, assert_golden, compiled_model
from support.functions import valid_sources
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.kinetic.compile import compile_catalog
from graph_cli.skills.graph_skill import FEATURED_LIMIT, render_checked, render_graph_skill
from graph_cli.skills.names import check_names
from graph_cli.skills.template import FUNCTIONS_FILE, MODEL_FILE, MODULES_DIR, parse_template
from graph_cli.skills.tree import SKILL_FILE, read_tree

TEMPLATE = FIXTURES / "skills" / "company_functions" / GRAPH_SKILL_NAME
COMPANY_TEMPLATE = FIXTURES / "skills" / "company" / GRAPH_SKILL_NAME
MODEL = compiled_model("company", "1.1.0")
GOLDEN_DIR = GOLDENS / "skills" / "company_functions"


def template_files(directory: Any = TEMPLATE) -> dict[str, bytes]:
    files, problems = read_tree(directory)
    assert problems == []
    return files


def catalog() -> dict[str, Any]:
    document, problems = compile_catalog(valid_sources(), MODEL)
    assert problems == []
    return document


def render(functions: dict[str, Any] | None, directory: Any = TEMPLATE) -> dict[str, bytes]:
    documents, graph = render_inputs(MODEL, functions=functions)
    rendered, problems = render_checked(
        template_files(directory), documents, graph, name=GRAPH_SKILL_NAME
    )
    assert problems == [] and rendered is not None
    return rendered


def text(files: dict[str, bytes], path: str) -> str:
    return files[path].decode()


def test_goldens_with_a_catalog() -> None:
    for name, data in render(catalog()).items():
        assert_golden(GOLDEN_DIR / "with_catalog" / name, data)


def test_goldens_without_a_catalog() -> None:
    files = render(None)
    assert_golden(GOLDEN_DIR / "without_catalog" / SKILL_FILE, files[SKILL_FILE])
    assert "This graph has no published functions." in text(files, SKILL_FILE)


def test_an_empty_or_deprecated_only_catalog_renders_like_none() -> None:
    assert render({"schema": "graph_cli.functions/1", "functions": []}) == render(None)
    only_old = catalog()
    only_old["functions"] = [{**f, "status": "deprecated"} for f in only_old["functions"]]
    assert render(only_old) == render(None)


def test_a_graph_without_functions_renders_as_before() -> None:
    """The functions input changes nothing for the template of an existing graph."""
    assert render(None, COMPANY_TEMPLATE) == render(None, COMPANY_TEMPLATE)
    with_catalog = render(catalog(), COMPANY_TEMPLATE)
    without = render(None, COMPANY_TEMPLATE)
    assert "Functions" not in text(without, SKILL_FILE)
    assert "## Functions" not in "".join(
        text(without, p) for p in without if p.startswith(MODULES_DIR)
    )
    assert FUNCTIONS_FILE not in without
    assert "| Functions |" in text(with_catalog, SKILL_FILE)  # the column, with a catalog


def test_the_block_lists_the_featured_functions_by_module_then_name() -> None:
    skill = text(render(catalog()), SKILL_FILE)
    lines = [line for line in skill.splitlines() if line.startswith("- `") and "(" in line][:2]
    assert [line.split("(")[0] for line in lines] == [
        "- `missing_required_trainings",
        "- `members_of_unit",
    ]  # module operations, then organization
    assert "- `members_of_unit(unit: ref org:OrgUnit)`: " in skill
    assert "`graph describe functions --module <module> --graph example_graph`" in skill
    assert "`graph describe search <text> --kind function --graph example_graph --json`" in skill
    assert "unit_members" not in skill  # deprecated: left out everywhere
    assert "people_by_email_text" not in skill  # not featured


def test_the_block_lists_at_most_twelve() -> None:
    many = catalog()
    many["functions"] = [
        {**many["functions"][0], "name": f"function_{i:02d}", "featured": True} for i in range(20)
    ]
    skill = text(render(many), SKILL_FILE)
    listed = re.findall(r"^- `function_(\d\d)\(", skill, re.MULTILINE)
    assert len(listed) == FEATURED_LIMIT
    assert listed == [f"{i:02d}" for i in range(FEATURED_LIMIT)]


def test_the_modules_table_counts_active_functions() -> None:
    skill = text(render(catalog()), SKILL_FILE)
    rows = {
        cells[1].strip(" `"): cells[5].strip()
        for cells in (line.split("|") for line in skill.splitlines() if line.startswith("| `"))
    }
    assert rows == {"company": "0", "operations": "3", "organization": "2", "people": "1"}
    assert "| Module | Prefix | What it holds | Types | Functions |" in skill


def test_every_module_page_gets_a_functions_section() -> None:
    files = render(catalog())
    assert FUNCTIONS_FILE not in files  # a modular graph has pages instead
    page = text(files, f"{MODULES_DIR}organization.md")
    section = page.split("## Functions", 1)[1]
    assert "| `members_of_unit` | unit: ref org:OrgUnit |" in section
    assert "graph run <function> --param <name>=<value> --graph example_graph --json" in section
    assert text(files, f"{MODULES_DIR}company.md").endswith("## Functions\n\nNone.\n")


def single_module_catalog() -> dict[str, Any]:
    entry = {
        "name": "recent_items",
        "module": "inventory",
        "summary": "Items added in the last days.",
        "status": "active",
        "featured": True,
        "params": [
            {"name": "days", "type": "integer", "class": None, "enum": None, "many": False}
            | {"default": 7, "summary": "How far back."},
            {"name": "category", "type": "enum", "class": None, "enum": "inv:Category"}
            | {"many": True, "default": None, "summary": "Only these."},
        ],
    }
    return {"schema": "graph_cli.functions/1", "functions": [entry]}


def test_a_single_module_graph_gets_a_functions_page() -> None:
    model = compiled_model("inventory")
    files = read_tree(TEMPLATE)[0]
    documents, graph = render_inputs(model, functions=single_module_catalog())
    template, _ = parse_template(files)
    assert template is not None
    rendered = render_graph_skill(template, documents, graph)
    assert sorted(rendered) == [SKILL_FILE, FUNCTIONS_FILE, MODEL_FILE]
    page = rendered[FUNCTIONS_FILE]
    assert "| `recent_items` | days?: integer, category: list of enum inv:Category |" in page
    assert (
        "- `recent_items(days?: integer, category: list of enum inv:Category)`:"
        in (rendered[SKILL_FILE])
    )


def test_a_curated_functions_page_is_refused() -> None:
    files = {**template_files(), FUNCTIONS_FILE: b"# Mine\n"}
    _, problems = parse_template(files)
    assert [(p.rule, p.path) for p in problems] == [("template", FUNCTIONS_FILE)]


def test_the_catalog_must_have_the_schema() -> None:
    documents, graph = render_inputs(MODEL, functions={"schema": "other/1", "functions": []})
    template, _ = parse_template(template_files())
    assert template is not None
    with pytest.raises(ValueError, match=r"graph_cli\.functions/1"):
        render_graph_skill(template, documents, graph)


# ---- names ------------------------------------------------------------------------------

INVENTORY = {
    "commands": [{"path": ["run"], "options": [{"name": "--param"}]}],
    "global_options": [],
}


def named(body: str, functions: dict[str, Any] | None) -> list[tuple[str, str]]:
    template, problems = parse_template({SKILL_FILE: body.encode()})
    assert problems == [] and template is not None
    documents, graph = render_inputs(MODEL, functions=functions)
    return [(p.rule, p.message) for p in check_names(template, documents, graph, INVENTORY)]


def test_graph_run_must_name_a_published_function() -> None:
    body = (
        "---\nname: x\n---\n"
        "Run `graph run members_of_unit --param unit=<id>`.\n\n"
        "```\ngraph run members_of_unitt --param unit=<id>\n```\n\n"
        "Pattern: `graph run <function> --param key=value`; also `graph run unit_members`.\n"
    )
    found = named(body, catalog())
    assert [rule for rule, _ in found] == ["function"]
    assert "members_of_unitt is not a published function" in found[0][1]
    assert "did you mean members_of_unit?" in found[0][1]


def test_graph_run_without_a_catalog_names_nothing() -> None:
    found = named("---\nname: x\n---\nRun `graph run members_of_unit`.\n", None)
    assert [rule for rule, _ in found] == ["function"]
    assert named("---\nname: x\n---\nNo run here: `graph run <function>`.\n", None) == []
