"""Finding entities: find, lookup and search, and how a lookup fits the callable functions."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EdgeRecord
from graph_cli.kinetic.entries import find_entry
from graph_cli.kinetic.params import ParamDef
from graph_cli.kinetic.view_entries import find_view
from graph_cli.query.cypher.errors import did_you_mean
from graph_cli.query.lookup import (
    LookupPlan,
    LookupSpec,
    fact_edges,
    fact_label,
    fit_classes,
    main_label,
    match_revision,
    plan_lookup,
    rank_key,
)
from graph_cli.query.plan import FindSpec, check_fields, plan_find, resolve_term
from graph_cli.query.search import SearchSpec, plan_search
from graph_cli.query.search import main_label as search_label
from graph_cli.query.search import match_revision as search_match
from graph_cli.query.search import rank_key as search_rank
from graph_cli.services.catalog_access import EntryPath, EntryRef
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.graph_read import GraphRead, Page
from graph_cli.services.read.json_shapes import entity_json
from graph_cli.services.read.state import query_phase
from graph_cli.services.refs import resolve_ref

# The kinds ``--for`` may name before a function, action or view (``view:NAME.PARAM``), and the
# slot of the entity of a view that shows one.
SLOT_KINDS: Final = ("function", "action", "view")
ENTITY_SLOT: Final = "_entity"


@dataclass(frozen=True, slots=True)
class _Slot:
    """A parameter a lookup can be for: its name, its type as written, and its class (``None``
    unless it is a ``ref``)."""

    name: str
    type_text: str
    of_class: str | None


def _narrow_shape(spec: LookupSpec) -> bool:
    """Whether ``spec`` asks nothing a lookup through an entry may not: an exact match of the
    text in the graph's language, first page, active records, every type of the slot."""
    return (
        spec.match == "exact"
        and spec.offset == 0
        and not spec.include_inactive
        and not spec.ignore_accents
        and not spec.types
        and spec.lang is None
    )


def _param_slots(params: Sequence[ParamDef]) -> list[_Slot]:
    return [_Slot(p.name, p.type_text, p.of_class if p.type == "ref" else None) for p in params]


def split_slot(use: str) -> tuple[str | None, str, str] | None:
    """``(kind or None, entry name, parameter)`` of a ``--for`` that names a parameter
    (``NAME.PARAM``, ``kind:NAME.PARAM``); ``None`` when it names a relation (no ``.``) or its
    qualifier is no kind of entry."""
    owner, dot, param = use.partition(".")
    if not dot:
        return None
    qualifier, colon, name = owner.rpartition(":")
    if colon and qualifier not in SLOT_KINDS:
        return None
    return (qualifier if colon else None), name, param


class ReadFinding(ReadCatalogs):
    def lookup_path(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        spec: LookupSpec,
        *,
        for_use: str | None,
        for_source: str | None,
    ) -> EntryPath | None:
        """The entry a lookup may work through when the caller's own right to find entities does
        not allow it (``open(through=...)``): the function, action or view whose parameter
        ``--for`` names, and only for the narrow form of a lookup: ``--for NAME.PARAM`` with the
        text and the limit, an exact match, the first page, active records, no ``--type``,
        ``--lang`` or accent folding. Any other shape (a relation, ``--for-source``, a prefix, an
        offset, ``--include-inactive``) has no entry: it is the plain ``read.find`` question and a
        caller that lacks the right is refused. It decides no right: ``open`` does. An unqualified
        name is the action when the caller may run an action of that name, else a function (the
        order ``_slots`` looks in), so that a name no entry answers to is one error for every
        caller. Only for a caller limited to its catalog (``catalog_scope``), the one
        ``lookup_served`` serves through entries, and for a graph it may list (an unreadable
        catalog of a graph it cannot see is no reason to answer before ``graph.not_found``);
        for everyone else the lookup is the plain ``read.find`` question."""
        parsed = None if for_use is None else split_slot(for_use)
        if parsed is None or for_source is not None or not _narrow_shape(spec):
            return None
        if self.catalog_scope(actor, entry) is None or not self.visible(actor, entry):
            return None  # only a caller limited to its catalog works through an entry here
        qualifier, name, _ = parsed
        if qualifier is None:
            actions = self.actions(actor, entry) or ()
            qualifier = "action" if any(a.name == name for a in actions) else "function"
        return EntryPath(qualifier, (name,))

    @query_phase
    def find(
        self,
        view: GraphRead,
        spec: FindSpec,
        *,
        rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        in_rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        fields: Sequence[str] = (),
        full: bool = False,
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        warnings: list[WarningRecord] = []
        shown = check_fields(view.model, spec.types, fields)

        def resolve(
            pairs: Sequence[tuple[str, Sequence[str]]], *, outgoing: bool
        ) -> tuple[tuple[str, tuple[EntityId, ...]], ...]:
            resolved = []
            for rel, texts in pairs:
                entities: list[EntityId] = []
                for text in texts:
                    entity, found = resolve_ref(
                        text,
                        view.snapshot,
                        argument="--rel" if outgoing else "--in-rel",
                        lookup_for=rel if outgoing else None,
                        lookup_for_source=None if outgoing else rel,
                    )
                    warnings.extend(found)
                    entities.append(entity)
                resolved.append((rel, tuple(sorted(set(entities)))))
            return tuple(resolved)

        spec = FindSpec(
            types=spec.types,
            where=spec.where,
            rels=(*spec.rels, *resolve(rel_refs, outgoing=True)),
            in_rels=(*spec.in_rels, *resolve(in_rel_refs, outgoing=False)),
            include_inactive=spec.include_inactive,
            sort=spec.sort,
            limit=spec.limit,
            offset=spec.offset,
        )
        query: Query = plan_find(view.model, spec)
        page = view.snapshot.query(query)
        items: list[JsonValue] = [
            entity_json(r, shown, full=full, names_mapped=view.names_mapped(r)) for r in page.items
        ]
        data: dict[str, JsonValue] = {"items": items}
        self._signal(view, [r.id for r in page.items])
        return data, Page(page.total, len(page.items), page.limit, page.offset), warnings

    def _use_fit(
        self, view: GraphRead, use: str | None, source: str | None
    ) -> tuple[tuple[str, ...] | None, bool]:
        """The classes ``--for`` and ``--for-source`` allow, intersected, and whether either
        was given. ``--for`` is a relation (the candidate is its target) or
        ``<function or action>.<param>``, a ``ref`` parameter of a published function the
        actor may read (``read.function``) or of an action the actor may run; ``--for-source`` is
        a relation (the candidate is its source). None: any class."""
        fits: list[tuple[str, ...] | None] = []
        if use is not None:
            if "." in use:
                fits.append(self._parameter_fit(view, use))
            else:
                relation = resolve_term("relation", use, view.model.relations)
                fits.append(fit_classes(view.model, relation, "target"))
        if source is not None:
            relation = resolve_term("relation", source, view.model.relations)
            fits.append(fit_classes(view.model, relation, "source"))
        allowed = [set(found) for found in fits if found is not None]
        if not allowed:
            return None, bool(fits)
        return tuple(sorted(set.intersection(*allowed))), True

    def _parameter_fit(self, view: GraphRead, use: str) -> tuple[str, ...]:
        """The class of a ``ref`` parameter of a function, an action or a view and its subclasses
        (``--for NAME.PARAM``): the slot ``--for`` names (see ``_slot``)."""
        parsed = split_slot(use)
        if parsed is None:
            bad = use.partition(".")[0].rpartition(":")[0]
            raise GraphCliError(
                "usage.invalid_argument",
                f"--for: {bad!r} is not a kind; write NAME.PARAM or "
                + " or ".join(f"{kind}:NAME.PARAM" for kind in SLOT_KINDS),
                details={"argument": "--for", "actual": use, "expected": list(SLOT_KINDS)},
            )
        qualifier, name, param_name = parsed
        through = view.through
        asked = through.kind if through is not None and qualifier is None else qualifier
        # (through: the entry the lookup was opened through says which kind an unqualified name is)
        kind, slots = self._slots(view, asked, name)
        if through is not None and through != EntryRef(kind, name):
            # the command layer opened the graph through another entry: nothing it decided holds
            raise GraphCliError("internal.error", "a lookup was opened through another entry")
        found = next((s for s in slots if s.name == param_name), None)
        if found is None:
            known = [s.name for s in slots]
            raise GraphCliError(
                f"{kind}.parameter_unknown",
                f"{name} has no parameter {param_name!r}"
                + (f"; it takes {', '.join(known)}" if known else " and takes no parameters"),
                details={
                    kind: name,
                    "parameter": param_name,
                    "expected": known,
                    "did_you_mean": did_you_mean(param_name, known),
                },
            )
        if found.of_class is None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--for: parameter {param_name!r} of {name} is {found.type_text}, not a ref",
                details={"argument": "--for", kind: name, "parameter": param_name},
            )
        return view.model.subtypes(found.of_class)

    def _slots(self, view: GraphRead, qualifier: str | None, name: str) -> tuple[str, list[_Slot]]:
        """The kind and the slots of the function, action or view ``name`` the actor may use: its
        parameters and, for a view that shows one entity, its entity (``ENTITY_SLOT``). A
        ``qualifier`` (``function``, ``action`` or ``view``) says which catalog to look in; without
        one, names are unique across functions and actions, so a visible action is looked up
        first, then the functions (a view needs its qualifier: it may share its name with a
        function). What the actor may not use is not found, like what it does not see."""
        if qualifier == "view":
            found = find_view(self.views(view.actor, view.entry), name)
            slots = _param_slots(found.defn.params)
            if found.takes is not None:
                slots.insert(0, _Slot(ENTITY_SLOT, "ref", found.takes))
            return "view", slots
        actions = self.actions(view.actor, view.entry)
        action = next((a for a in actions or () if a.name == name), None)
        if qualifier == "action" and action is None:
            names = sorted(a.name for a in actions or ())
            raise GraphCliError(
                "action.not_found",
                f"no action {name!r}",
                details={"name": name, "did_you_mean": difflib.get_close_matches(name, names, n=5)},
            )
        if action is not None and qualifier != "function":
            return "action", _param_slots(action.defn.params)
        functions = self.functions(view.actor, view.entry)
        if (
            qualifier is None
            and actions is not None
            and all(f.name != name for f in functions or ())
        ):
            names = sorted([*(f.name for f in functions or ()), *(a.name for a in actions)])
            raise GraphCliError(
                "function.not_found",
                f"no function or action {name!r}",
                details={
                    "name": name,
                    "did_you_mean": difflib.get_close_matches(name, names, n=5),
                },
            )
        return "function", _param_slots(find_entry(functions, name).defn.params)

    @query_phase
    def lookup(
        self,
        view: GraphRead,
        spec: LookupSpec,
        texts: Sequence[str] = (),
        *,
        for_use: str | None = None,
        for_source: str | None = None,
    ) -> tuple[dict[str, JsonValue], Page]:
        """Entities whose labels match, best first; one item per entity and text, its best match.

        ``texts`` (default: ``spec.text`` alone) are looked up one after the other with the
        options of ``spec``; texts with the same normalized key are looked up once. Limit and
        offset apply per text; ``data.texts`` counts the candidates and exact matches of each.

        ``for_use`` / ``for_source`` (``--for``, ``--for-source``) narrow the candidates to the
        classes that can fill the slot (see ``_use_fit``) and replace ``spec.fit``. A match of
        another class is no candidate: ``data.texts[].excluded`` counts it, ``total`` and
        ``exact`` do not, and ``data.fit`` names the options and the classes that fit.

        ``items[].facts`` tells the candidates of a text apart (``query.lookup.fact_edges``):
        a list when the text has more than one candidate, else null. One ``get_many`` reads
        the targets of every shown item.

        A graph opened through an entry (``view.through``: the caller may not find entities, its
        grants name the entry whose parameter ``--for`` is) gets the narrow lookup: the request
        must be the narrow shape (``lookup_path`` decided it; anything else is a bug and
        ``internal.error``), only active exact matches of the slot's class among the types the
        caller sees are candidates, the first page only (no ``next_offset``), and a match of
        another class is neither shown nor counted (``excluded`` is 0). The facts are those of
        the visible model and snapshot, as for any limited reader.
        """
        narrow = view.through is not None
        if narrow and not (
            _narrow_shape(spec) and for_use is not None and "." in for_use and for_source is None
        ):
            raise GraphCliError("internal.error", "a lookup through an entry asked for more")
        fit, asked = self._use_fit(view, for_use, for_source)
        spec = replace(spec, fit=fit)
        plans: dict[str, tuple[str, LookupPlan]] = {}
        for text in texts or (spec.text,):
            plan = plan_lookup(view.model, replace(spec, text=text), view.entry.language)
            plans.setdefault(plan.key, (text, plan))
        revisions = view.snapshot.query(next(iter(plans.values()))[1].query).items
        items: list[JsonValue] = []
        counts: list[JsonValue] = []
        shown_ids: list[EntityId] = []
        total = more = 0
        pending: list[tuple[dict[str, JsonValue], LookupPlan, tuple[EdgeRecord, ...]]] = []
        targets: set[EntityId] = set()
        for text, plan in plans.values():
            found = []
            excluded = 0
            for revision in revisions:
                match = match_revision(plan, revision)
                if match is None:
                    continue
                if plan.fits(revision.type):
                    found.append((rank_key(plan, revision, match), revision, match))
                elif not narrow:
                    excluded += 1
            found.sort(key=lambda row: row[0])
            shown = found[plan.offset : plan.offset + plan.limit]
            ambiguous = len(found) > 1
            for _, revision, match in shown:
                item: dict[str, JsonValue] = {
                    "text": text,
                    "id": str(revision.id),
                    "alias": revision.alias,
                    "type": revision.type,
                    "record_status": revision.record_status,
                    "label": main_label(plan, revision, match),
                    "match": match.to_json(),
                    "facts": None,
                }
                if ambiguous:
                    edges = fact_edges(view.model, revision)
                    pending.append((item, plan, edges))
                    targets.update(edge.target for edge in edges)
                items.append(item)
            exact = sum(1 for _, _, match in found if match.kind == "exact")
            counts.append({"text": text, "total": len(found), "exact": exact, "excluded": excluded})
            shown_ids += [revision.id for _, revision, _ in shown]
            total += len(found)
            more += plan.offset + len(shown) < len(found)
        read = view.snapshot.get_many(sorted(targets)) if targets else {}
        for item, plan, edges in pending:
            facts: list[JsonValue] = []
            for edge in edges:
                target = read.get(edge.target)
                facts.append(
                    {
                        "rel": str(edge.rel),
                        "id": str(edge.target),
                        "alias": None if target is None else target.alias,
                        "label": None if target is None else fact_label(view.model, plan, target),
                    }
                )
            item["facts"] = facts
        fact_ids = [edge.edge_id for _, _, edges in pending for edge in edges]
        self._signal(view, [*shown_ids, *(t for t in sorted(targets) if t in read)], fact_ids)
        page = Page(
            total, len(items), spec.limit, spec.offset, more=bool(more), first_page_only=narrow
        )
        data: dict[str, JsonValue] = {"items": items, "texts": counts}
        data["fit"] = (
            {"for": for_use, "for_source": for_source, "types": None if fit is None else list(fit)}
            if asked
            else None
        )
        return data, page

    @query_phase
    def search(self, view: GraphRead, spec: SearchSpec) -> tuple[dict[str, JsonValue], Page]:
        """Entities whose text fields match, best first; one item per entity."""
        plan = plan_search(view.model, spec, view.entry.language)
        found = []
        for revision in view.snapshot.query(plan.query).items:
            match = search_match(plan, revision)
            if match is not None:
                found.append((search_rank(plan, revision, match), revision, match))
        found.sort(key=lambda row: row[0])
        shown = found[plan.offset : plan.offset + plan.limit]
        items: list[JsonValue] = [
            {
                "id": str(revision.id),
                "alias": revision.alias,
                "type": revision.type,
                "record_status": revision.record_status,
                "label": search_label(plan, revision, match),
                "match": match.to_json(plan),
                "fields": list(match.fields),
            }
            for _, revision, match in shown
        ]
        self._signal(view, [revision.id for _, revision, _ in shown])
        return {"items": items}, Page(len(found), len(shown), plan.limit, plan.offset)
