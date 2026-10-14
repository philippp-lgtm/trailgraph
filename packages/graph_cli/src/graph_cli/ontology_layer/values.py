"""Value conversion and facet checks per base type.

Used for authored defaults now and for ``attrs_text`` coercion (B3.16) later,
so both follow one table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError

DECIMAL_RE: Final = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
DURATION_RE: Final = re.compile(
    r"^P(?!$)([0-9]+Y)?([0-9]+M)?([0-9]+W)?([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+S)?)?$"
)
_INT_RE: Final = re.compile(r"^[-+]?[0-9]+$")


def coerce_text(base: str, text: str) -> JsonValue:
    """Convert text to the JSON value of a base type; ``ValueError`` if it does not fit."""
    if base in ("string", "text", "iri"):
        return text
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
    """A violation message, or ``None`` when ``value`` satisfies every facet."""
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
