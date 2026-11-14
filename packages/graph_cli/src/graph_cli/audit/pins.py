"""The versions a graph was pinned to, from its audit stream (migration plan M7).

The graph stream starts with ``maint.graph_create`` (the genesis pin in ``graph.ontology``)
and records every pin switch as an ``ok`` ``maint.ontology_pin`` intent with
``change.pin{from, to}``; ``from`` is read under the graph lock, so the intents chain
(``audit.verify`` walks them). Read-time mapping composes the renames of consecutive pinned
versions, so it needs this chain and nothing else: ``diff.json`` of a release is relative to
the pin at install time, not to the version before it in the chain.

The lines are filtered by their bytes before they are parsed: most of a graph stream is
write intents, and only the two actions above matter here. The stream is not verified (that
is ``audit verify``'s job); a line that does not parse is skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from graph_cli.audit.schema import PIN_ACTION
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json

CREATE_ACTION: Final = "maint.graph_create"
_NEEDLES: Final = (f'"{PIN_ACTION}"'.encode(), f'"{CREATE_ACTION}"'.encode())


def pinned_versions(lines: Iterable[bytes]) -> list[str]:
    """The pinned versions in stream order, consecutive repeats collapsed: the genesis pin,
    then the ``to`` of every ``ok`` pin intent."""
    found: list[str] = []
    for line in lines:
        if not any(needle in line for needle in _NEEDLES):
            continue
        try:
            event = parse_json(line)
        except CanonicalError:
            continue
        version = _pinned(event) if isinstance(event, Mapping) else None
        if version is not None and (not found or found[-1] != version):
            found.append(version)
    return found


def _pinned(event: Mapping[str, JsonValue]) -> str | None:
    result = event.get("result")
    if not isinstance(result, Mapping) or result.get("status") != "ok":
        return None
    actions = event.get("actions")
    if not isinstance(actions, list):
        return None
    if PIN_ACTION in actions:
        change = event.get("change")
        pin = change.get("pin") if isinstance(change, Mapping) else None
        target = pin.get("to") if isinstance(pin, Mapping) else None
    elif actions == [CREATE_ACTION]:
        graph = event.get("graph")
        target = graph.get("ontology") if isinstance(graph, Mapping) else None
    else:
        return None
    version = target.get("version") if isinstance(target, Mapping) else None
    return version if isinstance(version, str) else None
