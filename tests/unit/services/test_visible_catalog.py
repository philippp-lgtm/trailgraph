"""Functions and actions of a type-limited reader (type read limits plan V7): which ones a model
view hides and why, the describe documents and the generated skill of a reader who sees only
items (goldens), and the curated-text name check against the full model.

The inventory toy ontology has items, locations and categories. A reader of items alone loses
the function over a location, the action that moves an item to one, the relations to locations
and categories and the modules and enums nothing visible uses.
"""

import json
from dataclasses import replace
from typing import Any

import pytest
from support.actions import read_dir
from support.fixtures import FIXTURES, GOLDENS, assert_golden, compiled_model
from support.skills import GRAPH_SKILL_NAME, graph_entry, render_inputs

from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.params import ParamDef
from graph_cli.services.describe import (
    describe_action,
    describe_actions,
    describe_function,
    describe_functions,
    describe_module,
    describe_modules,
)
from graph_cli.services.schema_search import search_model
from graph_cli.services.visibility import filter_model
from graph_cli.services.visible_catalog import (
    HiddenCatalog,
    action_hidden,
    function_hidden,
    hidden_actions,
    hidden_functions,
)
from graph_cli.skills.graph_skill import render_checked
from graph_cli.skills.names import check_names
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import SKILL_FILE, read_tree

MODEL = compiled_model("inventory")
ENTRY = graph_entry(MODEL)
TEMPLATE = FIXTURES / "skills" / "inventory_limited" / GRAPH_SKILL_NAME
ALL = frozenset({"inv:Category", "inv:Item", "inv:Location"})
ITEMS = frozenset({"inv:Item"})
PLACES = frozenset({"inv:Location", "inv:Category"})
# Names a reader of items alone must never meet
HIDDEN_NAMES = (
    "Location",
    "located_in",
    "Category",
    "in_category",
    "items_in_location",
    "move_item",
)
# Names no ``touches`` list shows: an unlabelled node reaches an attribute only items have
NAMED_BY_QUANTITY = """\
schema: graph_cli.function/1
name: named_by_quantity
summary: Names of the things held in a larger number.
use_when: Which things come in numbers above a limit, whatever they are.
params:
  limit:
    type: integer
    summary: The least number.
query: |
  MATCH (n) WHERE n.quantity > $limit RETURN n.name AS name
returns:
  - column: name
    summary: Name of the thing.
examples:
  - question: Which things come in numbers above 10?
"""


def functions_document() -> dict[str, Any]:
    sources = read_dir(FIXTURES / "functions" / "inventory" / "valid")
    sources["named_by_quantity.yaml"] = NAMED_BY_QUANTITY
    document, problems = compile_catalog(sources, MODEL)
    assert [p.to_json() for p in problems] == []
    return document


def actions_document() -> dict[str, Any]:
    document, problems = compile_actions(
        read_dir(FIXTURES / "actions" / "inventory" / "valid"), MODEL
    )
    assert [p.to_json() for p in problems] == []
    return document


FUNCTIONS_DOC = functions_document()
ACTIONS_DOC = actions_document()
FUNCTIONS = load_entries(FUNCTIONS_DOC)
ACTIONS = load_action_entries(ACTIONS_DOC)


def names(entries: Any) -> list[str]:
    return [e.name for e in entries]


class Reader:
    """What a reader who sees ``visible`` is given: its model and the entries that fit it."""

    def __init__(self, visible: frozenset[str]) -> None:
        self.visible = visible
        self.model = filter_model(MODEL, visible)
        self.hidden_functions = hidden_functions(FUNCTIONS, FUNCTIONS_DOC, MODEL, self.model)
        self.hidden_actions = hidden_actions(ACTIONS, ACTIONS_DOC, MODEL, self.model)
        self.functions = tuple(f for f in FUNCTIONS if f.name not in self.hidden_functions)
        self.actions = tuple(a for a in ACTIONS if a.name not in self.hidden_actions)

    def catalog(self, key: str, document: dict[str, Any], kept: Any) -> dict[str, Any]:
        wanted = set(names(kept))
        return {**document, key: [e for e in document[key] if e["name"] in wanted]}

    def documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        functions = self.catalog("functions", FUNCTIONS_DOC, self.functions)
        actions = self.catalog("actions", ACTIONS_DOC, self.actions)
        return render_inputs(self.model, functions=functions or None, actions=actions or None)


# ---- what a model view hides ------------------------------------------------------------


def test_the_toy_catalogs_hold_what_the_checks_below_need() -> None:
    assert names(FUNCTIONS) == [
        "items_by_condition",
        "items_in_location",
        "named_by_quantity",
    ]
    assert names(ACTIONS) == ["move_item", "retire_item"]


def test_a_reader_who_sees_everything_loses_nothing() -> None:
    reader = Reader(ALL)
    assert reader.hidden_functions == reader.hidden_actions == frozenset()


def test_a_function_naming_a_hidden_type_relation_or_parameter_class_is_hidden() -> None:
    reader = Reader(ITEMS)
    # touches a hidden type and relation, and takes a ref to a hidden class
    assert reader.hidden_functions == {"items_in_location"}
    assert names(reader.functions) == ["items_by_condition", "named_by_quantity"]
    # nothing of the items remains for a reader of places: every function names an item term
    assert Reader(PLACES).hidden_functions == {
        "items_by_condition",
        "items_in_location",
        "named_by_quantity",
    }
    # a reader of items and locations keeps the function over locations, loses nothing
    assert Reader(ITEMS | {"inv:Location"}).hidden_functions == frozenset()


def test_an_action_naming_a_hidden_type_relation_or_parameter_class_is_hidden() -> None:
    assert Reader(ITEMS).hidden_actions == {"move_item"}  # it takes a location
    assert Reader(PLACES).hidden_actions == {"move_item", "retire_item"}
    assert Reader(ITEMS | {"inv:Location"}).hidden_actions == frozenset()


def test_a_function_that_names_no_hidden_term_by_label_is_hidden_when_it_cannot_resolve() -> None:
    """``named_by_quantity`` has no label, relation or class parameter: its ``touches`` are
    empty, but its attribute exists on items only, so over the view of places it is stale."""
    function = next(f for f in FUNCTIONS if f.name == "named_by_quantity")
    view = filter_model(MODEL, PLACES)
    assert function.types == () and function.relations == ()
    assert not function_hidden(function, view)  # the structural check alone passes
    assert "named_by_quantity" in hidden_functions(FUNCTIONS, FUNCTIONS_DOC, MODEL, view)
    # over the view of items it resolves and stays
    assert "named_by_quantity" not in Reader(ITEMS).hidden_functions


def with_params(entry: FunctionEntry, *params: ParamDef) -> FunctionEntry:
    return replace(entry, defn=replace(entry.defn, params=tuple(params)))


def test_a_parameter_that_names_a_missing_enum_hides_the_function() -> None:
    view = filter_model(MODEL, PLACES)
    assert "inv:Condition" not in view.enums  # only items use it
    code = ParamDef("code", "string", "A code.")
    enum = ParamDef("kind", "enum", "A kind.", enum="inv:Condition")
    base = next(f for f in FUNCTIONS if f.name == "items_by_condition")
    on_places = replace(base, types=(), relations=(), module="inventory")
    assert not function_hidden(with_params(on_places, code), view)
    assert function_hidden(with_params(on_places, code, enum), view)
    assert function_hidden(with_params(on_places, ParamDef("l", "ref", "L.", "inv:Item")), view)


def test_the_module_of_an_entry_must_still_hold_a_term() -> None:
    view = filter_model(MODEL, ITEMS)
    base = next(f for f in FUNCTIONS if f.name == "items_by_condition")
    assert not function_hidden(base, view)
    assert function_hidden(replace(base, module="notes_and_sources"), view)
    assert not function_hidden(replace(base, module=None), view)


def test_an_action_hides_when_a_type_it_needs_a_write_on_is_missing() -> None:
    view = filter_model(MODEL, ITEMS)
    retire = next(a for a in ACTIONS if a.name == "retire_item")
    assert not action_hidden(retire, view)
    needs = (*retire.needs, replace(retire.needs[0], type="inv:Location"))
    assert action_hidden(replace(retire, needs=needs), view)


def test_the_hidden_names_are_computed_once_per_catalog_pin_and_set() -> None:
    cache = HiddenCatalog()
    calls: list[int] = []

    def compute() -> frozenset[str]:
        calls.append(1)
        return frozenset({"a"})

    for _ in range(3):
        assert cache.of("functions", "h1", "p1", ITEMS, compute) == {"a"}
    cache.of("functions", "h1", "p1", PLACES, compute)  # another set
    cache.of("actions", "h1", "p1", ITEMS, compute)  # another kind
    cache.of("functions", "h2", "p1", ITEMS, compute)  # another catalog
    cache.of("functions", "h1", "p2", ITEMS, compute)  # another pin
    assert len(calls) == 5


# ---- the documents a reader of items gets ------------------------------------------------


def golden(name: str, document: dict[str, Any]) -> None:
    data = json.loads(json.dumps(document))
    del data["graph"]["id"]
    assert_golden(
        GOLDENS / "describe" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode()
    )


def test_describe_goldens_of_a_reader_of_items() -> None:
    reader = Reader(ITEMS)
    model, functions, actions = reader.model, reader.functions, reader.actions
    golden("limited_modules_inventory_items", describe_modules(ENTRY, model, functions, actions))
    golden(
        "limited_module_inventory_inventory_items",
        describe_module(ENTRY, model, "inventory", functions, actions),
    )
    golden("limited_functions_inventory_items", describe_functions(ENTRY, model, functions))
    golden("limited_actions_inventory_items", describe_actions(ENTRY, model, actions))
    golden(
        "limited_function_inventory_items_items_by_condition",
        describe_function(ENTRY, model, functions, "items_by_condition"),
    )
    golden(
        "limited_action_inventory_items_retire_item",
        describe_action(ENTRY, model, actions, "retire_item"),
    )


def test_the_counts_and_listings_cover_the_visible_entries_only() -> None:
    reader = Reader(ITEMS)
    full = describe_modules(ENTRY, MODEL, FUNCTIONS, ACTIONS)["modules"]
    limited = describe_modules(ENTRY, reader.model, reader.functions, reader.actions)["modules"]
    counts = lambda modules: {  # noqa: E731
        m["id"]: (m["functions"], m["actions"])
        for m in modules  # type: ignore[index]
    }
    assert counts(full)["inventory"] == (3, 2)
    assert counts(limited)["inventory"] == (2, 1)


def test_a_hidden_function_or_action_is_as_unknown_as_a_missing_one() -> None:
    from graph_cli.kernel.errors import GraphCliError

    reader = Reader(ITEMS)

    def error(call: Any, *args: Any, suggestions: bool = False) -> tuple[str, str, Any]:
        with pytest.raises(GraphCliError) as info:
            call(ENTRY, reader.model, *args)
        details = dict(info.value.details)
        if not suggestions:  # they follow the spelling of the name asked
            details.pop("did_you_mean")
        return info.value.code, info.value.message, details

    for call, entries, hidden, other in (
        (describe_function, reader.functions, "items_in_location", "items_in_nowhere"),
        (describe_action, reader.actions, "move_item", "move_nothing"),
    ):
        asked, absent = error(call, entries, hidden), error(call, entries, other)
        assert json.dumps(asked).replace(hidden, other) == json.dumps(absent)
        assert hidden not in json.dumps(error(call, entries, hidden[:-1], suggestions=True))


def test_a_search_names_no_hidden_function_action_or_term() -> None:
    reader = Reader(ITEMS)
    for text in ("location", "move", "stored", "category", "put away"):
        body, _ = search_model(
            ENTRY, reader.model, text, functions=reader.functions, actions=reader.actions
        )
        hits = json.dumps(body)
        for name in HIDDEN_NAMES:
            assert name not in hits, (text, name)
    full, _ = search_model(ENTRY, MODEL, "location", functions=FUNCTIONS, actions=ACTIONS)
    assert "items_in_location" in json.dumps(full)  # positive control


# ---- the generated skill -----------------------------------------------------------------


def template_files() -> dict[str, bytes]:
    files, problems = read_tree(TEMPLATE)
    assert problems == []
    return files


def render(reader: Reader, files: dict[str, bytes] | None = None) -> dict[str, bytes]:
    documents, graph = reader.documents()
    rendered, problems = render_checked(
        files or template_files(), documents, graph, name=GRAPH_SKILL_NAME
    )
    assert [p.to_json() for p in problems] == [] and rendered is not None
    return rendered


def test_the_skill_of_a_reader_of_items_names_nothing_hidden() -> None:
    rendered = render(Reader(ITEMS))
    for path, data in rendered.items():
        text = data.decode()
        for name in HIDDEN_NAMES:
            assert name not in text, (path, name)
    everything = "".join(data.decode() for data in rendered.values())
    assert "items_by_condition" in everything and "retire_item" in everything
    for path, data in rendered.items():
        assert_golden(GOLDENS / "skills" / "inventory_limited" / path, data)


def test_the_skill_of_a_reader_of_everything_is_the_unlimited_one() -> None:
    full = Reader(ALL)
    documents, graph = render_inputs(MODEL, functions=FUNCTIONS_DOC, actions=ACTIONS_DOC)
    rendered, problems = render_checked(template_files(), documents, graph, name=GRAPH_SKILL_NAME)
    assert problems == [] and rendered == render(full)
    assert b"items_in_location" in rendered[SKILL_FILE] + b"".join(rendered.values())


CURATED = b"\nLocations (`inv:Location`) hold the items; see also `inv:located_in`.\n"
INVENTORY = {"commands": [], "global_options": []}


def test_curated_text_naming_a_hidden_type_is_checked_against_the_full_model() -> None:
    """The curated text is the maintainer's prose, left as written; a name check against the
    reader's own documents would report ``inv:Location`` to a reader who must not know it."""
    files = template_files()
    files[SKILL_FILE] = files[SKILL_FILE].replace(
        b"## Is it available here?", CURATED.strip() + b"\n\n## Is it available here?"
    )
    template, problems = parse_template(files)
    assert problems == [] and template is not None
    limited = Reader(ITEMS)
    own, graph = limited.documents()
    full, _ = render_inputs(MODEL, functions=FUNCTIONS_DOC, actions=ACTIONS_DOC)
    against_own = check_names(template, own, graph, INVENTORY)
    assert {p.rule for p in against_own} == {"term"}
    assert any("inv:Location" in p.message for p in against_own)  # what must not be sent
    assert check_names(template, full, graph, INVENTORY) == []
    # the text itself is kept as written
    assert b"`inv:Location`" in render(limited, files)[SKILL_FILE]
