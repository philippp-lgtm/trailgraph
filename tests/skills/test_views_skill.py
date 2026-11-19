"""The hand-written `authoring-graph-views` skill agrees with the view format.

- Every complete view file in the skill (a YAML block that starts with the schema line)
  compiles against the example `company` ontology and the example functions without a problem.
- The format reference names every key the parser accepts (read from the parser with probe
  files that have an unknown key in every mapping, in both flavours), every parameter type,
  status, direction and section kind, the limits, and every `view.*` and `admin.views_*` code.
- The format has one home: `SKILL.md` links to the reference and does not repeat the key
  tables.

Links and anchors are checked by ``tests/test_docs.py``, command lines by
``tests/skills/test_admin_skills.py``.
"""

import re

import pytest
from support.skills import ADMIN_SKILLS_DIR
from support.views import company_model, function_entries

from graph_cli.kernel.error_codes import all_error_codes
from graph_cli.kinetic import view_format
from graph_cli.kinetic.format import PARAM_TYPES, STATUSES
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.kinetic.view_format import (
    DIRECTIONS,
    KINDS,
    VIEW_SCHEMA,
    load_view,
)
from graph_cli_admin.services_admin import error_codes as admin_codes

SKILL_DIR = ADMIN_SKILLS_DIR / "authoring-graph-views"
SKILL = (SKILL_DIR / "SKILL.md").read_text()
FORMAT = (SKILL_DIR / "references" / "format.md").read_text()

YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
UNKNOWN = "zz_unknown"

ENTITY_PROBE = f"""\
schema: {VIEW_SCHEMA}
{UNKNOWN}: 1
name: probe
summary: s
use_when: u
takes: c
params:
  p: {{type: string, summary: s, {UNKNOWN}: 1}}
sections:
  - {{kind: fields, {UNKNOWN}: 1}}
  - {{kind: relations, relations: [{{relation: r, {UNKNOWN}: 1}}], {UNKNOWN}: 1}}
  - {{kind: files, {UNKNOWN}: 1}}
  - {{kind: affordances, {UNKNOWN}: 1}}
  - kind: neighbors
    relation: r
    relations:
      - {{relation: r, {UNKNOWN}: 1}}
    {UNKNOWN}: 1
  - {{kind: rows, function: f, {UNKNOWN}: 1}}
examples:
  - {{question: q, {UNKNOWN}: 1}}
"""
SET_PROBE = f"""\
schema: {VIEW_SCHEMA}
name: probe
summary: s
use_when: u
sections:
  - {{kind: list, type: c, {UNKNOWN}: 1}}
  - {{kind: rows, function: f, {UNKNOWN}: 1}}
"""

# What a call, a listing or a release reports; the reference names them in its own sections.
CODES = {c for c in all_error_codes() if c.startswith("view.")}
ADMIN_CODES = {s.code for s in admin_codes.ADMIN_CODES if s.code.startswith("admin.views_")}


def _blocks() -> list[str]:
    return [m.group(1) for m in YAML_BLOCK.finditer(FORMAT + SKILL)]


def _accepted_keys() -> set[str]:
    keys: set[str] = set()
    expected = {"entity": 11, "set": 2}  # unknown keys in every mapping of each probe file
    for flavour, probe in (("entity", ENTITY_PROBE), ("set", SET_PROBE)):
        _, problems = load_view("probe.yaml", probe)
        found = [p for p in problems if p.code == "view.unknown_key"]
        assert len(found) == expected[flavour], [p.to_json() for p in found]
        for problem in found:
            listed = problem.details["expected"]
            assert isinstance(listed, list)
            keys.update(str(key) for key in listed)
    return keys


def test_the_skill_has_complete_examples() -> None:
    blocks = [b for b in _blocks() if b.startswith(f"schema: {VIEW_SCHEMA}\n")]
    assert any("takes:" in b for b in blocks) and any("takes:" not in b for b in blocks)


@pytest.mark.parametrize("block", _blocks())
def test_every_complete_file_compiles(block: str) -> None:
    if not block.startswith(f"schema: {VIEW_SCHEMA}\n"):
        pytest.skip("a fragment")
    name = re.search(r"^name: (\w+)$", block, re.MULTILINE)
    assert name
    model = company_model()
    document, problems = compile_views({f"{name.group(1)}.yaml": block}, model, function_entries())
    assert [p.to_json() for p in problems] == []
    assert len(document["views"]) == 1  # type: ignore[arg-type]


def test_the_reference_names_every_key_and_word() -> None:
    spans = set(CODE_SPAN.findall(FORMAT))
    mentioned = {word for span in spans for word in re.findall(r"[a-z_]+", span)}
    keys = _accepted_keys()
    assert {"schema", "name", "use_when", "replaced_by", "takes", "default", "params"} <= keys
    assert {"max_bytes", "page_bytes", "sections", "examples", "id", "title"} <= keys
    assert {"value_chars", "max_groups", "max_items", "function", "args", "relation"} <= keys
    assert sorted(keys - mentioned) == []
    for word in (*PARAM_TYPES, *STATUSES, *DIRECTIONS, *KINDS, "all"):
        assert word in spans or f"`{word}`" in FORMAT, word


def test_the_reference_names_every_limit() -> None:
    assert (view_format.MAX_SECTIONS, view_format.ENTITY_MAX_BYTES) == (32, 6144)
    assert (view_format.PAGE_BYTES_MAX, view_format.MIN_BYTES) == (262_144, 1024)
    assert (view_format.VALUE_CHARS_MAX, view_format.FIELDS_MAX_MAX) == (2000, 32)
    assert (view_format.NEIGHBORS_MAX_MAX, view_format.ROWS_MAX_MAX) == (50, 50)
    assert (view_format.ROW_RELATION_MAX_MAX, view_format.MAX_TYPES) == (500, 16)
    for text in (
        "1 to 32 sections",
        "1024 to 6144",
        "1024 to 262144",
        "at most 2,000",
        "at most 32",
        "at most 50",
        "at most 500",
        "up to 16",
        "1,000 per graph",
        "6,144 bytes",
        "12,288",
        "262,144",
        "20,000",
        "100,000",
    ):
        assert text in FORMAT, text


def test_the_reference_names_every_code() -> None:
    text = FORMAT + SKILL
    assert CODES and ADMIN_CODES
    assert sorted(c for c in CODES | ADMIN_CODES if c not in text) == []


def test_the_format_has_one_home() -> None:
    assert "references/format.md" in SKILL
    for heading in (
        "## Top-level keys",
        "## Parameters",
        "## Sections",
        "## What publishing checks",
    ):
        assert heading in FORMAT
        assert heading not in SKILL
