"""Changed names between two revisions (contracts B8.15)."""

from dataclasses import replace

from support.snapshots import edge, eid, revision

from graph_cli.temporal_knowledge.history import changed_names


def test_nothing_changed_gives_empty_lists() -> None:
    before = revision("a", "ex:Thing", attrs={"title": "x"})
    assert changed_names(before, before) == {
        "attrs": [],
        "edges": {"added": [], "removed": [], "changed": []},
        "record": [],
    }


def test_attrs_by_name_added_removed_and_changed() -> None:
    before = revision("a", "ex:Thing", attrs={"title": "x", "gone": 1, "same": True})
    after = replace(before, rev=2, attrs={"title": "y", "same": True, "new": [1]})
    assert changed_names(before, after)["attrs"] == ["gone", "new", "title"]


def test_edges_by_relation_name() -> None:
    kept = edge("ex:keeps", "b")
    moved = edge("ex:moves", "b")
    dropped = edge("ex:drops", "b")
    before = revision("a", "ex:Thing", edges=(kept, moved, dropped))
    after = replace(
        before,
        rev=2,
        edges=(kept, replace(moved, target=eid("c")), edge("ex:adds", "c")),
    )
    assert changed_names(before, after)["edges"] == {
        "added": ["ex:adds"],
        "removed": ["ex:drops"],
        "changed": ["ex:moves"],
    }


def test_record_fields_never_values() -> None:
    before = revision("a", "ex:Thing", attrs={"secret": "old"})
    after = replace(
        before, rev=2, record_status="retracted", alias="T-0002", valid_from="2030-01-01"
    )
    changed = changed_names(before, after)
    assert changed["record"] == ["record_status", "alias", "valid_from"]
    assert "old" not in repr(changed)
