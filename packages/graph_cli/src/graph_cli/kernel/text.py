"""Normalized match keys for names (label lookup, a future label index).

``normalize_label`` makes "abc", "Abc", "ABC", full-width and composed or
decomposed forms equal: NFKC, ``casefold``, NFKC again (casefolding can
leave text that is not NFKC), whitespace runs collapsed to one space and
stripped. Punctuation and hyphens are kept; hidden labels cover such
variants. ``fold_accents`` additionally drops combining marks (NFKD, remove
``Mn``, NFKC). It is opt-in because folding is language-dependent: German
"ä" is "ae", Swedish "ä" is its own letter.

Keys are never stored in revisions: Unicode tables change between Python
versions, and revisions are content-hashed. They belong only in disposable
indexes, which is why this lives in the kernel (storage imports only the
kernel).
"""

from __future__ import annotations

import unicodedata


def normalize_label(text: str, *, fold_accents: bool = False) -> str:
    key = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    if fold_accents:
        decomposed = unicodedata.normalize("NFKD", key)
        stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        key = unicodedata.normalize("NFKC", stripped)
    return " ".join(key.split())
