"""Shared by the read commands: the error list of an entity reference, the stdin marker,
opening the graph for a command and the parameters several commands take."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH_ERRORS, context
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.catalog_access import EntryPath
from graph_cli.services.read import GraphRead
from graph_cli.temporal_knowledge.as_of import as_of_text, parse_as_of

REF_ERRORS: Final = (
    *GRAPH_ERRORS,
    "entity.not_found",
    "entity.ambiguous_ref",
    "temporal.as_of_unsupported",
)
STDIN: Final = "-"


def open_graph(
    state: CommandState,
    graph: str | None,
    action: str,
    command: str,
    args: dict[str, JsonValue],
    as_of: AsOf | None,
    via: str | None = None,
    through: EntryPath | Callable[[GraphEntry], EntryPath | None] | None = None,
) -> GraphRead:
    """The graph opened for ``command``. ``through`` names the catalog entries it works through
    (or tells them from the graph once it is selected): a caller whose rights on the graph do
    not allow ``action`` may still be allowed through an entry its grants name."""
    ctx = context(state)
    entry = ctx.select(graph)
    if as_of is not None:
        args = {**args, "as_of": as_of_text(as_of)}
    request = RequestMeta.of(command, {"graph": entry.name, **args}, via=via)
    path = through(entry) if callable(through) else through
    view = ctx.reader.open(ctx.actor, entry, action, request, as_of=as_of, through=path)
    state.warnings += view.warnings
    return view


def as_of_flag(text: str | None) -> AsOf | None:
    """``--as-of`` parsed before the graph is opened (B8.8)."""
    return None if text is None else parse_as_of(text)


def pinned(view: GraphRead, as_of: AsOf | None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``--as-of tx:<seq>`` for a next-page command: the same state, also after a timestamp."""
    return () if as_of is None else (("--as-of", (f"tx:{view.snapshot.seq}",)),)


FIELDS: Final = ParamSpec(
    "--fields",
    "string",
    "Only these fields (comma-separated): attributes, edges or system fields;"
    " id, alias, type, rev and record_status always.",
    repeatable=True,
)
FULL: Final = ParamSpec(
    "--full",
    "flag",
    "Also the system fields of how each entity was stored (created_at, recorded_at, tx_seq,"
    " ontology_version, content_hash) and the edge ids, for unlink --edge-id.",
)


REF: Final = ParamSpec(
    "ref", "entity_ref", "Full id, alias or trailing hex of the id.", required=True, positional=True
)
