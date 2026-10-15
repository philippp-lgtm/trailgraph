"""BCP 47 well-formedness and canonical case (Q18 stage 2, E3)."""

import pytest

from graph_cli.kernel.langtag import normalize_language_tag


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
