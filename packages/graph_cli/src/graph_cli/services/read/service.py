"""``ReadService``: the concerns of the read package composed into one class."""

from __future__ import annotations

from graph_cli.services.read.finding import ReadFinding
from graph_cli.services.read.history import ReadHistory
from graph_cli.services.read.installation import ReadInstallation
from graph_cli.services.read.opening import ReadOpen
from graph_cli.services.read.skills import ReadSkills
from graph_cli.services.read.views import ReadViews


class ReadService(ReadViews, ReadFinding, ReadHistory, ReadOpen, ReadSkills, ReadInstallation):
    """The reads of an installation: one method per read command, composed from the concerns.

    The constructor is ``ReadState.__init__``; the concerns form a chain below it (state,
    access, catalogs) and none defines state of its own: ``opening`` opens a graph for a
    read, ``entities`` (get, neighbors), ``finding`` (find, lookup, search), ``history``,
    ``queries`` (Cypher and named functions), ``views`` and ``skills`` answer from it, and
    ``installation`` holds the reads that name no graph (graphs, whoami)."""
