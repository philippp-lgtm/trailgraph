"""BCP 47 language tags: well-formedness and canonical case (RFC 5646).

``normalize_language_tag`` accepts a tag that matches the RFC 5646
``langtag`` grammar (2.1) and returns it in canonical case (2.1.1):
language and extension subtags lower, script title, region upper. It
returns None for anything else: empty text, ``_`` separators, a bare
private-use tag (``x-foo``), grandfathered irregular tags (``i-klingon``),
subtags over 8 characters, a repeated extension singleton. Only
well-formedness is checked, not the IANA registry, so no data file goes
stale and no dependency is needed.

Used for the graph default language (Q18 stage 2); per-value tags of a
later ``lang_string`` reuse it, which is why it lives in the kernel.
"""

from __future__ import annotations

import re
from typing import Final

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
