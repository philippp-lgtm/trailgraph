"""Label normalization (Q18 stage 1, D7)."""

import unicodedata

import pytest

from graph_cli.kernel.text import normalize_label


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("abc", "ABC"),
        ("abc", "Abc"),
        ("\uff21\uff22\uff23", "abc"),  # full-width forms (NFKC)
        ("ﬁle", "file"),  # ligature (NFKC)
        (unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "Café")),
        ("Straße", "STRASSE"),  # casefold: ß -> ss
        ("  two   words\t", "two words"),
        ("line\nbreak", "line break"),
    ],
)
def test_equal_keys(a: str, b: str) -> None:
    assert normalize_label(a) == normalize_label(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("café", "cafe"),  # accents are kept by default
        ("e-mail", "email"),  # punctuation and hyphens are kept
        ("a b", "ab"),
    ],
)
def test_distinct_keys(a: str, b: str) -> None:
    assert normalize_label(a) != normalize_label(b)


def test_accent_folding_is_opt_in() -> None:
    assert normalize_label("Café", fold_accents=True) == "cafe"
    assert normalize_label(unicodedata.normalize("NFD", "Ärger"), fold_accents=True) == "arger"
    assert normalize_label("Straße", fold_accents=True) == "strasse"


@pytest.mark.parametrize("text", ["ABC", "Straße", " \uff21\uff22  \uff23 ", "ǅ", "ﬃ", "Ⅻ", "café"])
@pytest.mark.parametrize("fold", [False, True])
def test_idempotent(text: str, fold: bool) -> None:
    once = normalize_label(text, fold_accents=fold)
    assert normalize_label(once, fold_accents=fold) == once
    assert unicodedata.is_normalized("NFKC", once)


def test_blank_text_normalizes_to_empty() -> None:
    assert normalize_label(" \t\n ") == ""
