"""BCP 47 well-formedness and canonical case (Q18 stage 2, E3)."""

import pytest

from graph_cli.kernel.langtag import (
    language_matches,
    normalize_language_tag,
    split_tagged_text,
    tagged_parts,
    text_of,
)


@pytest.mark.parametrize(
    "tag",
    [
        "en",
        "de-CH",
        "zh-Hant-TW",
        "sl-rozaj-biske",
        "en-u-ca-gregory",
        "de-1996",
        "es-419",
        "zh-yue-HK",
        "ast",
        "en-a-bbb-x-a-ccc",
        "en-x-private",
        "art-lojban",  # regular grandfathered tags follow the grammar
    ],
)
def test_canonical_tags_are_kept(tag: str) -> None:
    assert normalize_language_tag(tag) == tag


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("de-ch", "de-CH"),
        ("EN", "en"),
        ("ZH-HANT-tw", "zh-Hant-TW"),
        ("SL-Rozaj", "sl-rozaj"),
        ("en-U-CA-Gregory", "en-u-ca-gregory"),
        ("en-a-AB-x-AB-ABCD", "en-a-ab-x-ab-abcd"),  # no region or script case after a singleton
    ],
)
def test_case_is_canonicalized(text: str, canonical: str) -> None:
    assert normalize_language_tag(text) == canonical


@pytest.mark.parametrize(
    "text",
    [
        "",
        " en",
        "en ",
        "en\n",
        "de_CH",
        "x-foo",  # bare private use
        "i-klingon",  # grandfathered irregular
        "en-GB-oed",
        "e",
        "toolongtag",
        "en-",
        "-en",
        "en--US",
        "*",
        "en-a-bbb-a-ccc",  # duplicate singleton
        "en-a",
        "en-x",
        "de-CH-abcdefghi",
        "en-\u212aa",  # Kelvin sign folds to "k" without re.ASCII
        "dé",
    ],
)
def test_not_well_formed(text: str) -> None:
    assert normalize_language_tag(text) is None


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ('"Steuerung und Kontrolle"@de', ("Steuerung und Kontrolle", "de")),
        ('"SuK"@DE-ch', ("SuK", "de-CH")),
        ('"a "quoted" word"@en', ('a "quoted" word', "en")),
        ('"user@example"@en', ("user@example", "en")),
        ('"Kiswahili"@art-lojban', ("Kiswahili", "art-lojban")),
        ("SuK@de", ("SuK", "de")),
        ("Name@zh-Hant-TW", ("Name", "zh-Hant-TW")),
        ("SuK", ("SuK", None)),
        ("user@example", ("user@example", None)),  # a 5-8 letter subtag needs quotes
        ("a@b_c", ("a@b_c", None)),
        ("@de", ("@de", None)),
        ("", ("", None)),
        ('""@de', ("", "de")),
    ],
)
def test_split_tagged_text(text: str, parts: tuple[str, str | None]) -> None:
    assert split_tagged_text(text) == parts


@pytest.mark.parametrize("text", ['"SuK"@de_CH', '"SuK"@', '"SuK"@x-foo'])
def test_quoted_form_needs_a_well_formed_tag(text: str) -> None:
    with pytest.raises(ValueError, match="BCP 47"):
        split_tagged_text(text)


def test_tagged_parts_and_text_of() -> None:
    assert tagged_parts({"lang": "de", "value": "SuK"}) == ("SuK", "de")
    assert tagged_parts({"value": "SuK"}) is None
    assert tagged_parts({"lang": "de", "value": 1}) is None
    assert tagged_parts("SuK") is None
    assert text_of({"lang": "de", "value": "SuK"}) == "SuK"
    assert text_of("SuK") == "SuK"
    assert text_of(1) is None


@pytest.mark.parametrize(
    ("tag", "language_range", "expected"),
    [
        ("de", "de", True),
        ("de-CH", "de", True),
        ("de-CH", "DE-ch", True),
        ("de", "de-CH", False),
        ("den", "de", False),
        ("en", "*", True),
    ],
)
def test_language_matches(tag: str, language_range: str, expected: bool) -> None:
    assert language_matches(tag, language_range) is expected
