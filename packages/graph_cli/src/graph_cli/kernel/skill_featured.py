"""What a generated graph skill shows of the published catalogs, as one digest.

A graph skill lists the featured functions and actions and says whether the graph has functions,
actions or views at all; nothing else of the catalogs reaches the render, so a publish that
changes neither leaves every installed copy as current as it was. ``kind_digest`` hashes that
projection for one catalog document, ``catalog_digest`` combines the three kinds.

The digest is of the whole published catalog (not of what one reader may see), so it is the same
for every caller and can be stored once with the compiled catalog when it is published (the
``featured`` key of ``slot.json``). It is derived and judges freshness only: it is no access
control and nothing checks it against the catalog it came from.

A document that says ``withheld`` (a reader may not use the entries at all) has its own digest.
Every featured entry counts, not only the few the lines show, and a featured entry that is
deprecated stays in with its status, so deprecating it changes the digest (it leaves the lines).
The projection keeps whole ``params`` lists and the ``reason`` of an action: better stale too
often than silent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value

LISTED: Final = ("name", "module", "summary", "status", "params")  # a line of the skill
ACTION_EXTRA: Final = ("reason",)  # an action line says whether it needs --reason
LIST_KEYS: Final = {"functions": "functions", "actions": "actions", "views": "views"}


def kind_digest(kind: str, compiled: Mapping[str, JsonValue] | None) -> str:
    """The digest of one catalog document of ``kind`` (``functions``, ``actions`` or ``views``);
    ``None`` (nothing published) has the digest of an empty catalog, which renders alike."""
    entries = compiled.get(LIST_KEYS[kind]) if compiled is not None else None
    found = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    present = any(e.get("status") != "deprecated" for e in found)
    featured: list[JsonValue] = []
    if kind != "views":  # views have no featured lines: only their presence shows
        keys = LISTED + (ACTION_EXTRA if kind == "actions" else ())
        chosen = sorted(
            (e for e in found if e.get("featured") is True),
            key=lambda e: (str(e.get("module") or ""), str(e.get("name"))),
        )
        featured = [_projection(e, keys) for e in chosen]
    shown: dict[str, JsonValue] = {"kind": kind, "present": present, "featured": featured}
    if compiled is not None and compiled.get("withheld") is True:
        shown["withheld"] = True  # a reader who may not use them sees a different line
    return hash_value(shown)


def catalog_digest(functions: str, actions: str, views: str) -> str:
    """The digest of the three kinds together (the values of ``kind_digest``)."""
    return hash_value({"functions": functions, "actions": actions, "views": views})


def _projection(entry: JsonObject, keys: tuple[str, ...]) -> JsonValue:
    return {key: entry.get(key) for key in keys}
