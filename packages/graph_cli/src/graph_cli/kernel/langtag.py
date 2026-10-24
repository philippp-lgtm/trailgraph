"""BCP 47 language tags: well-formedness and canonical case (RFC 5646).

``normalize_language_tag`` accepts a tag that matches the RFC 5646
``langtag`` grammar (2.1) and returns it in canonical case (2.1.1):
language and extension subtags lower, script title, region upper. It
returns None for anything else: empty text, ``_`` separators, a bare
private-use tag (``x-foo``), grandfathered irregular tags (``i-klingon``),
subtags over 8 characters, a repeated extension singleton. Only
well-formedness is checked, not the IANA registry, so no data file goes
stale and no dependency is needed.

Used for the graph default language (Q18 stage 2) and for the tags of
language-tagged values (Q18 stage 3, base types ``lang_string`` and
``lang_text``): a value ``{"lang": <tag>, "value": <text>}`` as RDF
``rdf:langString``. ``tagged_parts`` and ``text_of`` read one,
``split_tagged_text`` reads the text form ``"text"@tag`` or ``text@tag``,
``language_matches`` is RFC 4647 basic filtering. These live in the kernel
because the query evaluator compares such values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue

LANG_KEYS: Final = frozenset({"lang", "value"})

_LANGTAG: Final = re.compile(
    r"""
    (?: [a-z]{2,3} (?:-[a-z]{3}){0,3} | [a-z]{4,8} )    # language, extlang
    (?: -[a-z]{4} )?                                    # script
    (?: -(?:[a-z]{2}|[0-9]{3}) )?                       # region
    (?: -(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}) )*          # variants
    (?: -[0-9a-wyz] (?:-[a-z0-9]{2,8})+ )*              # extensions
    (?: -x (?:-[a-z0-9]{1,8})+ )?                       # private use
    """,
    re.VERBOSE | re.IGNORECASE | re.ASCII,
)


def normalize_language_tag(text: str) -> str | None:
    """``text`` in canonical case, or None when it is not a well-formed tag."""
    if not _LANGTAG.fullmatch(text):
        return None
    subtags = text.lower().split("-")
    out = [subtags[0]]
    singletons: set[str] = set()
    for i, subtag in enumerate(subtags[1:], 1):
        if subtag == "x":  # private use: everything after it stays lower
            out += subtags[i:]
            break
        if len(subtag) == 1:
            if subtag in singletons:
                return None
            singletons.add(subtag)
        elif not singletons and subtag.isalpha() and len(subtag) == 2:
            subtag = subtag.upper()  # region
        elif not singletons and subtag.isalpha() and len(subtag) == 4:
            subtag = subtag.title()  # script
        out.append(subtag)
    return "-".join(out)


# The text form: Turtle's "text"@tag (any tag), or text@tag with a 2-3 letter
# language subtag (every ISO 639 code), so "name@example" stays text.
_QUOTED: Final = re.compile(r'"(.*)"@([^"@]*)', re.DOTALL)
_SUFFIX: Final = re.compile(r"(.+)@([A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*)", re.DOTALL)


def tagged_parts(value: JsonValue) -> tuple[str, str] | None:
    """``(text, tag)`` of a language-tagged value, or None for anything else."""
    if isinstance(value, Mapping) and value.keys() == LANG_KEYS:
        text, tag = value["value"], value["lang"]
        if isinstance(text, str) and isinstance(tag, str):
            return text, tag
    return None


def text_of(value: JsonValue) -> str | None:
    """The text of a plain string or of a language-tagged value, else None."""
    if isinstance(value, str):
        return value
    parts = tagged_parts(value)
    return None if parts is None else parts[0]


def split_tagged_text(text: str) -> tuple[str, str | None]:
    """``(text, canonical tag)`` of the text form; ``(text, None)`` when untagged.

    ``ValueError`` when the quoted form names a tag that is not well-formed. An
    unquoted suffix that is not a well-formed tag is part of the text.
    """
    quoted = _QUOTED.fullmatch(text)
    if quoted is not None:
        tag = normalize_language_tag(quoted[2])
        if tag is None:
            raise ValueError(f"{quoted[2]!r} is not a well-formed BCP 47 language tag")
        return quoted[1], tag
    suffix = _SUFFIX.fullmatch(text)
    if suffix is not None and (tag := normalize_language_tag(suffix[2])) is not None:
        return suffix[1], tag
    return text, None


def language_matches(tag: str, language_range: str) -> bool:
    """RFC 4647 basic filtering: ``*`` matches every tag, ``de`` matches ``de`` and
    ``de-CH``; case-insensitive."""
    if language_range == "*":
        return True
    tag, language_range = tag.lower(), language_range.lower()
    return tag == language_range or tag.startswith(language_range + "-")
