"""Normalized match keys for names and text (label lookup, search, a future index).

``normalize_label`` makes "abc", "Abc", "ABC", full-width and composed or
decomposed forms equal: NFKC, ``casefold``, NFKC again (casefolding can
leave text that is not NFKC), whitespace runs collapsed to one space and
stripped. Punctuation and hyphens are kept; hidden labels cover such
variants. ``fold_accents`` additionally drops combining marks (NFKD, remove
``Mn``, NFKC). It is opt-in because folding is language-dependent: German
"ä" is "ae", Swedish "ä" is its own letter.

``search_key`` is the looser key of ``graph search``: accents always
folded, every character that is not a letter or digit a space. Its
``trigrams`` are padded per word as in PostgreSQL ``pg_trgm`` (two spaces
before, one after), so a word's start and end count; ``containment`` is the
share of a query's trigrams found in a value's.

Keys are never stored in revisions: Unicode tables change between Python
versions, and revisions are content-hashed. They belong only in disposable
indexes, which is why this lives in the kernel (storage imports only the
kernel).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Set

_NOT_ALNUM = re.compile(r"[\W_]+")


def normalize_label(text: str, *, fold_accents: bool = False) -> str:
    key = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    if fold_accents:
        decomposed = unicodedata.normalize("NFKD", key)
        if not decomposed.isascii():
            decomposed = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        key = unicodedata.normalize("NFKC", decomposed)
    return " ".join(key.split())


def search_key(text: str) -> str:
    return " ".join(_NOT_ALNUM.sub(" ", normalize_label(text, fold_accents=True)).split())


def trigrams(key: str) -> frozenset[str]:
    found: set[str] = set()
    for word in key.split():
        padded = f"  {word} "
        found.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(found)


def containment(query: Set[str], value: Set[str]) -> float:
    return len(query & value) / len(query) if query else 0.0
