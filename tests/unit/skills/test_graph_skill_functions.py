"""Generated graph skills with a function catalog (plan K12): the ``functions`` block with its
featured lines, one pointer to ``graph describe module`` instead of per-module counts, columns,
sections and a functions page (graph start plan S5: a publish that is not about a featured
function changes nothing in the render); names checks (K12) and the unchanged output of a graph
without functions."""

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
    """The functions input changes nothing for the template of an existing graph without them."""
    without = render(None, COMPANY_TEMPLATE)
    assert "Functions" not in text(without, SKILL_FILE)
    assert "Its functions and actions" not in "".join(
        text(without, p) for p in without if p.startswith(MODULES_DIR)
    )
    assert FUNCTIONS_FILE not in without
    with_catalog = render(catalog(), COMPANY_TEMPLATE)
    assert "| Functions |" not in text(with_catalog, SKILL_FILE)  # no column, even with a catalog
    assert "are listed by the same command, not here" in text(with_catalog, SKILL_FILE)


def test_a_publish_that_is_not_about_a_featured_function_changes_nothing() -> None:
    """The point of S5: new, changed, deprecated and removed functions that are not featured
    leave every rendered file as it was; featuring one (or unfeaturing) does change it."""
    base = render(catalog())
    changed = catalog()
    plain = [i for i, f in enumerate(changed["functions"]) if f.get("featured") is not True]
    assert plain, "the fixture has a function that is not featured"
    changed["functions"][plain[0]] = {**changed["functions"][plain[0]], "summary": "Other text."}
    changed["functions"].append({**changed["functions"][plain[0]], "name": "one_more_function"})
    changed["functions"].append(
        {**changed["functions"][plain[0]], "name": "gone_function", "status": "deprecated"}
    )
    changed["functions"].append({**changed["functions"][plain[0]], "module": "legal"})
    assert render(changed) == base
    removed = catalog()
    removed["functions"] = [f for f in removed["functions"] if f.get("featured") is True]
    assert render(removed) == base
    featured = catalog()
    featured["functions"][plain[0]] = {**featured["functions"][plain[0]], "featured": True}
    assert render(featured) != base


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


def test_the_modules_table_has_no_function_column_and_points_to_the_module_view() -> None:
    skill = text(render(catalog()), SKILL_FILE)
    assert "| Module | Prefix | What it holds | Types | Links to other modules |" in skill
    assert "Functions" not in skill.split("| Module |", 1)[1].split("\n", 1)[0]
    assert (
        "are listed by the same command, not here: `graph describe module <module>"
        " --graph example_graph`."
    ) in skill


def test_every_module_page_has_one_pointer_instead_of_a_functions_section() -> None:
    files = render(catalog())
    assert FUNCTIONS_FILE not in files  # a modular graph has pages instead
    for path in (p for p in files if p.startswith(MODULES_DIR)):
        page = text(files, path)
        module = path.removeprefix(MODULES_DIR).removesuffix(".md")
        assert "## Functions" not in page and "members_of_unit" not in page
        assert page.endswith(
            f"\nIts functions and actions: `graph describe module {module}"
            " --graph example_graph`.\n"
        )


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


def test_a_single_module_graph_gets_no_functions_page() -> None:
    model = compiled_model("inventory")
    files = read_tree(TEMPLATE)[0]
    documents, graph = render_inputs(model, functions=single_module_catalog())
    template, _ = parse_template(files)
    assert template is not None
    rendered = render_graph_skill(template, documents, graph)
    assert sorted(rendered) == [SKILL_FILE, MODEL_FILE]  # the page is no longer generated
    assert (
        "- `recent_items(days?: integer, category: list of enum inv:Category)`:"
        in (rendered[SKILL_FILE])
    )


def test_a_curated_functions_page_is_refused() -> None:
    files = {**template_files(), FUNCTIONS_FILE: b"# Mine\n"}
    _, problems = parse_template(files)
    assert [(p.rule, p.path) for p in problems] == [("template", FUNCTIONS_FILE)]


def test_a_reader_who_may_not_run_functions_gets_one_line_and_no_name() -> None:
    withheld = {**catalog(), "functions": [], "withheld": True}
    files = render(withheld)
    skill = text(files, SKILL_FILE)
    assert "This token may not run named functions in this graph (`read.function`)" in skill
    assert "members_of_unit" not in "".join(text(files, p) for p in files)
    assert "Named functions:" not in skill and "graph run <function>" not in skill
    assert render(None) != files  # not the line of a graph without functions


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
