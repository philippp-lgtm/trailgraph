"""The hand-written `authoring-graph-functions` skill agrees with the function format.

- Every complete function file in the skill (a YAML block that starts with the schema
  line) compiles against the `company` fixture without a problem.
- The format reference names every key the parser accepts (read from the parser with a
  probe file that has an unknown key in every mapping), every parameter type and status,
  and every `function.*` code a publish check can report.
- The format has one home: `SKILL.md` links to the reference and does not repeat the key
  tables.

Links and anchors are checked by ``tests/test_docs.py``, command lines by
``tests/skills/test_admin_skills.py``.
"""

import re

import pytest
from support.fixtures import compiled_model
from support.skills import ADMIN_SKILLS_DIR

from graph_cli.kernel.error_codes import all_error_codes
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.format import FUNCTION_SCHEMA, PARAM_TYPES, STATUSES, load_function

SKILL_DIR = ADMIN_SKILLS_DIR / "authoring-graph-functions"
SKILL = (SKILL_DIR / "SKILL.md").read_text()
FORMAT = (SKILL_DIR / "references" / "format.md").read_text()

YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
UNKNOWN = "zz_unknown"

# What `graph run` and the listings report; publishing never does.
RUNTIME_CODES = {
    "function.not_found",
    "function.parameter_missing",
    "function.parameter_unknown",
    "function.parameter_invalid",
    "function.parameter_type",
    "function.deprecated",
}

PROBE = f"""\
schema: {FUNCTION_SCHEMA}
{UNKNOWN}: 1
name: probe
summary: s
use_when: u
params:
  p: {{type: string, summary: s, {UNKNOWN}: 1}}
query: RETURN 1 AS one
returns:
  - {{column: one, summary: s, {UNKNOWN}: 1}}
examples:
  - {{question: q, {UNKNOWN}: 1}}
"""


def _blocks() -> list[str]:
    return [m.group(1) for m in YAML_BLOCK.finditer(FORMAT + SKILL)]


def _accepted_keys() -> set[str]:
    _, problems = load_function("probe.yaml", PROBE)
    keys: set[str] = set()
    found = [p for p in problems if p.code == "function.unknown_key"]
    assert len(found) == 4  # file, parameter, column, example
    for problem in found:
        expected = problem.details["expected"]
        assert isinstance(expected, list)
        keys.update(str(key) for key in expected)
    return keys


def test_the_skill_has_complete_examples() -> None:
    assert [b for b in _blocks() if b.startswith(f"schema: {FUNCTION_SCHEMA}\n")]


@pytest.mark.parametrize("block", _blocks())
def test_every_complete_file_compiles(block: str) -> None:
    if not block.startswith(f"schema: {FUNCTION_SCHEMA}\n"):
        pytest.skip("a fragment")
    name = re.search(r"^name: (\w+)$", block, re.MULTILINE)
    assert name
    document, problems = compile_catalog(
        {f"{name.group(1)}.yaml": block}, compiled_model("company", "1.1.0")
    )
    assert problems == []
    assert len(document["functions"]) == 1  # type: ignore[arg-type]


def test_the_reference_names_every_key_and_word() -> None:
    spans = set(CODE_SPAN.findall(FORMAT))
    mentioned = {word for span in spans for word in re.findall(r"[a-z_]+", span)}
    keys = _accepted_keys()
    assert {"schema", "name", "use_when", "replaced_by", "class", "enum", "many"} <= keys
    assert sorted(keys - mentioned) == []
    for word in (*PARAM_TYPES, *STATUSES):
        assert word in spans or f"`{word}`" in FORMAT, word


def test_the_reference_names_every_publish_code() -> None:
    codes = {c for c in all_error_codes() if c.startswith("function.")} - RUNTIME_CODES
    text = FORMAT + SKILL
    assert sorted(c for c in codes if c not in text) == []


def test_the_format_has_one_home() -> None:
    assert "references/format.md" in SKILL
    for heading in ("## Top-level keys", "## Parameters", "### Type fit"):
        assert heading in FORMAT
        assert heading not in SKILL
