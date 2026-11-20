"""Cypher queries and the named functions run over a graph."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kinetic.bind import bind_cli
from graph_cli.kinetic.entries import FunctionEntry, find_entry, seen_replacement
from graph_cli.query.cypher.values import Value
from graph_cli.query.plan import DEFAULT_LIMIT
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.graph_read import GraphRead, Page
from graph_cli.services.read.json_shapes import edges_in, entities_in, value_json
from graph_cli.services.refs import entity_params
from graph_cli.services.visibility import SIGNATURE_VIEWS, remember

if TYPE_CHECKING:
    from graph_cli.kinetic.cover import Cover
    from graph_cli.query.cypher.ast import Query as CypherQuery
    from graph_cli.query.cypher.execute import Result as QueryResult
    from graph_cli.query.cypher.resolve import ResolvedQuery


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """How to answer a parsed Cypher query: its parameters, the paging of its rows."""

    parameters: Mapping[str, Value] = field(default_factory=dict)
    include_inactive: bool = False
    explain: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    budget: int | None = None  # None: the engine's default (``execute.DEFAULT_BUDGET``)


class ReadQueries(ReadCatalogs):
    def parse_query(self, text: str) -> CypherQuery:
        """The syntax tree of a Cypher query; needs no graph, so it runs before ``open``."""
        from graph_cli.query.cypher.parser import parse

        with self.timings.phase("parse"):
            return parse(text)

    def query(
        self, view: GraphRead, query: CypherQuery, spec: QuerySpec
    ) -> tuple[dict[str, JsonValue], Page | None, tuple[Cover, ...]]:
        """A parsed query resolved against the graph's model and, unless ``explain``,
        run over its snapshot; the rows are paged like ``find``. The third item is the
        functions the caller may run that cover the query, best first (usage shapes U2,
        U5); the shape and the first three are noted for the access log."""
        from graph_cli.query.cypher.resolve import resolve

        with self.timings.phase("resolve"):
            resolved = resolve(query, view.model, spec.parameters)
        found = self._note_shape(view, resolved)
        if spec.explain:
            return {"plan": resolved.explain()}, None, found
        data, page = self._rows(
            view, resolved, spec.include_inactive, spec.budget, spec.limit, spec.offset
        )
        return data, page, found

    def _note_shape(self, view: GraphRead, resolved: ResolvedQuery) -> tuple[Cover, ...]:
        """Note the query's shape for the access log and find the functions that cover it,
        over the functions the caller may see only: a hidden function is never hinted or
        logged. Before the query runs, so a failing execution still logs its shape."""
        from graph_cli.kinetic.cover import SignatureCache, covers, query_signature
        from graph_cli.query.cypher.shape import shape

        with self.timings.phase("shape"):
            found: tuple[Cover, ...] = ()
            visible = self.functions(view.actor, view.entry)
            if visible:
                signatures = self._signatures.get(view.read_view)
                if signatures is None:
                    signatures = SignatureCache()
                    remember(self._signatures, view.read_view, signatures, SIGNATURE_VIEWS)
                pairs = signatures.pairs(visible, view.model)
                found = covers(query_signature(resolved), pairs, view.model)
            described = shape(resolved)
            self.usage.query = {
                "shape_hash": described.hash,
                "shape": described.text,
                "covered_by": [{"name": c.function, "list_fit": c.list_fit} for c in found[:3]],
            }
        return found

    def _rows(
        self,
        view: GraphRead,
        resolved: ResolvedQuery,
        include_inactive: bool,
        budget: int | None,
        limit: int,
        offset: int,
        function: str | None = None,
    ) -> tuple[dict[str, JsonValue], Page]:
        """A resolved query run over the snapshot, one page of its rows, and the activity
        signal for what it read."""
        result = self._execute(view, resolved, include_inactive, budget)
        shown = result.rows[offset : offset + limit]
        items: list[JsonValue] = [
            {column: value_json(value) for column, value in zip(result.columns, row, strict=True)}
            for row in shown
        ]
        # What the rows show first, then what the matches went through: a query that
        # returns only values or counts lights up the entities it read too.
        entities = dict.fromkeys([*entities_in(shown), *result.touched])
        edges = dict.fromkeys([*edges_in(shown), *result.walked])
        self._signal(view, list(entities), list(edges), function)
        data: dict[str, JsonValue] = {"columns": list(result.columns), "items": items}
        return data, Page(len(result.rows), len(shown), limit, offset)

    def _execute(
        self,
        view: GraphRead,
        resolved: ResolvedQuery,
        include_inactive: bool,
        budget: int | None,
    ) -> QueryResult:
        """A resolved query run over the reader's snapshot and model, every row of it."""
        from graph_cli.query.cypher.execute import DEFAULT_BUDGET, execute

        with self.timings.phase("execute"):
            return execute(
                resolved,
                view.model,
                view.snapshot,
                include_inactive=include_inactive,
                budget=DEFAULT_BUDGET if budget is None else budget,
            )

    def _resolve_function(
        self, view: GraphRead, function: FunctionEntry, bound: Mapping[str, Value]
    ) -> ResolvedQuery:
        """The stored query text of a function parsed and resolved against the reader's model
        with the bound values: text that no longer does is ``function.stale``."""
        from graph_cli.query.cypher.resolve import resolve

        try:
            parsed = self.parse_query(function.query)
            with self.timings.phase("resolve"):
                return resolve(parsed, view.model, bound)
        except GraphCliError as exc:
            raise GraphCliError(
                "function.stale",
                f"function {function.name} no longer fits the graph's ontology ({exc.code}); "
                "tell the maintainer of this graph",
                details={"function": function.name, "hash": function.hash, "cause": exc.code},
            ) from exc

    def run(
        self,
        view: GraphRead,
        name: str,
        pairs: Sequence[tuple[str, str]],
        *,
        limit: int,
        offset: int,
    ) -> tuple[dict[str, JsonValue], Page, tuple[WarningRecord, ...]]:
        """A published function run with ``name=value`` parameters (plan K2, K3, K5, K8).

        The caller is authorized (``read.function``, by ``open``). Values are read by the
        declared types, a ``ref`` is resolved and its class checked, and the stored
        normalized query text is parsed and resolved against the current pin: text that no
        longer does is ``function.stale``. The rows are paged and signalled like ``query``;
        the activity signal names the function, never a value."""
        visible = self._visible_functions(view.entry, view.read_view)
        function = find_entry(visible, name)
        self.usage.function = {"name": name, "hash": function.hash}
        warnings: list[WarningRecord] = []
        if function.deprecated:
            replaced = seen_replacement(function.defn.replaced_by, visible)
            warnings.append(
                WarningRecord(
                    "function.deprecated",
                    f"function {name} is deprecated"
                    + (f"; use {replaced}" if replaced is not None else ""),
                    {"name": name, "replaced_by": replaced},
                )
            )
        with self.timings.phase("query"):
            bound = bind_cli(name, function.defn.params, pairs, view.model)
            for param in function.defn.params:
                if param.type == "ref":
                    bound[param.name], found = entity_params(
                        view.snapshot, view.model, "function", name, param, bound
                    )
                    warnings += found
        resolved = self._resolve_function(view, function, bound)
        data, page = self._rows(view, resolved, False, None, limit, offset, name)
        data = {"function": {"name": name, "hash": function.hash}, **data}
        return data, page, tuple(warnings)
