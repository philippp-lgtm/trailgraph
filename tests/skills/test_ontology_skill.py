"""The hand-written `ontology-best-practices` skill agrees with the ontology layer.

- Every YAML block parses with the strict loader; every complete module compiles
  without findings.
- The format reference names every key the IR accepts and every word of its fixed
  vocabulary. The accepted keys are read from the parser itself: a probe module with an
  unknown key in every mapping makes each ``model.structural.unknown_key`` finding list
  the keys allowed there.
- Structure: frontmatter, size budget, references linked from SKILL.md, ASCII only.

Links and anchors inside the skill are checked by ``tests/test_docs.py``.
"""

import re

import pytest
import yaml
from support.skills import ADMIN_SKILLS_DIR

from graph_cli.kernel.yaml_strict import load_strict
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.vocabulary import (
    ALL_FACETS,
    BASE_TYPES,
    CHARACTERISTICS,
    EDGE_FIELDS,
    IR_FORMAT,
    LABEL_ROLES,
    MAPPING_KINDS,
    MUTABILITY_CLASSES,
    RULE_TIMINGS,
    SEVERITIES,
    SYSTEM_FIELDS,
    TARGET_INACTIVE_POLICIES,
)

SKILL_DIR = ADMIN_SKILLS_DIR / "ontology-best-practices"
FORMAT = SKILL_DIR / "references" / "ir_format.md"

YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
UNKNOWN = "zz_unknown"

# An unknown key in every mapping the parser reads; the rest is just enough to reach them.
PROBE = f"""\
format: {IR_FORMAT}
{UNKNOWN}: 1
module:
  id: probe
  prefix: probe
  version: 1.0.0
  summary: s
  {UNKNOWN}: 1
  imports:
    - {{module: files, version: "^1.0", {UNKNOWN}: 1}}
datatypes:
  D: {{summary: s, base: string, {UNKNOWN}: 1}}
enums:
  E:
    summary: s
    {UNKNOWN}: 1
    values:
      a: {{summary: s, {UNKNOWN}: 1}}
classes:
  C:
    summary: s
    {UNKNOWN}: 1
    attributes:
      a: {{summary: s, type: string, {UNKNOWN}: 1}}
relations:
  r:
    summary: s
    {UNKNOWN}: 1
    cardinality: {{{UNKNOWN}: 1, out: {{min: 0, {UNKNOWN}: 1}}}}
    qualifiers:
      q: {{summary: s, type: string, {UNKNOWN}: 1}}
lifecycles:
  L:
    summary: s
    {UNKNOWN}: 1
    states:
      a: {{summary: s, initial: true, {UNKNOWN}: 1}}
    transitions:
      - from: a
        to: a
        {UNKNOWN}: 1
        guards:
          - id: g
            summary: s
            {UNKNOWN}: 1
            require:
              - {{count: {{rel: r, {UNKNOWN}: 1, where: {{{UNKNOWN}: 1}}}}, minimum_value: 1}}
rules:
  - id: x
    summary: s
    {UNKNOWN}: 1
    severity: error
    when: write
    scope: C
    then:
      - {{attr: a, nonempty: true, {UNKNOWN}: 1}}
      - {{sum: {{rel: r, attr: a, {UNKNOWN}: 1}}, minimum_value: 1}}
      - {{any_of: [{{attr: a, nonempty: true}}], {UNKNOWN}: 1}}
mappings:
  C: {{{UNKNOWN}: [x]}}
"""


def _yaml_blocks() -> list[tuple[str, str]]:
    return [
        (f"{path.relative_to(SKILL_DIR)}[{index}]", match.group(1))
        for path in sorted(SKILL_DIR.rglob("*.md"))
        for index, match in enumerate(YAML_BLOCK.finditer(path.read_text()))
    ]


def _is_module(block: str) -> bool:
    return block.startswith(f"format: {IR_FORMAT}\n")


def _accepted_keys() -> set[str]:
    outcome = compile_bundle("probe", LayeredSource(MappingSource({"probe": PROBE.encode()})))
    unknown = [f for f in outcome.findings if f.code == "model.structural.unknown_key"]
    keys: set[str] = set()
    for finding in unknown:
        assert isinstance(finding.expected, list)
        keys.update(str(key) for key in finding.expected)
    return keys


def _spans(text: str) -> set[str]:
    return set(CODE_SPAN.findall(text))


@pytest.mark.parametrize(("name", "block"), _yaml_blocks())
def test_every_yaml_block_is_strict_yaml(name: str, block: str) -> None:
    load_strict(block)  # raises with the line of any problem


@pytest.mark.parametrize(("name", "block"), [b for b in _yaml_blocks() if _is_module(b[1])])
def test_every_complete_module_compiles_without_findings(name: str, block: str) -> None:
    module_id = yaml.safe_load(block)["module"]["id"]
    outcome = compile_bundle(module_id, LayeredSource(MappingSource({module_id: block.encode()})))
    assert outcome.model is not None
    assert [f.to_json() for f in outcome.findings] == []


def test_the_format_reference_holds_a_complete_module() -> None:
    assert any(_is_module(match.group(1)) for match in YAML_BLOCK.finditer(FORMAT.read_text()))


def test_the_probe_reaches_every_mapping() -> None:
    # Positive control: a probe that stopped early would make the next test vacuous.
    keys = _accepted_keys()
    for key in ("format", "imports", "values", "display_prefix", "mutability", "qualifiers"):
        assert key in keys
    for key in ("out", "min", "initial", "guards", "require", "where", "record_status"):
        assert key in keys
    for key in ("all_of", "equals_string_in", "direction", "exact", "severity", "then"):
        assert key in keys


def test_the_format_reference_names_every_accepted_key() -> None:
    missing = sorted(_accepted_keys() - _spans(FORMAT.read_text()))
    assert missing == [], missing


def test_the_format_reference_names_the_whole_vocabulary() -> None:
    vocabulary = {
        *BASE_TYPES,
        *ALL_FACETS,
        *MUTABILITY_CLASSES,
        *CHARACTERISTICS,
        *TARGET_INACTIVE_POLICIES,
        *LABEL_ROLES,
        *MAPPING_KINDS,
        *SEVERITIES,
        *RULE_TIMINGS,
        *SYSTEM_FIELDS,
        *EDGE_FIELDS,
        "PRESENT",
        "ABSENT",
        "active",
        "superseded",
        "retracted",
        "core",
        "provenance",
        "files",
    }
    missing = sorted(vocabulary - _spans(FORMAT.read_text()))
    assert missing == [], missing


def test_a_missing_key_is_detected() -> None:
    text = FORMAT.read_text().replace("`display_prefix`", "display_prefix")
    assert "display_prefix" in _accepted_keys() - _spans(text)


class TestStructure:
    def _frontmatter(self) -> dict[str, object]:
        text = (SKILL_DIR / "SKILL.md").read_text()
        _, head, _ = text.split("---\n", 2)
        meta = yaml.safe_load(head)
        assert isinstance(meta, dict)
        return meta

    def test_frontmatter(self) -> None:
        meta = self._frontmatter()
        assert set(meta) == {"name", "description"}
        assert meta["name"] == SKILL_DIR.name
        description = meta["description"]
        assert isinstance(description, str) and 200 <= len(description) <= 1024
        assert "<" not in description and ">" not in description

    def test_size_budgets(self) -> None:
        assert len((SKILL_DIR / "SKILL.md").read_text().splitlines()) <= 200
        for path in (SKILL_DIR / "references").glob("*.md"):
            assert len(path.read_bytes()) <= 32 * 1024, path.name

    def test_references_are_linked_from_skill_md(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text()
        linked = {link.partition("#")[0] for link in re.findall(r"\]\((references/[^)]+)\)", text)}
        files = {str(p.relative_to(SKILL_DIR)) for p in (SKILL_DIR / "references").glob("*")}
        assert linked == files

    def test_plain_ascii_without_local_paths(self) -> None:
        for path in SKILL_DIR.rglob("*"):
            if path.is_file():
                text = path.read_text()
                assert text.isascii(), path.name
                assert not re.search(r"(/Users/|/home/|~/)", text), path.name
