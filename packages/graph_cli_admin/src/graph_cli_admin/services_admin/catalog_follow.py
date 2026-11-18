"""Published function and action catalogs follow a release that renames terms (migration plan
M8).

A published catalog keeps the authored sources and the compiled document the app reads. The
compiled queries hold the resolved names, CURIEs even where the source used bare names, so a
rename leaves a catalog that does not resolve although its sources would. ``follow`` takes the
published catalog and the model a release brings:

- the catalog still holds: nothing to do;
- it does not, the release carries renames or a migration (``recompile``) and its sources
  compile cleanly against the new model (bare names, CURIEs that still exist): the catalog is
  *republished*, the same sources with a newly compiled document (``Republish``). The sources
  hash stays, so ``change.<kind>.from`` equals ``to`` and ``compiled_hash`` differs, which the
  audit verifier accepts (an interrupted republish of the same sources);
- otherwise its problems stand and the release is refused (``admin.functions_broken``,
  ``admin.actions_broken``), unless the push that carries the release replaces the catalog.

``write`` is the W.e shape of a catalog switch, shared by ``graph <kind> publish`` and by a
release that republishes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.kinetic.action_compile import check_actions, compile_actions
from graph_cli.kinetic.compile import check_catalog, compile_catalog
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.models import ModelCache
from graph_cli.services.names import NameMaps, hinted_problems
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin.context import AdminContext


@dataclass(frozen=True, slots=True)
class Republish:
    """A catalog to publish again: ``slot`` holds the sources of ``current`` compiled against
    the new model; ``names`` are the entries whose stored form no longer resolved."""

    current: KineticSlot
    slot: KineticSlot
    names: tuple[str, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "count": self.slot.count,
            "names": list[JsonValue](self.names),
            "compiled_hash": {"from": self.current.compiled_hash, "to": self.slot.compiled_hash},
        }


@dataclass(frozen=True, slots=True)
class Followed:
    """What a release does with one published catalog: ``republish`` (``None``: nothing to
    publish again) and the ``problems`` that stand (the release is refused)."""

    republish: Republish | None
    problems: list[Problem]


def names_of(slot: KineticSlot | None) -> frozenset[str]:
    """The names in a published catalog (deprecated ones too: they still hold the name)."""
    if slot is None:
        return frozenset()
    key = slot.spec.list_key
    return frozenset(req_str(as_object(e, key), "name") for e in req_list(slot.compiled, key))


def catalog_problems(
    slot: KineticSlot | None,
    model: CompiledModel,
    *,
    function_names: frozenset[str] | None = None,
) -> list[Problem]:
    """The problems a published catalog has against ``model``; none for a missing one.
    ``function_names`` are the functions the graph has: an action may not share a name."""
    if slot is None:
        return []
    if slot.kind == "functions":
        return check_catalog(slot.compiled, model)
    return check_actions(slot.compiled, model, function_names=function_names or frozenset())


def compiled_from_sources(
    slot: KineticSlot, model: CompiledModel, function_names: frozenset[str]
) -> tuple[JsonObject, list[Problem]]:
    """The authored sources of ``slot`` compiled against ``model`` (what publishing them
    again would store)."""
    if slot.kind == "functions":
        compiled, problems = compile_catalog(slot.files, model)
    else:
        compiled, problems = compile_actions(slot.files, model, function_names=function_names)
    return dict(compiled), problems


def follow(
    slot: KineticSlot | None,
    model: CompiledModel,
    pin: OntologyPin,
    *,
    recompile: bool,
    function_names: frozenset[str] | None = None,
) -> Followed:
    """The published catalog against ``model``, the model of the release pinned as ``pin``.
    ``recompile``: the release carries renames or a migration, so a catalog that does not
    resolve is compiled again from its sources before it blocks."""
    problems = catalog_problems(slot, model, function_names=function_names)
    if not problems or slot is None:
        return Followed(None, [])
    if not recompile or not slot.files:
        return Followed(None, problems)
    compiled, found = compiled_from_sources(slot, model, function_names or frozenset())
    if found:
        return Followed(None, found)
    names = tuple(sorted({p.name for p in problems}))
    republished = KineticSlot(slot.hash, hash_value(compiled), pin, compiled, slot.files, slot.kind)
    return Followed(Republish(slot, republished, names), [])


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    current: KineticSlot | None,
    slot: KineticSlot,
    *,
    reason: str | None,
) -> None:
    """Switch the slot from ``current`` to ``slot`` in the W.e shape: the intent
    (``change.<kind>{from, to, compiled_hash}``), the slot rename (the commit point), the
    installation event anchoring the intent."""
    build = {
        "functions": events.functions_change,
        "actions": events.actions_change,
        "views": events.views_change,
    }[slot.kind]
    change = build(current.hash if current is not None else None, slot.hash, slot.compiled_hash)
    receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
    replace = {
        "functions": session.replace_functions,
        "actions": session.replace_actions,
        "views": session.replace_views,
    }[slot.kind]
    replace(slot, receipt)
    ctx.anchor(entry, decisions, meta, receipt, reason=reason)


# Problems of a folder with the current names of the old terms they name (migration plan M7).
ProblemHints = Callable[[list[Problem]], list[Problem]]


def rename_hints(
    ctx: AdminContext, entry: GraphEntry, model: CompiledModel, pending: Hop | None = None
) -> ProblemHints:
    """The hints for problems checked against ``model``: from the graph's name map (its pin
    history), with ``pending`` on top for a release made in the same push. The name map is
    read only when there are problems."""

    def hints(problems: list[Problem]) -> list[Problem]:
        if not problems:
            return problems
        try:
            name_map = NameMaps(ctx.catalog, ctx.audit, ModelCache(ctx.catalog)).of(entry)
        except GraphCliError:
            return problems
        if pending is not None:
            name_map = name_map.extended(pending)
        return hinted_problems(problems, name_map, model)

    return hints
