"""What changed between two revisions of one entity, by name only (`graph history`).

Names, never values: values come from reading a revision, so a history list
stays short and never echoes a hidden label.
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import Revision

_RECORD_FIELDS = ("type", "record_status", "alias", "valid_from", "valid_to")


def changed_names(before: Revision, after: Revision) -> dict[str, JsonValue]:
    """``{attrs, edges{added, removed, changed}, record}``: sorted, unique names."""
    attrs = sorted(
        name
        for name in before.attrs.keys() | after.attrs.keys()
        if before.attrs.get(name) != after.attrs.get(name)
    )
    old = {edge.edge_id: edge for edge in before.edges}
    new = {edge.edge_id: edge for edge in after.edges}
    added = sorted({new[e].rel for e in new.keys() - old.keys()})
    removed = sorted({old[e].rel for e in old.keys() - new.keys()})
    changed = sorted(
        {new[e].rel for e in new.keys() & old.keys() if new[e].to_json() != old[e].to_json()}
    )
    record = [name for name in _RECORD_FIELDS if getattr(before, name) != getattr(after, name)]
    return {
        "attrs": list(attrs),
        "edges": {"added": list(added), "removed": list(removed), "changed": list(changed)},
        "record": list(record),
    }
