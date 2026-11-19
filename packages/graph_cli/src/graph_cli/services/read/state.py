"""The state every read concern shares and the phase wrapper of reads answered from a snapshot."""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Concatenate

from graph_cli.access_control.terms import TermModel
from graph_cli.kernel.clock import Clock
from graph_cli.kernel.ids import EdgeId, EntityId, GraphId, IdProvider
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink, ReadSignal
from graph_cli.kernel.ports.audit import AuditSink
from graph_cli.kernel.ports.catalog import Catalog
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import EnvInfo, OntologyPin
from graph_cli.kernel.timing import Timings
from graph_cli.kernel.usage import UsageNote
from graph_cli.kinetic.action_needs import NeedOptions
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.params import Problem
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.services.action_rights import FineTypes
from graph_cli.services.models import ModelCache
from graph_cli.services.names import NameMapCache, NameMaps
from graph_cli.services.read.graph_read import GraphRead
from graph_cli.services.visibility import Visibility
from graph_cli.services.visible_catalog import HiddenCatalog

if TYPE_CHECKING:
    from graph_cli.kinetic.cover import SignatureCache
    from graph_cli.services.read.graph_read import ActionCatalog


def query_phase[S: ReadState, **P, R](
    method: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], R]:
    """A read answered from an open snapshot: the ``query`` phase."""

    @functools.wraps(method)
    def timed(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self.timings.phase("query"):
            return method(self, *args, **kwargs)

    return timed


class ReadState:
    """The collaborators and caches of a read service. The concerns build on it as mixins and
    none of them defines ``__init__``."""

    def __init__(
        self,
        *,
        catalog: Catalog,
        audit: AuditSink,
        policy: PolicySource,
        clock: Clock,
        ids: IdProvider,
        env: EnvInfo,
        activity: ActivitySink = NULL_ACTIVITY,
        timings: Timings | None = None,
        usage: UsageNote | None = None,
        name_maps: NameMapCache | None = None,
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self.activity = activity
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._env = env
        self.timings = timings if timings is not None else Timings()
        self.usage = usage if usage is not None else UsageNote()
        # Function signatures depend on the model they were derived from, so a limited reader
        # (a filtered model) never shares them with another; ``UNLIMITED``: the raw model.
        # Bounded like the other view caches (the oldest view goes first).
        self._signatures: dict[ReadView, SignatureCache] = {}
        self._models = ModelCache(catalog, self.timings)
        self._functions: dict[str, tuple[FunctionEntry, ...]] = {}  # compiled hash -> entries
        self._actions: dict[str, ActionCatalog] = {}  # compiled hash -> catalog
        self._views: dict[str, tuple[ViewEntry, ...]] = {}  # compiled hash -> entries
        # (views compiled hash, functions compiled hash, pin) -> problem of each view that no
        # longer fits the pin
        self._view_problems: dict[tuple[str, str | None, OntologyPin], dict[str, Problem]] = {}
        # (compiled hash, pin) -> problem of each action that no longer fits the pin
        self._action_problems: dict[tuple[str, OntologyPin], dict[str, Problem]] = {}
        # (compiled hash, pin) -> the needs of each action on concrete types (W9)
        self._action_needs: dict[tuple[str, OntologyPin], dict[str, tuple[NeedOptions, ...]]] = {}
        self._fine = FineTypes()
        self._visibility = Visibility()
        self._hidden = HiddenCatalog()
        self._terms: dict[tuple[GraphId, OntologyPin], TermModel] = {}  # per graph and pin
        self._names = NameMaps(catalog, audit, self._models, name_maps)  # read-time mapping, M7

    @property
    def models(self) -> ModelCache:
        """The pinned models this service loads, cached by their bytes; a write service of
        the same command may share them (``WriteService(models=...)``)."""
        return self._models

    def _signal(
        self,
        view: GraphRead,
        entities: Sequence[EntityId],
        edges: Sequence[EdgeId] = (),
        function: str | None = None,
    ) -> None:
        """Tell running views what this read returned (``[activity] signal_reads``)."""
        self.activity.reads(
            ReadSignal(view.entry.id, view.command, view.actor, entities, edges, function)
        )
