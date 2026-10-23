"""Label normalization (Q18 stage 1, D7)."""

import unicodedata

import pytest

from graph_cli.kernel.text import containment, normalize_label, search_key, trigrams


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


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Light- and Refraction", "light and refraction"),
        ("Café", "CAFE"),  # accents always folded
        ("e-mail", "e mail"),
        ("snake_case", "snake case"),
        ("  a.b,c  ", "a b c"),
    ],
)
def test_equal_search_keys(a: str, b: str) -> None:
    assert search_key(a) == search_key(b)


def test_search_key_keeps_letters_and_digits_of_any_script() -> None:
    assert search_key("Straße 12") == "strasse 12"
    assert search_key("Ελλάδα") == "ελλαδα"
    assert search_key("-- !") == ""


def test_trigrams_are_padded_per_word() -> None:
    assert trigrams("ab") == {"  a", " ab", "ab "}
    assert trigrams("ab ab") == trigrams("ab")
    assert trigrams("") == frozenset()


def test_containment_is_the_share_of_query_trigrams() -> None:
    query = trigrams(search_key("Lightrefraction"))
    assert containment(query, query) == 1.0
    assert containment(query, trigrams(search_key("Light and refraction"))) == 14 / 16
    assert containment(query, trigrams("optics")) == 0.0
    assert containment(frozenset(), query) == 0.0
