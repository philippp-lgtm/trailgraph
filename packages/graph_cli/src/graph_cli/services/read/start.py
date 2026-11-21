"""What ``graph start`` tells about a graph: how much of it the caller may use (plan S2).

One item per graph the caller reaches: its title, the caller's level, the number of modules and
types the caller sees, the number of active functions, actions and views it may use, and the number
it sees but may not use (only when there are any). Nothing is recorded: counting takes no decision,
like a listing. A hidden function, action or view is in no number (the classification never holds
one), so a limited reader learns nothing from the counts.
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.access_control.effective import graph_level
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.services.catalog_access import Denied, allowed_entries
from graph_cli.services.describe import describe_modules
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.installation import ReadInstallation


def _active(entries: Sequence[object]) -> int:
    return sum(1 for e in entries if not getattr(e, "deprecated", False))


class ReadStart(ReadInstallation, ReadCatalogs):
    def start_graphs(
        self, actor: ActorContext, entries: Sequence[GraphEntry]
    ) -> list[dict[str, JsonValue]]:
        """The ``start`` item of each of ``entries`` (graphs the caller sees), in order."""
        return [self._start_graph(actor, entry) for entry in entries]

    def reachable(
        self, actor: ActorContext, *, allowed: Sequence[str] | None
    ) -> tuple[list[GraphEntry], int]:
        """The graphs the caller sees, narrowed by the pointer allowlist (as ``graphs``), and
        the number of visible graphs the pointer hides."""
        visible = [e for e in self._listed() if self.visible(actor, e)]
        shown = [e for e in visible if allowed is None or e.name in allowed or str(e.id) in allowed]
        return shown, len(visible) - len(shown)

    def _start_graph(self, actor: ActorContext, entry: GraphEntry) -> dict[str, JsonValue]:
        item: dict[str, JsonValue] = {
            "name": entry.name,
            "title": entry.title,
            "level": graph_level(actor, entry.id, self._policy.current(entry.id)),
        }
        # the model, its skill and its catalogs are read with read.describe
        if not self._may(actor, entry, "read.describe"):
            item.update({"skill": None, "counts": None})
            return item
        item["skill"] = (
            graph_skill_name(entry.name) if self._catalog.read_skill(entry.id) is not None else None
        )
        try:
            model, _ = self._models.load_for_read(entry)
            limited = self._visibility.model(entry, model, self.read_view(actor, entry))
            modules, types = len(limited.modules), len(limited.classes)
        except GraphCliError:
            item["counts"] = None  # a model that cannot be read tells nothing; start still answers
            return item
        functions = self.classified_functions(actor, entry)
        published = self.published_actions(entry)
        actions = () if published is None else self.classified_actions(actor, entry, published)
        views = self.classified_views(actor, entry)
        item["counts"] = {
            "modules": modules,
            "types": types,
            "functions": _active([c.entry for c in functions if c.allowed]),
            "actions": _active([c.entry for c in actions if c.allowed]),
            "views": _active([c.entry for c in views if c.allowed]),
        }
        denied = Denied.of(functions, actions, views)
        refused = {
            kind: n
            for kind, n in (
                ("functions", _active([c.entry for c in denied.functions])),
                ("actions", _active([c.entry for c in denied.actions])),
                ("views", _active([c.entry for c in denied.views])),
            )
            if n
        }
        if refused:
            item["not_allowed"] = dict(refused)
        return item

    def start_modules(self, actor: ActorContext, entry: GraphEntry) -> list[dict[str, JsonValue]]:
        """The modules of one graph with what the caller may use of each: id, summary and the
        counts of ``describe modules`` (what the caller may not use is counted per kind in
        ``not_allowed``). Empty without ``read.describe`` or when the model cannot be read."""
        if not self._may(actor, entry, "read.describe"):
            return []
        try:
            model, _ = self._models.load_for_read(entry)
            limited = self._visibility.model(entry, model, self.read_view(actor, entry))
        except GraphCliError:
            return []
        functions = self.classified_functions(actor, entry)
        published = self.published_actions(entry)
        actions = () if published is None else self.classified_actions(actor, entry, published)
        views = self.classified_views(actor, entry)
        denied = Denied.of(functions, actions, views)
        described = describe_modules(
            entry,
            limited,
            allowed_entries(functions) or None,
            allowed_entries(actions) or None,
            allowed_entries(views) or None,
            denied,
        )
        keep = ("id", "summary", "types", "functions", "actions", "views", "not_allowed")
        return [
            {key: m[key] for key in keep if key in m}
            for m in (as_object(v, "module") for v in req_list(described, "modules"))
        ]
