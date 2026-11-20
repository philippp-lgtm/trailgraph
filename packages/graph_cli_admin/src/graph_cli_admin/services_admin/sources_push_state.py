"""The records the lock-time phases of ``graph push`` share.

Under the lock a push reads the published slots (``Current``), decides which
folders it publishes (``Publishing``), plans the release and gathers all of it
in the frame the checks run against (``Locked``: the graph as the release
leaves it, the workspace, the slots, the plan), and keeps the catalogs it
checked (``Catalogs``). They are frozen: a phase reads them and builds the
next one, nothing is changed afterwards.

``sources_push`` creates them in its phases and passes them on,
``sources_push_answer`` reads them for the answer; the workspace they hold is
read by ``sources_push_read``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SkillSlot
from graph_cli.kernel.records import OntologyPin
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin import release
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.sources_push_read import EMPTY, Workspace


@dataclass(frozen=True, slots=True)
class Current:
    """The slots a graph holds under the lock; ``None``: nothing published."""

    skill: SkillSlot | None
    functions: KineticSlot | None
    actions: KineticSlot | None
    views: KineticSlot | None

    @property
    def skill_hash(self) -> str | None:
        return self.skill.hash if self.skill is not None else None

    @property
    def functions_hash(self) -> str:
        return self.functions.hash if self.functions is not None else EMPTY

    @property
    def actions_hash(self) -> str:
        return self.actions.hash if self.actions is not None else EMPTY

    @property
    def views_hash(self) -> str:
        return self.views.hash if self.views is not None else EMPTY


@dataclass(frozen=True, slots=True)
class Publishing:
    """What a push really writes: a kind is published when its folder differs from the base
    and from what the graph holds; and the names each catalog will have."""

    functions: bool
    actions: bool
    views: bool
    # One namespace per graph: each kind is checked against the names of the other kind the
    # graph will have (the pushed files if the push publishes them, else the published).
    function_names: frozenset[str]
    action_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class Locked:
    """What the checks of a push under the lock read: the graph as the release leaves it
    (``after``, before any write), the workspace, the published slots and the release plan."""

    ctx: AdminContext
    entry: GraphEntry  # re-read under the lock
    after: GraphEntry  # the entry with the pin the release will set; the writes return the real one
    ws: Workspace
    current: Current
    publishes: Publishing
    planned: release.Planned | None
    pending: Hop | None  # the release as the last hop of the name map, for the rename hints
    releasing: bool  # the release changes the graph: its pin moves

    @classmethod
    def of(
        cls,
        ctx: AdminContext,
        entry: GraphEntry,
        ws: Workspace,
        current: Current,
        publishes: Publishing,
        planned: release.Planned | None,
        target_pin: OntologyPin,
    ) -> Locked:
        releasing = planned is not None and not planned.unchanged
        after = replace(entry, pin=target_pin) if releasing else entry
        # The functions the graph will have must resolve against the ontology it will have.
        pending = _pending_hop(entry, planned) if releasing else None
        return cls(
            ctx=ctx,
            entry=entry,
            after=after,
            ws=ws,
            current=current,
            publishes=publishes,
            planned=planned,
            pending=pending,
            releasing=releasing,
        )

    @property
    def publishing_skill(self) -> bool:
        return self.ws.skill.changed and self.current.skill_hash != self.ws.skill.hash

    def model(self) -> CompiledModel:
        """The model the graph will have: the compiled one for a release, else the pinned one
        (read on each call: once per kind, as the checks run)."""
        if self.releasing:
            return self.ws.compiled.model
        return pinned_model(self.ctx.catalog, self.after)


@dataclass(frozen=True, slots=True)
class Catalogs:
    """The catalogs a push publishes (``None``: not published), checked, and what the
    warnings read of the checks afterwards."""

    functions: KineticSlot | None
    actions: KineticSlot | None
    views: KineticSlot | None
    action_model: CompiledModel | None  # the model the pushed actions were checked against
    view_functions: tuple[FunctionEntry, ...]  # the functions the pushed views were checked with


def _pending_hop(entry: GraphEntry, planned: release.Planned | None) -> Hop | None:
    """The release a push makes, as the last step of the graph's name map: the rename hints
    of the functions and actions it publishes name its version (migration plan M7)."""
    if planned is None or planned.old is None:
        return None
    old, new = planned.old.model, planned.compiled.model
    run = planned.migration
    renames = run.renames if run is not None else derive_renames(old, new)
    doc = run.migration.doc if run is not None and run.migration is not None else None
    return Hop(entry.pin.version, planned.compiled.pin.version, old, new, renames, doc)
