"""Catalogs for the tool manifest tests: the entries of the ``company`` example, of the
``inventory`` toy graph and of the ``staff`` toy graph (markings), and the readers of them.

A ``Scene`` is one caller's view: the model it sees and the functions, actions and views that
fit it, the way ``ReadService`` hands them to ``describe tools``. ``inventory(ITEMS)`` is a reader
of items only (a type read limit), ``staff(PERSONAL)`` one who holds a marking.
"""

from dataclasses import dataclass
from typing import Any

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.kinetic.view_entries import ViewEntry, load_view_entries
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.visibility import attribute_mask, filter_model
from graph_cli.services.visible_catalog import hidden_actions, hidden_functions, hidden_views
from support import functions as function_files
from support.actions import read_dir as read_actions
from support.fixtures import EXAMPLES, FIXTURES, compiled_model
from support.markings import ACTION_SOURCES, FUNCTION_SOURCES
from support.skills import graph_entry
from support.views import company_model, function_sources
from support.views import read_dir as read_views

ITEMS = frozenset({"inv:Item"})
EVERYTHING = frozenset({"stf:personal", "stf:pay"})
PERSONAL = frozenset({"stf:personal"})
NOTHING: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Catalog:
    """A graph's published entries over its full model."""

    entry: GraphEntry
    model: CompiledModel
    functions: tuple[FunctionEntry, ...]
    actions: tuple[ActionEntry, ...]
    views: tuple[ViewEntry, ...]
    function_document: dict[str, Any]
    action_document: dict[str, Any]
    view_document: dict[str, Any]

    def scene(self) -> "Scene":
        """What a caller without limits sees."""
        return Scene(self.entry, self.model, self.functions, self.actions, self.views)

    def reader(
        self, visible: frozenset[str] | None = None, held: frozenset[str] | None = None
    ) -> "Scene":
        """What a caller sees who reads only the types ``visible`` or holds only the markings
        ``held`` (``None``: no limit of that kind)."""
        mask = None if held is None else attribute_mask(self.model, held)
        limited = filter_model(self.model, visible, mask)
        functions = hidden_functions(self.functions, self.function_document, self.model, limited)
        actions = hidden_actions(self.actions, self.action_document, self.model, limited)
        views = hidden_views(self.views, self.view_document, limited, self.functions)
        return Scene(
            self.entry,
            limited,
            tuple(e for e in self.functions if e.name not in functions),
            tuple(e for e in self.actions if e.name not in actions),
            tuple(e for e in self.views if e.name not in views),
            ReadView(visible, mask),
        )


@dataclass(frozen=True)
class Scene:
    entry: GraphEntry
    model: CompiledModel
    functions: tuple[FunctionEntry, ...]
    actions: tuple[ActionEntry, ...]
    views: tuple[ViewEntry, ...]
    read_view: ReadView = UNLIMITED


def _compiled(
    model: CompiledModel,
    function_files_: dict[str, str],
    action_files: dict[str, str],
    view_files: dict[str, str],
) -> Catalog:
    functions_doc, problems = compile_catalog(function_files_, model)
    assert [p.to_json() for p in problems] == []
    functions = load_entries(functions_doc)
    actions_doc: JsonObject = {}
    actions: tuple[ActionEntry, ...] = ()
    if action_files:
        actions_doc, problems = compile_actions(action_files, model)
        assert [p.to_json() for p in problems] == []
        actions = load_action_entries(actions_doc)
    views_doc: JsonObject = {}
    views: tuple[ViewEntry, ...] = ()
    if view_files:
        views_doc, problems = compile_views(view_files, model, functions)
        assert [p.to_json() for p in problems] == []
        views = load_view_entries(views_doc)
    return Catalog(
        graph_entry(model), model, functions, actions, views, functions_doc, actions_doc, views_doc
    )


def company() -> Catalog:
    """The ``company`` example: its functions (and the fixture with a list and a date), actions
    and the valid view fixtures, which include a deprecated view and set views."""
    return _compiled(
        company_model(),
        function_sources(),
        read_actions(EXAMPLES / "company" / "actions"),
        read_views(FIXTURES / "views" / "company" / "valid"),
    )


def inventory() -> Catalog:
    """The ``inventory`` toy graph with the valid fixtures of its three kinds."""
    return _compiled(
        compiled_model("inventory"),
        function_files.read_dir(FIXTURES / "functions" / "inventory" / "valid"),
        read_actions(FIXTURES / "actions" / "inventory" / "valid"),
        read_views(FIXTURES / "views" / "inventory" / "valid"),
    )


def staff() -> Catalog:
    """The ``staff`` toy graph: its personal attributes carry markings; its functions and actions
    are the ones of ``support.markings`` (no views)."""
    return _compiled(compiled_model("staff"), FUNCTION_SOURCES, ACTION_SOURCES, {})
