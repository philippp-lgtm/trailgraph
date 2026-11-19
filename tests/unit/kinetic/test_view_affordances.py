"""The ``views`` affordance: the entity views that take an entity (object views stage 2, V12)."""

from dataclasses import replace

from support.snapshots import revision
from support.views import company_model, compiled_entries

from graph_cli.kinetic.affordances import VIEWS_MAX, views_for
from graph_cli.kinetic.view_entries import ViewEntry

MODEL = company_model()
ENTRIES = compiled_entries()
VIEWS = tuple(ENTRIES.values())


def team() -> object:
    return revision("ops", "org:Team", {"name": "Ops"})


def named(entry: ViewEntry, name: str, **changes: object) -> ViewEntry:
    return replace(entry, defn=replace(entry.defn, name=name, **changes))  # type: ignore[arg-type]


def test_an_entity_view_is_named_through_the_class_it_takes_and_its_subclasses() -> None:
    unit = revision("hq", "org:OrgUnit", {"name": "HQ"})
    found = views_for(unit, VIEWS, MODEL)
    assert found.names == ("unit_overview",)  # the deprecated one takes org:Team
    ops = revision("ops", "org:Team", {"name": "Ops"})
    assert views_for(ops, VIEWS, MODEL).names == ("unit_overview",)  # a Team is an OrgUnit
    position = revision("lead", "org:Position", {"title": "Lead", "status": "active"})
    assert views_for(position, VIEWS, MODEL).names == ("position_overview",)


def test_the_view_that_graph_view_shows_is_left_out() -> None:
    unit = revision("hq", "org:OrgUnit", {"name": "HQ"})
    shown = [ENTRIES["unit_overview"]]
    assert views_for(unit, VIEWS, MODEL, shown).names == ()
    assert views_for(unit, VIEWS, MODEL, shown).to_json() == {}
    other = named(ENTRIES["unit_overview"], "unit_table", default=False)
    both = [ENTRIES["unit_overview"], other]
    assert views_for(unit, [*VIEWS, other], MODEL, both).names == ()
    assert views_for(unit, [*VIEWS, other], MODEL, shown).names == ("unit_table",)


def test_set_views_deprecated_views_and_other_classes_are_not_named() -> None:
    unit = revision("hq", "org:OrgUnit", {"name": "HQ"})
    assert (
        views_for(unit, (ENTRIES["org_catalog"], ENTRIES["legacy_unit_overview"]), MODEL).names
        == ()
    )
    project = revision("p", "ops:Policy", {"name": "P"})
    assert views_for(project, VIEWS, MODEL).names == ()


def test_the_key_is_absent_without_views_and_names_them_otherwise() -> None:
    unit = revision("hq", "org:OrgUnit", {"name": "HQ"})
    assert views_for(unit, (), MODEL).to_json() == {}
    assert views_for(unit, VIEWS, MODEL).to_json() == {"views": [{"view": "unit_overview"}]}


def test_views_are_capped_and_count_the_rest() -> None:
    unit = revision("hq", "org:OrgUnit", {"name": "HQ"})
    base = ENTRIES["unit_overview"]
    many = [named(base, f"unit_{n:02d}", default=False) for n in range(VIEWS_MAX + 4)]
    found = views_for(unit, many, MODEL)
    assert len(found.names) == VIEWS_MAX and found.more == 4
    assert found.names[0] == "unit_00"
    assert found.to_json()["views_more"] == 4
    assert "views_more" not in views_for(unit, many[:VIEWS_MAX], MODEL).to_json()


def test_an_inactive_entity_has_no_views() -> None:
    for record in ("superseded", "retracted"):
        gone = revision("hq", "org:OrgUnit", {"name": "HQ"}, status=record)  # type: ignore[arg-type]
        assert views_for(gone, VIEWS, MODEL).names == ()
