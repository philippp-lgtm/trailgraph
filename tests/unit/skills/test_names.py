"""Checks of curated graph skill text: terms, commands, flags and aliases (Q3, Q10).

Each refusal has a positive control: the same text with the mistake fixed passes.
"""

import json
from typing import Any

import pytest
from support.fixtures import GOLDENS, compiled_model
from support.skills import render_inputs

from graph_cli.skills.names import check_names
from graph_cli.skills.template import parse_template

FRONT = '---\nname: graph-example-graph\ndescription: "Example."\n---\n\n'


def inventory() -> dict[str, Any]:
    found: dict[str, Any] = json.loads((GOLDENS / "app" / "inventory.json").read_text())
    return found


def problems(body: str, extra: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    files = {"SKILL.md": (FRONT + body).encode()}
    files.update({name: text.encode() for name, text in (extra or {}).items()})
    template, broken = parse_template(files)
    assert broken == [] and template is not None
    documents, graph = render_inputs(compiled_model("inventory"))
    return [
        (p.rule, p.path, p.message) for p in check_names(template, documents, graph, inventory())
    ]


GOOD = """\
# Items

<!-- graph_cli:block types -->

`inv:Item` items sit in an `inv:Location` through `inv:located_in`; the
condition is an `inv:Condition`, the state an `inv:ItemLifecycle`.

```sh
graph find --type inv:Item --rel inv:located_in=LOC-0001 --graph example_graph --json
graph create inv:Item --set name="Two words" --set quantity=2 \\
  --edge inv:located_in=LOC-0001 --graph example_graph --dry-run --json
graph apply - --graph example_graph --dry-run --json <<'JSON'
{"reason": "from graph find --help", "ops": [{"op": "create", "type": "inv:Item",
  "edges": [{"rel": "inv:in_category", "target": "CAT-0012"}]}]}
JSON
graph get ITEM-10000 --graph example_graph --json | jq .data
```

Read `graph describe type inv:Item --json` or `graph --help`; the `graph`
command line is on PATH, and text in UTF-8 or ISO-8601 dates stays plain.
"""


def test_good_text_has_no_problems() -> None:
    assert problems(GOOD) == []


@pytest.mark.parametrize(
    ("wrong", "right", "rule", "message"),
    [
        (
            "`inv:Itme`",
            "`inv:Item`",
            "term",
            "inv:Itme is not in the model; did you mean inv:Item?",
        ),
        ("relation inv:stored_in", "relation inv:located_in", "term", "inv:stored_in"),
        ("`graph serach x`", "`graph lookup x`", "command", "'graph serach' is not a command"),
        ("`graph describe typo`", "`graph describe type`", "command", "'graph describe typo'"),
        (
            "`graph find --kind x`",
            "`graph find --type x`",
            "command",
            "graph find has no option --kind",
        ),
        (
            "`graph get ITEM-1`",
            "`graph get ITEM-0001`",
            "alias",
            "ITEM-1 is not an alias; aliases look like ITEM-0001",
        ),
        ("item ITEM-00001", "item ITEM-0001", "alias", "ITEM-00001"),
        (
            "`graph get SCOPE-0001`",
            "`graph get ITEM-0001`",
            "alias",
            "SCOPE-0001 has a prefix this graph does not use",
        ),
    ],
)
def test_each_rule_with_a_positive_control(wrong: str, right: str, rule: str, message: str) -> None:
    (found,) = problems(f"See {wrong}.\n")
    assert found[0] == rule and found[1] == "SKILL.md"
    assert message in found[2] and found[2].startswith("line 6: ")
    assert problems(f"See {right}.\n") == []


def test_prefixes_outside_the_model_and_generated_blocks_are_not_checked() -> None:
    assert problems("`other:Thing`, https://example.org/a:b and SCOPE-1 in prose.\n") == []
    # The types block names every inv: term; only the curated text around it is read.
    assert problems("<!-- graph_cli:block types -->\n<!-- graph_cli:block names -->\n") == []


def test_extra_files_are_checked_with_their_line() -> None:
    found = problems("Text.\n", {"references/recipes.md": "# Recipes\n\n`graph find --typo`\n"})
    assert found == [
        ("command", "references/recipes.md", "line 3: graph find has no option --typo")
    ]


def test_code_block_lines_and_continuations() -> None:
    body = "Intro.\n\n```sh\ngraph find --type inv:Item \\\n  --bogus 1\ngraph nope\n```\n"
    assert problems(body) == [
        ("command", "SKILL.md", "line 9: graph find has no option --bogus"),
        ("command", "SKILL.md", "line 11: 'graph nope' is not a command of graph"),
    ]
