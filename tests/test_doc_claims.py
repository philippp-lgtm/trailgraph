"""Every claim of the documentation has a test, and every claim a test names exists.

A guarantee written in prose that no parsing check reaches (CONTRIBUTING.md "Documentation")
is marked in its document with ``<!-- claim: <doc stem>.<name> -->`` at the end of its
sentence or list item, and the tests that check it carry
``@pytest.mark.doc_claim("<doc stem>.<name>", ...)`` (``support.doc_claims``, which also
names the claims in the report of a failing test).

- The documents are those of docs/; their claim markers are read outside fenced blocks.
- The tests are read as source (AST), not collected: a marker counts on a test function
  (``test_*``) or a test class (``Test*``) of ``tests/``, with string literals as its
  arguments. A marker anywhere else (a helper, ``pytestmark``) fails, since it would not
  say which test is responsible.
- Every claim has at least one test, every claim a test names is in a document, every id
  is written once across the documents, has the shape ``<stem>.<name>`` (lowercase, ``_``)
  and its stem is the stem of the document that holds it.

A marker only says which test is responsible for a sentence; it does not prove that the
test checks every part of it. Each scanner has a positive control.
"""

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from harness import REPO_ROOT
from support.doc_claims import MARKER, where

TESTS_DIR = REPO_ROOT / "tests"
CLAIM = re.compile(r"<!--\s*claim:\s*(\S*)\s*-->")
CLAIM_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
# A fenced block, also one indented in a list item.
FENCED_BLOCK = re.compile(r"^( *)```.*?^\1```", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class MarkedTest:
    claim: str
    where: str  # "<path>::<test>"


def _documents() -> list[Path]:
    return sorted((REPO_ROOT / "docs").glob("*.md"))


def _doc_claims(text: str) -> list[str]:
    """The claim ids of a document's markers, in order."""
    return CLAIM.findall(FENCED_BLOCK.sub("", text))


def _is_marker(node: ast.expr) -> bool:
    """Whether ``node`` is a call of ``<...>.mark.doc_claim``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == MARKER
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "mark"
    )


def _test_claims(source: str, path: str) -> tuple[list[MarkedTest], list[str]]:
    """The claims the markers of a test module name, and the problems: markers outside a
    test, arguments that are no string literal, markers without an argument."""
    found: list[MarkedTest] = []
    problems: list[str] = []
    placed: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        is_test = node.name.startswith("Test" if isinstance(node, ast.ClassDef) else "test_")
        for decorator in node.decorator_list:
            if not _is_marker(decorator):
                continue
            assert isinstance(decorator, ast.Call)
            placed.add(id(decorator))
            site = f"{path}::{node.name}"
            if not is_test:
                problems.append(f"{site}: the marker is not on a test")
                continue
            if not decorator.args or decorator.keywords:
                problems.append(f"{site}: the marker names no claim id")
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(MarkedTest(arg.value, site))
                else:
                    problems.append(f"{site}: a claim id is not a string literal")
    for node in ast.walk(tree):
        if isinstance(node, ast.expr) and _is_marker(node) and id(node) not in placed:
            problems.append(f"{path}:{node.lineno}: the marker is not on a test")
    return found, problems


def _all_doc_claims() -> dict[str, list[str]]:
    """Claim id -> the documents (relative paths) that write it."""
    found: dict[str, list[str]] = defaultdict(list)
    for path in _documents():
        for claim in _doc_claims(path.read_text()):
            found[claim].append(str(path.relative_to(REPO_ROOT)))
    return found


def _all_test_claims() -> tuple[list[MarkedTest], list[str]]:
    found: list[MarkedTest] = []
    problems: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        source = path.read_text()
        if MARKER not in source:
            continue
        claims, issues = _test_claims(source, str(path.relative_to(TESTS_DIR)))
        found += claims
        problems += issues
    return found, problems


# ---- positive controls ---------------------------------------------------------------------


def test_the_document_scan_reads_markers_outside_fenced_blocks() -> None:
    text = (
        "A promise. <!-- claim: doc.first -->\n"
        "- an item <!--claim:doc.second-->\n"
        "```text\n<!-- claim: doc.in_a_block -->\n```\n"
        "  ```html\n  <!-- claim: doc.indented_block -->\n  ```\n"
        "<!-- claim: -->\n"
    )
    assert _doc_claims(text) == ["doc.first", "doc.second", ""]


def test_the_test_scan_finds_markers_on_tests_and_flags_the_rest() -> None:
    source = """
import pytest

pytestmark = pytest.mark.doc_claim("doc.module")


@pytest.mark.doc_claim("doc.one", "doc.two")
@pytest.mark.parametrize("x", [1])
def test_one(x): ...


@pytest.mark.doc_claim("doc.three")
class TestGroup:
    @pytest.mark.doc_claim("doc.four")
    def test_inner(self): ...


@pytest.mark.doc_claim("doc.helper")
def helper(): ...


@pytest.mark.doc_claim(NAME)
def test_named(): ...


@pytest.mark.doc_claim()
async def test_empty(): ...
"""
    found, problems = _test_claims(source, "m.py")
    assert found == [
        MarkedTest("doc.one", "m.py::test_one"),
        MarkedTest("doc.two", "m.py::test_one"),
        MarkedTest("doc.three", "m.py::TestGroup"),
        MarkedTest("doc.four", "m.py::test_inner"),
    ]
    assert sorted(problems) == [
        "m.py:4: the marker is not on a test",
        "m.py::helper: the marker is not on a test",
        "m.py::test_empty: the marker names no claim id",
        "m.py::test_named: a claim id is not a string literal",
    ]


def test_the_scans_find_the_claims_of_the_security_model() -> None:
    claims = _all_doc_claims()
    assert sum(c.startswith("security_model.") for c in claims) >= 40
    tests, _ = _all_test_claims()
    assert len({t.where for t in tests}) >= 100


# ---- the checks ----------------------------------------------------------------------------


def test_every_marker_is_on_a_test_and_names_literal_ids() -> None:
    _, problems = _all_test_claims()
    assert problems == []


def test_every_claim_id_is_well_formed_and_held_by_its_document_once() -> None:
    problems = []
    for claim, docs in sorted(_all_doc_claims().items()):
        if not CLAIM_ID.fullmatch(claim):
            problems.append(f"{claim!r} in {docs}: not <doc stem>.<name>")
        elif docs != [where(claim)]:
            problems.append(f"{claim} is written in {docs}, expected once in {where(claim)}")
    assert problems == []


def test_every_claim_of_a_document_has_a_test() -> None:
    tested = {t.claim for t in _all_test_claims()[0]}
    untested = sorted(set(_all_doc_claims()) - tested)
    assert untested == [], "claims without a test (mark one with @pytest.mark.doc_claim)"


def test_every_claim_a_test_names_is_in_a_document() -> None:
    written = set(_all_doc_claims())
    unknown = sorted(
        f"{t.where}: {t.claim}" for t in _all_test_claims()[0] if t.claim not in written
    )
    assert unknown == []
