"""Every ``function.*`` code the kinetic package emits is in the error registry (B9)."""

import re
from pathlib import Path

import graph_cli.kinetic as kinetic
from graph_cli.cli_kit.hints import known_hint_codes
from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code

SOURCES = Path(kinetic.__file__).parent
CODE = re.compile(r'"(function\.[a-z_]+)"')


def emitted() -> set[str]:
    return {code for path in SOURCES.glob("*.py") for code in CODE.findall(path.read_text())}


def test_every_emitted_code_is_registered_with_a_hint_entry() -> None:
    codes = emitted()
    assert "function.pattern_impossible" in codes  # positive control for the scan
    missing = sorted(c for c in codes if lookup_error_code(c) is None)
    assert missing == []
    assert sorted(c for c in codes if c not in known_hint_codes()) == []


def test_compile_problems_are_invalid_not_usage() -> None:
    spec = lookup_error_code("function.pattern_impossible")
    assert spec is not None and spec.category is ErrorCategory.INVALID
