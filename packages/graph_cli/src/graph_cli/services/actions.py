"""Running a published action: ``graph do`` (actions plan A5, A6, A7).

``ActionService.do`` finds the action among those the caller may see, binds the
parameters by their declared types, resolves and class-checks the references on a
lock-free snapshot, and hands the action to the write service as a plan. The write
service expands the template and checks the preconditions under the graph lock, then
authorizes, records and commits the expansion as any change set (``services.write``).

An action is visible to an actor when every write right its ops need is allowed
(``ReadService.visible_actions``): a hidden action does not exist for the caller, so it
is ``action.not_found`` and ``did_you_mean`` names visible actions only; a hidden graph
is ``graph.not_found`` as for every command. Nothing of the parameter values leaves this
service except through the tx (the request digest covers them).
"""

from __future__ import annotations

from dataclasses import dataclass

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.records import ActionRef, RequestMeta
from graph_cli.kernel.timing import Timings
from graph_cli.kinetic.action_entries import find_action
from graph_cli.kinetic.entries import seen_replacement
from graph_cli.kinetic.expand import check_reason
from graph_cli.kinetic.params import json_value
from graph_cli.services.catalog_access import allowed_entries
from graph_cli.services.models import ModelCache
from graph_cli.services.read import ReadService
from graph_cli.services.refs import Given, bind_params
from graph_cli.services.write import ActionPlan, WriteRequest, WriteResult, WriteService


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The write the action made (or, as a dry run, would make) and which action it was."""

    write: WriteResult
    action: ActionRef
    warnings: tuple[WarningRecord, ...]  # of the action, before the write's own

    def to_json(self) -> dict[str, JsonValue]:
        return {**self.write.to_json(), "action": self.action.to_json()}


class ActionService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        reader: ReadService,
        writer: WriteService,
        timings: Timings | None = None,
    ) -> None:
        self._catalog = catalog
        self._reader = reader
        self._writer = writer
        self.timings = timings if timings is not None else Timings()
        self._models = ModelCache(catalog, self.timings)

    def do(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        name: str,
        given: Given,
        *,
        dry_run: bool,
        reason: str | None,
        idempotency_key: str | None,
    ) -> ActionResult:
        """Run action ``name`` of the graph with ``name=value`` parameter pairs or a typed JSON
        object (``services.refs.bind_params``)."""
        if not self._reader.visible(actor, entry):
            request = RequestMeta.of("do", {"graph": entry.name})
            raise self._reader.refuse_hidden(actor, entry, entry.name, request)
        published = self._reader.published_actions(entry)
        classified = (
            () if published is None else self._reader.classified_actions(actor, entry, published)
        )
        visible = allowed_entries(classified)
        # an action the caller sees but may not run is a recorded denial, not a missing one
        for item in classified:
            if item.entry.name == name and not item.allowed:
                request = RequestMeta.of("do", {"graph": entry.name, "action": name})
                raise self._reader.refuse_not_allowed(actor, entry, item, request)
        # a name the caller sees but may not run was refused above: what is found here is allowed,
        # and the suggestions of a name that is none are over everything the caller sees
        action = find_action([c.entry for c in classified], name)
        assert published is not None
        model = self._models.load(entry)
        problem = self._reader.action_problem(entry, published, model, name)
        if problem is not None:
            raise GraphCliError(
                "action.stale",
                f"action {name} no longer fits the graph's ontology ({problem.code}); "
                "tell the maintainer of this graph",
                details={"action": name, "hash": action.hash, "cause": problem.code},
            )
        warnings: list[WarningRecord] = []
        if action.deprecated:
            replaced = seen_replacement(action.defn.replaced_by, visible)
            warnings.append(
                WarningRecord(
                    "action.deprecated",
                    f"action {name} is deprecated"
                    + (f"; use {replaced}" if replaced is not None else ""),
                    {"name": name, "replaced_by": replaced},
                )
            )
        check_reason(action, reason)
        read, _ = self._catalog.store_for(entry.id)
        with self.timings.phase("snapshot"):
            raw = read.snapshot()
        # A type limited caller names entities among those it sees: a hidden one is missing.
        snapshot, shown, _ = self._reader.limited_view(actor, entry, raw, model)
        bound, found = bind_params(
            "action",
            name,
            action.defn.params,
            given,
            model=model,
            snapshot=snapshot,
            shown=shown,
        )
        warnings += found
        ref = ActionRef(name, action.hash)
        request = RequestMeta.of(
            "do",
            {
                "graph": entry.name,
                "action": name,
                "hash": action.hash,
                "params": {key: json_value(value) for key, value in sorted(bound.items())},
                "dry_run": dry_run,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
            ref,
        )
        write = self._writer.apply(
            WriteRequest(
                actor=actor,
                graph=str(entry.id),
                changeset=ChangeSet(ops=(), reason=reason, idempotency_key=idempotency_key),
                dry_run=dry_run,
                request=request,
                plan=ActionPlan(action, bound),
            )
        )
        return ActionResult(write, ref, tuple(warnings))
