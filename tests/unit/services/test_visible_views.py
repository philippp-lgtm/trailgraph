"""``hidden_views``: the views a reader of a filtered model must not see (views plan V10).

A view is hidden whole when it names a class, relation or parameter class the limited model lacks,
or no longer compiles against it (an attribute a type limit or a marking removed). A view that is
stale against the full model is hidden from a limited reader too (it cannot be called, and its
problems may name what the reader must not see). ``fields: all`` names nothing.
"""

from support import views
from support.fixtures import example_model

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kinetic.view_entries import ViewEntry, load_view_entries
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.visibility import filter_model
from graph_cli.services.visible_catalog import HiddenCatalog, hidden_views, view_hidden

MODEL = example_model("company", "1.1.0")
FUNCTIONS = views.function_entries(MODEL)
ALL_FIELDS = """\
schema: graph_cli.view/1
name: position_fields
module: organization
summary: Every position with all its values.
use_when: Every position, as a set view that names no attribute.
sections:
  - kind: list
    type: org:Position
    fields: all
"""


def catalog() -> tuple[JsonObject, tuple[ViewEntry, ...]]:
    sources = views.valid_sources()
    sources["position_fields.yaml"] = ALL_FIELDS
    document, problems = views.compile_views(sources, MODEL, FUNCTIONS)
    assert problems == []
    return document, load_view_entries(document)


def concrete() -> frozenset[str]:
    return frozenset(c for c, cls in MODEL.classes.items() if not cls.abstract)


def hidden(
    visible: frozenset[str], attributes: tuple[tuple[str, frozenset[str]], ...] | None = None
) -> frozenset[str]:
    document, entries = catalog()
    limited = filter_model(MODEL, visible, attributes)
    return hidden_views(entries, document, limited, FUNCTIONS)


def test_nothing_is_hidden_from_a_reader_who_sees_everything() -> None:
    assert hidden(concrete()) == frozenset()


def test_a_view_that_lists_or_takes_a_hidden_class_is_hidden() -> None:
    assert hidden(concrete() - {"ops:Task"}) == {"tasks_by_state"}
    # the abstract class stays while one subclass is visible: the views over it stay
    assert "unit_overview" not in hidden(concrete() - {"org:Department"})
    assert "legacy_unit_overview" in hidden(concrete() - {"org:Team"})  # takes the class itself


def test_a_view_with_a_reference_parameter_of_a_hidden_class_is_hidden() -> None:
    assert "members_report" in hidden(concrete() - {"org:Department"})
    assert "members_report" not in hidden(concrete() - {"org:Team"})


def test_a_view_over_a_relation_that_lost_an_end_is_hidden() -> None:
    found = hidden(concrete() - {"ppl:Employee", "ppl:External"})
    assert "position_overview" in found  # org:holds is gone with its holder class


def test_a_view_that_names_an_attribute_the_reader_lacks_is_hidden_and_all_fields_is_not() -> None:
    kept = tuple(
        (
            name,
            frozenset(a for a in cls.attributes if not (name == "org:Position" and a == "title")),
        )
        for name, cls in sorted(MODEL.classes.items())
        if not cls.abstract
    )
    found = hidden(concrete(), kept)
    # every view that names title of a position, in fields, order or a neighbour list
    assert {"org_catalog", "position_overview", "unit_overview"} <= found
    assert "position_fields" not in found  # fields: all names nothing
    assert not {"tasks_by_state", "members_report"} & found


def test_a_view_that_is_stale_for_everyone_is_hidden_from_a_limited_reader() -> None:
    document, entries = catalog()
    # the stored form of one view names an attribute that exists in no model: stale everywhere
    broken = {**document}
    broken["views"] = [
        {
            **e,
            "sections": [
                {**s, "fields": ["name", "no_such_attribute"]} if s["kind"] == "fields" else s
                for s in e["sections"]
            ],
        }
        if e["name"] == "unit_overview"
        else e
        for e in document["views"]  # type: ignore[attr-defined]
    ]
    limited = filter_model(MODEL, concrete() - {"ops:Task"}, None)
    found = hidden_views(entries, broken, limited, FUNCTIONS)
    assert found == {"tasks_by_state", "unit_overview"}  # stale for everyone, so not shown


def test_view_hidden_reads_the_touched_terms_of_the_entry() -> None:
    _, entries = catalog()
    by_name = {e.name: e for e in entries}
    limited: CompiledModel = filter_model(MODEL, concrete() - {"ops:Task"}, None)
    assert view_hidden(by_name["tasks_by_state"], limited)
    assert not view_hidden(by_name["unit_overview"], limited)


def test_the_hidden_names_are_computed_once_per_catalog_pin_and_view() -> None:
    cache, calls = HiddenCatalog(), []

    def compute() -> frozenset[str]:
        calls.append(1)
        return frozenset({"a"})

    key = ("views", "sha256:a+sha256:b", "sha256:pin", "view-1")
    assert cache.of(*key, compute) == {"a"} and cache.of(*key, compute) == {"a"}
    assert len(calls) == 1
    cache.of("views", "sha256:a+sha256:c", "sha256:pin", "view-1", compute)  # other functions
    assert len(calls) == 2
