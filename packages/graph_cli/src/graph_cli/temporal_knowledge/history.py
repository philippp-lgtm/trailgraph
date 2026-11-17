"""What changed between two revisions of one entity, by name only (`graph history`).

Names, never values: values come from reading a revision, so a history list
stays short and never echoes a hidden label.
"""

from __future__ import annotations

from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import EdgeRecord, Revision

_RECORD_FIELDS = ("type", "record_status", "alias", "valid_from", "valid_to")


@dataclass(frozen=True, slots=True)
class EdgeChanges:
    """The edges a revision added, removed or changed against the one before.

    ``changed`` holds ``(old, new)`` pairs of one edge id whose record differs
    (a moved target, other qualifiers)."""

    added: tuple[EdgeRecord, ...]
    removed: tuple[EdgeRecord, ...]
    changed: tuple[tuple[EdgeRecord, EdgeRecord], ...]


def edge_changes(before: Revision | None, after: Revision) -> EdgeChanges:
    """The edge differences of ``after`` against ``before``; every edge is added for a creation
    (``before`` is ``None``). Ordered by edge id, so the result is deterministic."""
    old = {edge.edge_id: edge for edge in before.edges} if before is not None else {}
    new = {edge.edge_id: edge for edge in after.edges}
    return EdgeChanges(
        added=tuple(new[e] for e in sorted(new.keys() - old.keys())),
        removed=tuple(old[e] for e in sorted(old.keys() - new.keys())),
        changed=tuple(
            (old[e], new[e])
            for e in sorted(new.keys() & old.keys())
            if new[e].to_json() != old[e].to_json()
        ),
    )


def changed_names(before: Revision, after: Revision) -> dict[str, JsonValue]:
    """``{attrs, edges{added, removed, changed}, record}``: sorted, unique names."""
    attrs = sorted(
        name
        for name in before.attrs.keys() | after.attrs.keys()
        if before.attrs.get(name) != after.attrs.get(name)
    )
    edges = edge_changes(before, after)
    added = sorted({edge.rel for edge in edges.added})
    removed = sorted({edge.rel for edge in edges.removed})
    changed = sorted({new.rel for _, new in edges.changed})
    record = [name for name in _RECORD_FIELDS if getattr(before, name) != getattr(after, name)]
    return {
        "attrs": list(attrs),
        "edges": {"added": list(added), "removed": list(removed), "changed": list(changed)},
        "record": list(record),
    }
