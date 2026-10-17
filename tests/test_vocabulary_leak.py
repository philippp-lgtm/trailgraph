"""The core is domain-neutral: no fixture vocabulary appears in the sources (B7.3).

The deny-list is generated from the fixture ontologies: CURIE prefixes, type
names (classes, datatypes, enums, lifecycles) and every compound snake_case
name (relations, attributes, enum values, states with an underscore). Plain
English single words (``about``, ``name``, ``draft``) are excluded to avoid
false positives. Python sources are scanned as identifiers and non-docstring
string constants; other source files as text. Names the standard modules
define (``core``, ``provenance``, ``files``) are package data and generic; they
are not denied even when a fixture uses them too.
"""

import ast
import re

from harness import REPO_ROOT
from support.fixtures import TOY_ONTOLOGIES, compiled_model, ontology_versions

from graph_cli.ontology_layer.bundle import LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle

BUILTIN_MODULES = ("core", "provenance", "files")
SRC_ROOTS = [REPO_ROOT / "packages" / name / "src" for name in ("graph_cli", "graph_cli_admin")]


def builtin_names() -> set[str]:
    """Names the standard modules define: package data and generic, never fixture vocabulary."""
    names: set[str] = set()
    for module_id in BUILTIN_MODULES:
        model = compile_bundle(module_id, LayeredSource()).model
        assert model is not None
        for curie in model.term_curies():
            names.add(curie.partition(":")[2])
            if curie in model.classes:
                names.update(model.classes[curie].attributes)
    return names


def deny_list() -> tuple[set[str], set[str]]:
    """``(words, prefixes)`` taken from the fixture modules themselves."""
    words: set[str] = set()
    prefixes: set[str] = set()
    for name in TOY_ONTOLOGIES:
        for version in ontology_versions(name):
            model = compiled_model(name, version)
            (module,) = [m for m in model.modules if m.id == name]
            prefixes.add(module.prefix)
            own = [c for c in model.term_curies() if c.startswith(module.prefix + ":")]
            names = {curie.partition(":")[2] for curie in own}
            for curie in own:
                if curie in model.classes:
                    names.update(
                        attr
                        for attr, definition in model.classes[curie].attributes.items()
                        if definition.declared_in in own
                    )
                if curie in model.enums:
                    names.update(model.enums[curie].value_names())
                if curie in model.lifecycles:
                    names.update(model.lifecycles[curie].state_names())
            words.update(n for n in names if n[0].isupper() or "_" in n)
    return words - builtin_names(), prefixes


def _python_tokens(tree: ast.Module) -> list[str]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef):
            tokens.append(node.name)
        elif isinstance(node, ast.arg):
            tokens.append(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            tokens.append(node.value)
    return tokens


def find_leaks(words: set[str], prefixes: set[str]) -> dict[str, list[str]]:
    pattern = re.compile(
        r"\b(" + "|".join(sorted(map(re.escape, words))) + r")\b"
        r"|\b(" + "|".join(sorted(map(re.escape, prefixes))) + r"):[A-Za-z]"
    )
    leaks: dict[str, list[str]] = {}
    for root in SRC_ROOTS:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if path.suffix == ".pyc":
                continue
            if path.suffix == ".py":
                texts = _python_tokens(ast.parse(path.read_text()))
            else:
                texts = [path.read_text()]
            hits = sorted({m.group(0) for text in texts for m in pattern.finditer(text)})
            if hits:
                leaks[str(path.relative_to(REPO_ROOT))] = hits
    return leaks


def test_deny_list_is_meaningful() -> None:
    words, prefixes = deny_list()
    assert {"Item", "WebPage", "located_in", "in_stock", "serial_number"} <= words
    assert prefixes == {"nas", "inv"}
    assert "about" not in words  # plain single words are excluded
    assert "retrieved_at" not in words  # also a term of the standard module files


def test_no_fixture_vocabulary_in_sources() -> None:
    assert find_leaks(*deny_list()) == {}


def test_positive_control() -> None:
    tree = ast.parse('"""Item docstring is fine."""\nx = "inv:Item"\nlocated_in = 1\n')
    tokens = _python_tokens(tree)
    assert "Item docstring is fine." not in tokens
    assert {"inv:Item", "located_in"} <= set(tokens)
