"""Functions, actions and views of a type-limited reader (type read limits plan V7, views plan V10).

A reader who sees only some types gets the published functions and actions that fit the
filtered model (``services.visibility.filter_model``) and none that names something it lacks,
so no listing, description, hint or skill ever carries a hidden term. A function or action is
hidden when:

- a type or relation its compiled query or ops touch is missing from the filtered model, or an
  action needs a write on a type that is missing;
- a parameter names a class (``ref``) or an enum that is missing;
- the module it belongs to lost all its terms to the filter (a main module that holds none in
  the full model either, an umbrella, is not hidden by that);
- it no longer compiles against the filtered model although it compiles against the full one:
  an unlabelled node can name an attribute only a hidden type has, which no ``touches`` list
  shows. A function that was already stale against the full model is not hidden by this check,
  its answer does not depend on what the reader sees.

Every other function runs over the filtered view (``services.visibility.VisibleSnapshot``), so
its answer holds what the reader may see. ``HiddenCatalog`` caches the names hidden per
(catalog, pin, read view): the check compiles every entry again, so it runs once per view.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry, Touched
from graph_cli.kinetic.params import ParamDef, Problem
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.ontology_layer.model import CompiledModel

_CACHE_LIMIT: Final = 64


def names_missing(
    model: CompiledModel,
    *,
    module: str | None,
    types: Iterable[str],
    relations: Iterable[str],
    params: Iterable[ParamDef],
) -> bool:
    """Whether a term of a catalog entry is missing from ``model``: a class, relation, enum
    or the module that owns the entry."""
    if module is not None and all(info.id != module for info in model.modules):
        return True
    if any(t not in model.classes for t in types):
        return True
    if any(r not in model.relations for r in relations):
        return True
    for param in params:
        if param.type == "ref" and param.of_class not in model.classes:
            return True
        if param.type == "enum" and param.enum not in model.enums:
            return True
    return False


def _curies(touched: Iterable[Touched]) -> list[str]:
    return [t.curie for t in touched]


def function_hidden(entry: FunctionEntry, model: CompiledModel) -> bool:
    """Whether ``entry`` names a term that ``model`` (a filtered model) does not have: what
    its query touches, its parameters and its module."""
    return names_missing(
        model,
        module=entry.module,
        types=_curies(entry.types),
        relations=_curies(entry.relations),
        params=entry.defn.params,
    )


def action_hidden(entry: ActionEntry, model: CompiledModel) -> bool:
    """As ``function_hidden``; an action also hides when a type it needs a write on is
    missing."""
    return names_missing(
        model,
        module=entry.module,
        types=[*_curies(entry.types), *(n.type for n in entry.needs)],
        relations=_curies(entry.relations),
        params=entry.defn.params,
    )


def view_hidden(entry: ViewEntry, model: CompiledModel) -> bool:
    """Whether ``entry`` names a term that ``model`` (a filtered model) does not have: the
    classes and relations it touches (its ``takes``, listed types and the classes of its
    ``ref`` parameters included), its parameters and its module."""
    return names_missing(
        model,
        module=entry.module,
        types=_curies(entry.types),
        relations=_curies(entry.relations),
        params=entry.defn.params,
    )


def _stale(problems: Iterable[Problem]) -> frozenset[str]:
    return frozenset(p.name for p in problems)


def hidden_functions(
    entries: Sequence[FunctionEntry],
    document: JsonObject,
    full: CompiledModel,
    limited: CompiledModel,
) -> frozenset[str]:
    """The names of the functions of ``document`` (``entries`` parsed) a reader of the
    ``limited`` model must not see, ``full`` being the model it is a view of."""
    from graph_cli.kinetic.compile import check_catalog  # compiles queries: Cypher engine

    hidden = {e.name for e in entries if function_hidden(e, limited)}
    if len(hidden) < len(entries):
        broken = _stale(check_catalog(document, limited)) - _stale(check_catalog(document, full))
        hidden |= broken
    return frozenset(hidden)


def hidden_actions(
    entries: Sequence[ActionEntry],
    document: JsonObject,
    full: CompiledModel,
    limited: CompiledModel,
) -> frozenset[str]:
    """``hidden_functions`` for the actions of a catalog document."""
    from graph_cli.kinetic.action_compile import check_actions  # compiles queries too

    hidden = {e.name for e in entries if action_hidden(e, limited)}
    if len(hidden) < len(entries):
        broken = _stale(check_actions(document, limited)) - _stale(check_actions(document, full))
        hidden |= broken
    return frozenset(hidden)


def hidden_views(
    entries: Sequence[ViewEntry],
    document: JsonObject,
    full: CompiledModel,
    limited: CompiledModel,
    functions: Sequence[FunctionEntry],
) -> frozenset[str]:
    """The names of the views of ``document`` (``entries`` parsed) a reader of the ``limited``
    model must not see: a view is hidden whole when it names a term the limited model lacks or
    has any problem against it (a ``fields``, ``where`` or ``order`` that names an attribute a
    type limit or a marking removes, a relation whose ends shrank). Unlike the functions and
    actions, a view that is already stale against the full model is hidden too: its problems may
    name what the reader must not see, and it cannot be called anyway (the reader gets
    ``view.not_found`` where a reader who sees everything gets ``view.stale``). ``functions``
    are the full published function entries; the functions a view runs are judged per call
    (``ReadService``: visible to the reader, ``read.function``)."""
    from graph_cli.kinetic.view_compile import check_views  # compiles function queries too

    hidden = {e.name for e in entries if view_hidden(e, limited)}
    if len(hidden) < len(entries):
        hidden |= _stale(check_views(document, limited, functions))
    return frozenset(hidden)


class HiddenCatalog:
    """The hidden names of a catalog for a read view, computed once and cached (bounded; the
    oldest entries go first)."""

    def __init__(self) -> None:
        self._found: dict[tuple[str, str, str, Hashable], frozenset[str]] = {}

    def of(
        self,
        kind: str,
        catalog_hash: str,
        pin_hash: str,
        view: Hashable,
        compute: Callable[[], frozenset[str]],
    ) -> frozenset[str]:
        """``compute()`` once per (kind, catalog, pin, ``view``): ``view`` is the reader's
        ``ReadView`` (any hashable key that tells the model views apart)."""
        key = (kind, catalog_hash, pin_hash, view)
        found = self._found.get(key)
        if found is None:
            found = compute()
            if len(self._found) >= _CACHE_LIMIT:
                del self._found[next(iter(self._found))]
            self._found[key] = found
        return found
