"""The parts of a ``graph view`` call that need no storage (views plan V3, V7, V11).

``ReadService.view`` resolves the view, binds its parameters and hands the rest to the pure
renderers; what is decided from the view definition and the reader's model alone lives here:
the default view of an entity's class, the key a page cursor is tied to, the budget each
``rows`` section may spend, and the lazy ``rows`` results.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kinetic.params import json_value
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.kinetic.view_page import cursor_key
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value

CARD: Final = "card"  # the generated card; also the name no authored view may take

# What one view call may spend on its ``rows`` functions: the engine's own budget
# (``query.cypher.execute.DEFAULT_BUDGET``, pinned equal by a test; imported there would load
# the Cypher engine into every read), shared evenly by the ``rows`` sections of the view.
ROWS_BUDGET: Final = 100_000


def rows_share(sections: int) -> int:
    """The budget of one ``rows`` section of a view with ``sections`` of them. A share that
    depends on the definition alone, not on which sections a page happens to run, so a section
    answers the same on every page."""
    return max(1, ROWS_BUDGET // max(1, sections))


def has_default_for(views: Sequence[ViewEntry], model: CompiledModel, entity_type: str) -> bool:
    """Whether some default entity view takes the class of the entity (itself or an ancestor): a
    reader whose class has none is shown the card without judging what it may see of the views.
    ``views`` is the whole catalog, ``model`` the reader's."""
    return any(
        v.entity and v.defn.default and v.takes is not None and model.is_a(entity_type, v.takes)
        for v in views
    )


def default_view(
    views: Sequence[ViewEntry], model: CompiledModel, entity_type: str
) -> ViewEntry | None:
    """The default view of an entity of ``entity_type`` among the visible ``views``: that of
    the nearest class on its chain (itself, then its ancestors, nearest first) that has one;
    ``None``: the generated card. A class has at most one default (compile check)."""
    defaults = {
        v.takes: v for v in views if v.entity and v.defn.default and not v.deprecated and v.takes
    }
    if not defaults:
        return None
    found = model.classes.get(entity_type)
    chain = (entity_type, *(found.ancestors if found is not None else ()))
    for cls in chain:
        if cls in defaults:
            return defaults[cls]
    return None


def page_key(
    *,
    view_hash: str,
    functions_hash: str | None,
    pin_hash: str,
    reader: str | None,
    bound: Mapping[str, Value],
    names: Sequence[str],
) -> str:
    """The key a page cursor is tied to: the view, its parameters (bound, defaults included,
    ``ref`` values as canonical ids) and everything else that changes what the pages hold
    without moving the graph's seq: the compiled function catalog (a ``rows`` section runs
    its functions), the pinned model and what the reader sees. A change of any of them makes
    the cursor ``view.cursor_invalid``. The page size is not part of it."""
    composite = f"{view_hash}|{functions_hash or ''}|{pin_hash}|{reader or ''}"
    params: dict[str, JsonValue] = {name: json_value(bound[name]) for name in names}
    return cursor_key(composite, params)


class LazyRows(Mapping[str, RowsResult]):
    """The ``rows`` results of a view call by section id, each computed on first use: a page of
    a set view never runs the functions of sections before its cursor or after its last row."""

    def __init__(self, ids: Sequence[str], run: Callable[[str], RowsResult]) -> None:
        self._ids = tuple(ids)
        self._run = run
        self._done: dict[str, RowsResult] = {}

    def __getitem__(self, key: str) -> RowsResult:
        if key not in self._ids:
            raise KeyError(key)
        found = self._done.get(key)
        if found is None:
            found = self._done[key] = self._run(key)
        return found

    def __iter__(self) -> Iterator[str]:
        return iter(self._ids)

    def __len__(self) -> int:
        return len(self._ids)
