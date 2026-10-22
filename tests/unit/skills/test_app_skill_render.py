"""The skill 1 renderer is a pure, strict function of the app inventory (B11, item 7 S1)."""

import json
import random
from typing import Any

import pytest
from support.fixtures import GOLDENS

from graph_cli.kernel.canonical import hash_value
from graph_cli.skills.app_skill import (
    COMMANDS_FILE,
    ERRORS_FILE,
    QUERY_FILE,
    SKILL_FILE,
    render_app_skill,
)
from graph_cli.skills.markdown import cell, table


def inventory() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((GOLDENS / "app" / "inventory.json").read_text())
    return loaded


def _reordered(value: Any, rng: random.Random) -> Any:
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {k: _reordered(value[k], rng) for k in keys}
    if isinstance(value, list):
        return [_reordered(v, rng) for v in value]
    return value


def test_renders_the_three_files_deterministically() -> None:
    first = render_app_skill(inventory())
    assert sorted(first) == sorted([SKILL_FILE, COMMANDS_FILE, ERRORS_FILE, QUERY_FILE])
    assert render_app_skill(inventory()) == first
    assert all(text.endswith("\n") and "\r" not in text for text in first.values())


def test_output_ignores_key_and_command_order() -> None:
    doc = inventory()
    rng = random.Random(7)
    for _ in range(3):
        shuffled = _reordered(doc, rng)
        rng.shuffle(shuffled["commands"])
        rng.shuffle(shuffled["error_codes"])
        assert render_app_skill(shuffled) == render_app_skill(doc)


def test_the_stamp_follows_the_inventory() -> None:
    doc = inventory()
    skill = render_app_skill(doc)[SKILL_FILE]
    assert f'inventory_hash: "{hash_value(doc)}"' in skill
    assert f'program_version: "{doc["program_version"]}"' in skill
    changed = {**doc, "program_version": "9.9.9"}
    assert render_app_skill(changed)[SKILL_FILE] != skill


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "graph_cli.inventory/2"}, "expected graph_cli.inventory/1"),
        ({"surface": "maint"}, "app surface"),
        ({"commands": []}, "no commands"),
    ],
)
def test_refuses_what_it_cannot_render(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render_app_skill({**inventory(), **change})


def test_refuses_an_error_category_without_guidance() -> None:
    doc = inventory()
    doc["error_codes"].append({"code": "x.y", "category": "novel", "exit": 7})
    with pytest.raises(ValueError, match="novel"):
        render_app_skill(doc)


def test_table_cells_escape_pipes_and_refuse_line_breaks() -> None:
    assert cell("a|b") == "a\\|b"
    assert table(["A"], [[cell("a|b")]]) == ["| A |", "|---|", "| a\\|b |"]
    for bad in ("two\nlines", "a `tick`"):
        with pytest.raises(ValueError):
            cell(bad)
    doc = inventory()
    doc["commands"][0]["summary"] = "first | second"
    assert "first \\| second" in render_app_skill(doc)[COMMANDS_FILE]
