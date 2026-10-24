"""Value conversion and facet checks per base type.

Used for authored defaults and for ``attrs_text`` coercion (B3.16), so both
follow one table.

Language-tagged text (``lang_string``, ``lang_text``) is ``{"lang", "value"}``
with a well-formed tag in canonical case. Its text form is ``"text"@tag`` or
``text@tag`` (``kernel.langtag.split_tagged_text``); an untagged text stays a
plain string, which ``with_language`` tags with the graph language on write.
Facets apply to the text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import HASH_RE, MAX_SAFE_INT, JsonValue
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.langtag import (
    LANG_KEYS,
    normalize_language_tag,
    split_tagged_text,
    tagged_parts,
)
from graph_cli.ontology_layer.vocabulary import FILE_ROOT_RE, LANG_BASES

DECIMAL_RE: Final = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
DURATION_RE: Final = re.compile(
    r"^P(?!$)([0-9]+Y)?([0-9]+M)?([0-9]+W)?([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+S)?)?$"
)
_INT_RE: Final = re.compile(r"^[-+]?[0-9]+$")
MAX_LOCAL_PATH_BYTES: Final = 4096


def coerce_text(base: str, text: str) -> JsonValue:
    """Convert text to the JSON value of a base type; ``ValueError`` if it does not fit."""
    if base in ("string", "text", "iri", "blob"):
        return text
    if base in LANG_BASES:
        value, tag = split_tagged_text(text)
        return value if tag is None else {"lang": tag, "value": value}
    if base == "integer":
        if not _INT_RE.match(text) or abs(int(text)) > MAX_SAFE_INT:
            raise ValueError(f"{text!r} is not an integer")
        return int(text)
    if base == "boolean":
        if text not in ("true", "false"):
            raise ValueError(f"{text!r} is not true or false")
        return text == "true"
    if base == "decimal":
        if not DECIMAL_RE.match(text):
            raise ValueError(f"{text!r} is not a decimal")
        return text
    try:
        if base == "date":
            return parse_date(text).isoformat()
        if base == "datetime":
            return format_timestamp(parse_timestamp(text))
    except GraphCliError as exc:
        raise ValueError(exc.message) from None
    if base == "duration":
        if not DURATION_RE.match(text):
            raise ValueError(f"{text!r} is not an ISO 8601 duration")
        return text
    raise ValueError(f"values of type {base!r} cannot be given as text")


def check_facets(value: JsonValue, facets: Mapping[str, JsonValue]) -> str | None:
    """A violation message, or ``None`` when ``value`` satisfies every facet.

    A language-tagged value is judged by its text."""
    parts = tagged_parts(value)
    if parts is not None:
        value = parts[0]
    if isinstance(value, str):
        minimum, maximum = facets.get("min_length"), facets.get("max_length")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"shorter than {minimum} characters"
        if isinstance(maximum, int) and len(value) > maximum:
            return f"longer than {maximum} characters"
        pattern = facets.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            return f"does not match {pattern}"
        if facets.get("single_line") is True and ("\n" in value or "\r" in value):
            return "contains a line break"
    elif isinstance(value, int) and not isinstance(value, bool):
        minimum, maximum = facets.get("min"), facets.get("max")
        if isinstance(minimum, int) and value < minimum:
            return f"less than {minimum}"
        if isinstance(maximum, int) and value > maximum:
            return f"greater than {maximum}"
    return None


def lang_value_problem(value: JsonValue) -> str | None:
    """A shape problem of a language-tagged value, or ``None``.

    An untagged value reaches the validator only when the graph has no language
    (``with_language`` tags it otherwise), so the message names both ways out.
    """
    if isinstance(value, str) or (isinstance(value, Mapping) and set(value) == {"value"}):
        return (
            "needs a language tag and the graph has no default language:"
            ' write "<text>"@<tag> or {"lang": <tag>, "value": <text>}'
        )
    if not isinstance(value, Mapping) or value.keys() != LANG_KEYS:
        return 'expected {"lang": <BCP 47 tag>, "value": <text>}'
    text, tag = value["value"], value["lang"]
    if not isinstance(text, str):
        return "value must be a string"
    canonical = normalize_language_tag(tag) if isinstance(tag, str) else None
    if canonical is None:
        return f"lang {tag!r} is not a well-formed BCP 47 language tag"
    if canonical != tag:
        return f"lang {tag!r} is not in canonical case ({canonical})"
    return None


def with_language(value: JsonValue, language: str | None) -> JsonValue:
    """A value of a language-tagged attribute as stored: a plain string, or an object
    without ``lang``, gets ``language`` (when set); a well-formed ``lang`` gets canonical
    case. Anything else is returned unchanged, for the validator to judge."""
    if isinstance(value, str):
        return value if language is None else {"lang": language, "value": value}
    if not isinstance(value, Mapping):
        return value
    if set(value) == {"value"}:
        return dict(value) if language is None else {"lang": language, "value": value["value"]}
    tag = value.get("lang")
    if value.keys() == LANG_KEYS and isinstance(tag, str):
        canonical = normalize_language_tag(tag)
        if canonical is not None and canonical != tag:
            return {"lang": canonical, "value": value["value"]}
    return value


def blob_problem(value: JsonValue) -> str | None:
    """A shape problem of a ``blob`` value (C.2 hash string), or ``None``."""
    ok = isinstance(value, str) and HASH_RE.match(value) is not None
    return None if ok else 'expected "sha256:" and 64 lowercase hex digits'


def local_path_problem(value: JsonValue) -> str | None:
    """A shape problem of a ``local_path`` value ``{root, path}``, or ``None``.

    ``path`` is relative POSIX: ``/``-separated segments that are non-empty and
    neither ``.`` nor ``..``; no backslash, no NUL, at most 4096 bytes UTF-8.
    The value is taken as received (C.5, no Unicode normalization).
    """
    if not isinstance(value, Mapping) or set(value) != {"root", "path"}:
        return 'expected {"root": <name>, "path": <relative path>}'
    root, path = value["root"], value["path"]
    if not isinstance(root, str) or FILE_ROOT_RE.match(root) is None:
        return f"root must match {FILE_ROOT_RE.pattern}"
    if not isinstance(path, str) or not path:
        return "path must be a non-empty string"
    if "\\" in path or "\x00" in path:
        return "path must not contain a backslash or NUL"
    if len(path.encode("utf-8", "surrogatepass")) > MAX_LOCAL_PATH_BYTES:
        return f"path is longer than {MAX_LOCAL_PATH_BYTES} bytes"
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        return "path must be relative, without empty, '.' or '..' segments"
    return None
