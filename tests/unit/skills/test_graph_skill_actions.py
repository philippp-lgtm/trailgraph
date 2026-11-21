"""Generated graph skills with an action catalog (actions plan A11): the ``actions`` block with
its featured lines; no count, column, section or actions page (graph start plan S5: a publish
that is not about a featured action changes nothing in the render); the ``graph do`` names
check and the unchanged output of a graph without actions."""

import re
from typing import Any

import pytest
from support.actions import company_model, valid_sources
from support.fixtures import FIXTURES, GOLDENS, assert_golden, compiled_model
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.services.describe import SKILL_ACTION_KEYS, describe_model, skill_actions
from graph_cli.skills.graph_skill import (
    ACTIONS_FEATURED_LIMIT,
    L0_ACTIONS,
    L0_BASE,
    L0_BUDGET,
    L0_FUNCTIONS,
    render_checked,
    render_graph_skill,
)
from graph_cli.skills.names import check_names
from graph_cli.skills.template import ACTIONS_FILE, MODEL_FILE, MODULES_DIR, parse_template
from graph_cli.skills.tree import SKILL_FILE, read_tree

TEMPLATE = FIXTURES / "skills" / "company_actions" / GRAPH_SKILL_NAME
INVENTORY_TEMPLATE = FIXTURES / "skills" / "inventory_actions" / GRAPH_SKILL_NAME
COMPANY_TEMPLATE = FIXTURES / "skills" / "company" / GRAPH_SKILL_NAME
MODEL = company_model()
GOLDEN_DIR = GOLDENS / "skills" / "company_actions"
INVENTORY_ACTIONS = FIXTURES / "actions" / "inventory" / "valid"


def template_files(directory: Any = TEMPLATE) -> dict[str, bytes]:
    files, problems = read_tree(directory)
    assert problems == []
    return files


def catalog() -> dict[str, Any]:
    document, problems = compile_actions(valid_sources(), MODEL)
    assert problems == []
    return document


def render(actions: dict[str, Any] | None, directory: Any = TEMPLATE) -> dict[str, bytes]:
    documents, graph = render_inputs(MODEL, actions=actions)
    rendered, problems = render_checked(
        template_files(directory), documents, graph, name=GRAPH_SKILL_NAME
    )
    assert problems == [] and rendered is not None
    return rendered


def text(files: dict[str, bytes], path: str) -> str:
    return files[path].decode()


def test_the_documents_carry_actions_only_when_given() -> None:
    plain, _ = render_inputs(MODEL)
    assert "actions" not in plain
    given, _ = render_inputs(MODEL, actions=catalog())
    assert {k: v for k, v in given.items() if k != "actions"} == plain


def test_skill_actions_keeps_the_signature_and_nothing_behind_it() -> None:
    reduced = skill_actions(catalog())
    assert reduced["schema"] == "graph_cli.action_catalog/1"
    entries = reduced["actions"]
    assert isinstance(entries, list) and len(entries) == 4
    for entry in entries:
        assert isinstance(entry, dict)
        assert set(entry) <= set(SKILL_ACTION_KEYS)
        assert {"name", "module", "summary", "status", "featured", "params", "reason"} <= set(entry)
    text_ = str(reduced)
    for secret in ("'ops'", "'needs'", "'preconditions'", "'hash'", "'touches'", "use_when"):
        assert secret not in text_


def test_describe_model_without_actions_is_unchanged() -> None:
    from support.skills import graph_entry

    entry = graph_entry(MODEL)
    assert describe_model(entry, MODEL) == describe_model(entry, MODEL, None, None)


def test_goldens_with_a_catalog() -> None:
    for name, data in render(catalog()).items():
        assert_golden(GOLDEN_DIR / "with_catalog" / name, data)


def test_goldens_without_a_catalog() -> None:
    files = render(None)
    assert_golden(GOLDEN_DIR / "without_catalog" / SKILL_FILE, files[SKILL_FILE])
    assert "This graph has no published actions." in text(files, SKILL_FILE)


def test_an_empty_or_deprecated_only_catalog_renders_like_none() -> None:
    assert render({"schema": "graph_cli.action_catalog/1", "actions": []}) == render(None)
    only_old = catalog()
    only_old["actions"] = [{**a, "status": "deprecated"} for a in only_old["actions"]]
    assert render(only_old) == render(None)


def test_a_graph_without_actions_renders_as_before() -> None:
    without = render(None, COMPANY_TEMPLATE)
    assert "Actions" not in text(without, SKILL_FILE)
    assert "Its functions and actions" not in "".join(
        text(without, p) for p in without if p.startswith(MODULES_DIR)
    )
    assert ACTIONS_FILE not in without
    with_catalog = render(catalog(), COMPANY_TEMPLATE)
    assert "| Actions |" not in text(with_catalog, SKILL_FILE)  # no column, even with a catalog


def test_a_publish_that_is_not_about_a_featured_action_changes_nothing() -> None:
    """The point of S5: new, changed, deprecated and removed actions that are not featured leave
    every rendered file as it was; featuring one, or a changed featured line, changes it."""
    base = render(catalog())
    plain = [i for i, a in enumerate(catalog()["actions"]) if a.get("featured") is not True]
    assert plain, "the fixture has an action that is not featured"
    changed = catalog()
    first = changed["actions"][plain[0]]
    changed["actions"][plain[0]] = {**first, "summary": "Other text."}
    changed["actions"].append({**first, "name": "one_more_action"})
    changed["actions"].append({**first, "name": "gone_action", "status": "deprecated"})
    assert render(changed) == base
    removed = catalog()
    removed["actions"] = [a for a in removed["actions"] if a.get("featured") is True]
    assert render(removed) == base
    featured = catalog()
    featured["actions"][plain[0]] = {**featured["actions"][plain[0]], "featured": True}
    assert render(featured) != base
    edited = catalog()
    loud = next(i for i, a in enumerate(edited["actions"]) if a.get("featured") is True)
    edited["actions"][loud] = {**edited["actions"][loud], "summary": "A new summary."}
    assert render(edited) != base


def test_the_block_lists_the_featured_actions_with_the_pointers() -> None:
    skill = text(render(catalog()), SKILL_FILE)
    assert "Named actions: for a change use one" in skill
    assert "4 named actions" not in skill  # no count: the tool says it
    assert "Dry run first: `graph do <action> --param <name>=<value> --graph example_graph" in skill
    block = skill.split("## Changes", 1)[1].split("## Names", 1)[0]
    lines = [line for line in block.splitlines() if line.startswith("- `")]
    names = [line.split("(")[0].removeprefix("- `") for line in lines]
    assert names == ["assign_position", "hand_over_position", "offboard_employee"]  # by module
    assert "create_task" not in block  # not featured
    assert (
        "- `assign_position(person: ref ppl:Person, position: ref org:Position, since: date)`: "
        in block
    )
    assert (
        "`hand_over_position(position: ref org:Position, person: ref ppl:Person, since?: date)`"
        in block
    )
    assert "`graph describe actions --module <module> --graph example_graph`" in skill
    assert "`graph describe search <text> --kind action --graph example_graph --json`" in skill
    assert "`graph describe action <action> --graph example_graph`" in skill


def test_the_block_lists_at_most_eight() -> None:
    many = catalog()
    many["actions"] = [
        {**many["actions"][0], "name": f"action_{i:02d}", "featured": True} for i in range(20)
    ]
    skill = text(render(many), SKILL_FILE)
    listed = re.findall(r"^- `action_(\d\d)\(", skill, re.MULTILINE)
    assert len(listed) == ACTIONS_FEATURED_LIMIT == 8
    assert listed == [f"{i:02d}" for i in range(ACTIONS_FEATURED_LIMIT)]


def test_deprecated_actions_are_left_out_everywhere() -> None:
    old = catalog()
    old["actions"][0] = {**old["actions"][0], "status": "deprecated"}
    name = old["actions"][0]["name"]
    files = render(old)
    assert not any(name in text(files, path) for path in files if path.endswith(".md"))
    assert "named actions;" not in text(files, SKILL_FILE)


def test_an_optional_parameter_is_marked() -> None:
    entry = {
        "name": "tag_item",
        "module": "inventory",
        "summary": "Tag an item.",
        "status": "active",
        "featured": True,
        "params": [
            {"name": "item", "type": "ref", "class": "inv:Item", "enum": None, "many": False}
            | {"default": None, "summary": "The item."},
            {"name": "note", "type": "string", "class": None, "enum": None, "many": False}
            | {"default": None, "optional": True, "summary": "A note."},
        ],
    }
    files = single_module_render({"schema": "graph_cli.action_catalog/1", "actions": [entry]})
    assert "- `tag_item(item: ref inv:Item, note?: string)`: Tag an item." in files[SKILL_FILE]


def test_an_action_that_needs_a_reason_says_so() -> None:
    entry = {
        "name": "retire_item",
        "module": "inventory",
        "summary": "Retire an item.",
        "status": "active",
        "featured": True,
        "reason": "required",
        "params": [
            {"name": "item", "type": "ref", "class": "inv:Item", "enum": None, "many": False}
            | {"default": None, "summary": "The item."},
        ],
    }
    catalog_ = {"schema": "graph_cli.action_catalog/1", "actions": [entry]}
    files = single_module_render(catalog_)
    line = "- `retire_item(item: ref inv:Item)`: Retire an item. Needs `--reason`."
    assert line in files[SKILL_FILE]
    assert ACTIONS_FILE not in files  # no page: the line of the block says it
    optional = single_module_render(
        {"schema": "graph_cli.action_catalog/1", "actions": [entry | {"reason": "optional"}]}
    )
    assert "Needs `--reason`" not in "".join(optional.values())


def test_the_modules_table_has_no_action_column() -> None:
    skill = text(render(catalog()), SKILL_FILE)
    assert "| Module | Prefix | What it holds | Types | Links to other modules |" in skill
    assert "Actions |" not in skill.split("| Module |", 1)[1].split("\n", 1)[0]


def test_every_module_page_has_one_pointer_instead_of_an_actions_section() -> None:
    files = render(catalog())
    assert ACTIONS_FILE not in files  # a modular graph has pages instead
    pages = [p for p in files if p.startswith(MODULES_DIR)]
    assert pages
    for path in pages:
        page = text(files, path)
        module = path.removeprefix(MODULES_DIR).removesuffix(".md")
        assert "## Actions" not in page and "assign_position" not in page
        assert page.endswith(
            f"\nIts functions and actions: `graph describe module {module}"
            " --graph example_graph`.\n"
        )


def single_module_render(actions: dict[str, Any]) -> dict[str, str]:
    model = compiled_model("inventory")
    documents, graph = render_inputs(model, actions=actions)
    template, _ = parse_template(template_files(INVENTORY_TEMPLATE))
    assert template is not None
    return render_graph_skill(template, documents, graph)


def inventory_catalog() -> dict[str, Any]:
    sources = {p.name: p.read_text() for p in sorted(INVENTORY_ACTIONS.glob("*.yaml"))}
    document, problems = compile_actions(sources, compiled_model("inventory"))
    assert problems == []
    return document


def test_a_single_module_graph_gets_no_actions_page() -> None:
    rendered = single_module_render(inventory_catalog())
    assert sorted(rendered) == [SKILL_FILE, MODEL_FILE]  # the page is no longer generated
    assert_golden(
        GOLDENS / "skills" / "inventory_actions" / SKILL_FILE, rendered[SKILL_FILE].encode()
    )


def test_a_curated_actions_page_is_refused() -> None:
    files = {**template_files(), ACTIONS_FILE: b"# Mine\n"}
    _, problems = parse_template(files)
    assert [(p.rule, p.path) for p in problems] == [("template", ACTIONS_FILE)]


def test_the_catalog_must_have_the_schema() -> None:
    documents, graph = render_inputs(MODEL, actions={"schema": "other/1", "actions": []})
    template, _ = parse_template(template_files())
    assert template is not None
    with pytest.raises(ValueError, match=r"graph_cli\.action_catalog/1"):
        render_graph_skill(template, documents, graph)


def test_the_budget_adds_up() -> None:
    assert (
        (L0_FUNCTIONS, L0_ACTIONS, L0_BUDGET)
        == (2_400, 1_950, L0_BASE + 4_350)
        == (
            2_400,
            1_950,
            24_350,
        )
    )


# ---- names ------------------------------------------------------------------------------

INVENTORY = {
    "commands": [
        {"path": ["do"], "options": [{"name": "--param"}, {"name": "--dry-run"}]},
        {"path": ["run"], "options": [{"name": "--param"}]},
    ],
    "global_options": [],
}


def named(body: str, actions: dict[str, Any] | None) -> list[tuple[str, str]]:
    template, problems = parse_template({SKILL_FILE: body.encode()})
    assert problems == [] and template is not None
    documents, graph = render_inputs(MODEL, actions=actions)
    return [(p.rule, p.message) for p in check_names(template, documents, graph, INVENTORY)]


def test_graph_do_must_name_a_published_action() -> None:
    body = (
        "---\nname: x\n---\n"
        "Run `graph do assign_position --param position=<id> --dry-run`.\n\n"
        "```\ngraph do assign_positon --param position=<id>\n```\n\n"
        "Pattern: `graph do <action> --param key=value`; also `graph do shutdown_unit`.\n"
    )
    found = named(body, catalog())
    assert [rule for rule, _ in found] == ["action", "action"]
    assert "assign_positon is not a published action" in found[0][1]
    assert "did you mean assign_position?" in found[0][1]
    assert "shutdown_unit is not a published action" in found[1][1]


def test_a_deprecated_action_is_still_a_known_name() -> None:
    old = catalog()
    old["actions"] = [{**a, "status": "deprecated"} for a in old["actions"]]
    assert named("---\nname: x\n---\n`graph do assign_position --dry-run`\n", old) == []


def test_without_a_catalog_no_action_is_known() -> None:
    found = named("---\nname: x\n---\n`graph do assign_position`\n", None)
    assert [rule for rule, _ in found] == ["action"]
