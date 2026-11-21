"""The inputs of a graph's skill: its documents, model and the digest of what it shows."""

from __future__ import annotations

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.json_access import req_list
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.kernel.skill_featured import catalog_digest, kind_digest
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.describe import describe_model
from graph_cli.services.read.catalogs import ReadCatalogs


class ReadSkills(ReadCatalogs):
    def skill(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> SkillSlot | None:
        """The graph's published skill (D15 Q1): a description of the graph, so
        ``read.describe`` on ``graph:<id>``; ``None`` when none is published."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        return self._catalog.read_skill(entry.id)

    def skill_model(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> tuple[dict[str, JsonValue], tuple[WarningRecord, ...]]:
        """The model documents a generated graph skill is rendered with: those of the
        graph's current pin, as ``graph describe`` prints them (``read.describe``), plus the
        compiled function catalog and the action catalog when the graph has them. The function
        names and summaries are schema-level text like the type names, but a skill block is a
        listing of what the actor may use: it holds the functions only when the actor may
        ``read.function`` (else the document is ``withheld``, without names) and the actions
        the actor may run (actions plan A6, as ``actions``); an actor who may run none gets the
        documents of a graph without actions. The rest of the catalog document is kept as
        published, so the renderer sees one shape. For an actor who sees only some types (type
        read limits plan V7) the documents are those of its model view and hold the functions
        and actions that fit it, so the skill names nothing the actor cannot see."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        view = self.read_view(actor, entry)
        if view.limited:
            model = self._visibility.model(entry, model, view)
        return self._skill_documents(actor, entry, model, view, limits=True), warnings

    def skill_names_model(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> dict[str, JsonValue] | None:
        """The documents of ``skill_model`` as they are without the actor's read limits, to
        check the curated text of a template against the full model (``skills.sources``): text
        that names a hidden type is the maintainer's, and a problem about it would name the
        type to the reader. ``None`` when the actor has no limit (``skill_model`` is the full
        model then). The documents are used for the check only, never rendered."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        if not self.read_view(actor, entry).limited:
            return None
        model, _ = self._models.load_for_read(entry)
        return self._skill_documents(actor, entry, model, UNLIMITED, limits=False)

    def skill_digest(self, actor: ActorContext, entry: GraphEntry) -> str | None:
        """A digest of what the actor sees (``ReadView.digest``), recorded in the claim of an
        installed graph skill so a policy change makes the copy stale; ``None`` for an actor
        without a read limit, whose claims stay as they were. Without an attribute half it is
        the digest of the visible types, as before markings existed."""
        return self.read_view(actor, entry).digest

    def skill_featured(self, actor: ActorContext, entry: GraphEntry) -> str:
        """The digest of what a generated skill shows of the graph's catalogs for ``actor``
        (``kernel.skill_featured``), recorded in the claim of an installed copy so a change of
        the featured entries or of what the graph has makes it stale.

        A caller without read limits and with every marking who may ``read.function`` sees the
        whole catalog: the digest stored with it, read from three small files. For any other
        caller it is computed from exactly the documents its render uses (the featured entries
        it sees, the presence of what it may use; whether functions are withheld from it), so
        a publish that only changes what it cannot see leaves its copy fresh, no digest of
        hidden content reaches it, and a change of its rights that changes the render makes the
        copy stale. The marker at install, the freshness check of a command and ``skills
        check`` all take this value, for the same actor. Derived, no access control.

        The shortcut for the unlimited caller ignores the rights that decide which actions it
        may run, on purpose (plan S6): a change of its write rights moves no stored digest, and
        only the comparison of files by ``graph start`` and ``skills check`` sees the
        difference in what its render lists."""
        view = self.read_view(actor, entry)
        if not view.limited and not self._function_denials(actor, entry):
            return self._catalog.skill_featured(entry.id)
        functions, actions, views = self._skill_catalogs(actor, entry, view, limits=True)
        return catalog_digest(
            kind_digest("functions", functions),
            kind_digest("actions", actions),
            kind_digest("views", views),
        )

    def _skill_documents(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        model: CompiledModel,
        view: ReadView,
        *,
        limits: bool,
    ) -> dict[str, JsonValue]:
        """``describe_model`` of ``model`` with the compiled function catalog (the functions
        that fit a reader with ``view``) and the action catalog narrowed to the
        actions ``actor`` may run (within its read limits when ``limits``)."""
        functions, actions, views = self._skill_catalogs(actor, entry, view, limits=limits)
        return describe_model(entry, model, functions, actions, views)

    def _skill_catalogs(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView, *, limits: bool
    ) -> tuple[JsonObject | None, JsonObject | None, JsonObject | None]:
        """The compiled function, action and view catalogs as the render of ``actor`` sees them
        (``None``: nothing to list). Functions: those that fit ``view``; with ``limits`` and no
        ``read.function`` none, and the document says ``withheld`` so the block can say that
        functions are not for this token, but only for a reader that sees at least one active
        function: one whose view hides them all gets ``None`` and one that sees only deprecated
        ones the catalog as it is, whatever its rights. Actions: those ``actor``
        may run. Views: those it may see."""
        found = self._published_slot(entry)
        functions: JsonObject | None = None
        if found is not None and found[1].count:
            # What the reader's view hides goes first, the right second: a reader that sees no
            # function must not learn through ``read.function`` that the graph has any.
            document: JsonObject | None = found[1].compiled
            if view.limited:
                hidden = self._hidden_functions(entry, found, view)
                kept = [
                    e
                    for e in req_list(found[1].compiled, "functions")
                    if isinstance(e, dict) and e.get("name") not in hidden
                ]
                document = {**found[1].compiled, "functions": kept} if kept else None
            if document is not None:
                # Only an active function counts as seen (the render and the digest skip
                # deprecated ones): a reader that sees none gets the same document with or
                # without the right.
                sees = any(
                    isinstance(e, dict) and e.get("status") != "deprecated"
                    for e in req_list(document, "functions")
                )
                if sees and limits and self._function_denials(actor, entry):
                    document = {**document, "functions": [], "withheld": True}
            functions = document
        return (
            functions,
            self._visible_catalog(actor, entry, limits=limits),
            self._views_catalog(actor, entry, view),
        )

    def _views_catalog(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView
    ) -> JsonObject | None:
        """The compiled view catalog narrowed to the views ``actor`` may see, as the published
        document; ``None`` when there are none (a graph skill then has no views clause)."""
        visible = self._visible_views(actor, entry, view)
        found = self._published_views(entry)
        if not visible or found is None:
            return None
        names = {v.name for v in visible}
        document = found[1].compiled
        kept = [
            e for e in req_list(document, "views") if isinstance(e, dict) and e.get("name") in names
        ]
        return {**document, "views": kept}

    def _visible_catalog(
        self, actor: ActorContext, entry: GraphEntry, *, limits: bool = True
    ) -> JsonObject | None:
        """The compiled action catalog narrowed to the actions ``actor`` may see, as the
        published document (same schema, entries as compiled); ``None`` when there are none."""
        published = self.published_actions(entry)
        if published is None:
            return None
        names = {a.name for a in self.visible_actions(actor, entry, published, limits=limits)}
        if not names:
            return None
        kept = [
            e
            for e in req_list(published.compiled, "actions")
            if isinstance(e, dict) and e.get("name") in names
        ]
        return {**published.compiled, "actions": kept}
