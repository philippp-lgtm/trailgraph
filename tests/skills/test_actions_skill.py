"""The hand-written `authoring-graph-actions` skill agrees with the action format.

- Every complete action file in the skill (a YAML block that starts with the schema
  line) compiles against the example `company` ontology without a problem.
- The format reference names every key the parser accepts (read from the parser with a
  probe file that has an unknown key in every mapping, every op kind included), every
  parameter type, status and `reason` value, and every `action.*` code a publish check can
  report.
- The format has one home: `SKILL.md` links to the reference and does not repeat the key
  tables.

Links and anchors are checked by ``tests/test_docs.py``, command lines by
``tests/skills/test_admin_skills.py``.
"""

import re

import pytest
from support.actions import company_model
from support.skills import ADMIN_SKILLS_DIR

from graph_cli.kernel.changeset import OP_KEYS
from graph_cli.kernel.error_codes import all_error_codes
from graph_cli.kinetic.action_compile import (
    MAX_ACTIONS,
    MAX_OPS,
    MAX_PRECONDITIONS,
    compile_actions,
)
from graph_cli.kinetic.action_format import ACTION_SCHEMA, DIRECTIONS, REASONS, load_action
from graph_cli.kinetic.format import PARAM_TYPES, STATUSES

SKILL_DIR = ADMIN_SKILLS_DIR / "authoring-graph-actions"
SKILL = (SKILL_DIR / "SKILL.md").read_text()
FORMAT = (SKILL_DIR / "references" / "format.md").read_text()

YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
UNKNOWN = "zz_unknown"

# What `graph do` and the listings report, and what only a stored catalog can show against a
# newer model (`action.stale` is in the skill text as a release and run-time refusal);
# publishing never reports these.
RUNTIME_CODES = {
    "action.not_found",
    "action.parameter_missing",
    "action.parameter_unknown",
    "action.parameter_invalid",
    "action.parameter_type",
    "action.precondition_failed",
    "action.nothing_to_do",
    "action.reason_required",
    "action.deprecated",
}

_OP_PROBES = "".join(f"  - {{op: {kind}, {UNKNOWN}: 1}}\n" for kind in OP_KEYS)
PROBE = f"""\
schema: {ACTION_SCHEMA}
{UNKNOWN}: 1
name: probe
summary: s
use_when: u
params:
  p: {{type: string, summary: s, {UNKNOWN}: 1}}
preconditions:
  - {{id: c, on: p, summary: s, require: [], {UNKNOWN}: 1}}
ops:
{_OP_PROBES}  - {{op: unlink, id: $p, rel: r, all: true, {UNKNOWN}: 1}}
examples:
  - {{request: q, {UNKNOWN}: 1}}
"""


def _blocks() -> list[str]:
    return [m.group(1) for m in YAML_BLOCK.finditer(FORMAT + SKILL)]


def _accepted_keys() -> set[str]:
    _, problems = load_action("probe.yaml", PROBE)
    keys: set[str] = set()
    found = [p for p in problems if p.code == "action.unknown_key"]
    # file, parameter, precondition, example, every op kind, the dynamic unlink
    assert len(found) == 4 + len(OP_KEYS) + 1
    for problem in found:
        expected = problem.details["expected"]
        assert isinstance(expected, list)
        keys.update(str(key) for key in expected)
    return keys


def test_the_skill_has_complete_examples() -> None:
    assert [b for b in _blocks() if b.startswith(f"schema: {ACTION_SCHEMA}\n")]


@pytest.mark.parametrize("block", _blocks())
def test_every_complete_file_compiles(block: str) -> None:
    if not block.startswith(f"schema: {ACTION_SCHEMA}\n"):
        pytest.skip("a fragment")
    name = re.search(r"^name: (\w+)$", block, re.MULTILINE)
    assert name
    document, problems = compile_actions({f"{name.group(1)}.yaml": block}, company_model())
    assert [p.to_json() for p in problems] == []
    assert len(document["actions"]) == 1  # type: ignore[arg-type]


def test_the_reference_names_every_key_and_word() -> None:
    spans = set(CODE_SPAN.findall(FORMAT))
    mentioned = {word for span in spans for word in re.findall(r"[a-z_]+", span)}
    keys = _accepted_keys()
    assert {"schema", "name", "use_when", "replaced_by", "preconditions", "ops", "reason"} <= keys
    assert {"required", "class", "enum", "many", "default", "on", "require", "request"} <= keys
    assert {"edges", "qualifiers", "valid_from", "targets", "alias", "attr", "all"} <= keys
    assert sorted(keys - mentioned) == []
    for word in (*PARAM_TYPES, *STATUSES, *REASONS, *DIRECTIONS):
        assert word in spans or f"`{word}`" in FORMAT, word


def test_the_reference_names_every_limit() -> None:
    assert (MAX_OPS, MAX_PRECONDITIONS, MAX_ACTIONS) == (50, 10, 1000)
    for text in ("50 per action", "10 per action", "1,000 per graph", "at most 8"):
        assert text in FORMAT


def test_the_reference_names_every_publish_code() -> None:
    codes = {c for c in all_error_codes() if c.startswith("action.")} - RUNTIME_CODES
    text = FORMAT + SKILL
    assert sorted(c for c in codes if c not in text) == []


def test_the_format_has_one_home() -> None:
    assert "references/format.md" in SKILL
    for heading in ("## Top-level keys", "## Parameters", "## Ops", "## Preconditions"):
        assert heading in FORMAT
        assert heading not in SKILL
