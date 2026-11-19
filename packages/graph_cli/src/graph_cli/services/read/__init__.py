"""Read services: graphs, get, history, changes, find, lookup, search, neighbors, query, whoami
(contracts §4.3, B2.10, B6.3, B6.12, B8.8, B8.12, B8.13, B8.15, B9.7, B13).

Every read authorizes its action before touching storage; a denial is
appended to the installation stream and raised. A graph is visible iff
``read.list`` on it is allowed; a denial on a graph the actor cannot see is
raised as ``graph.not_found``, exactly like a graph that does not exist
(B6.15), and still audited. Reads write nothing else.
A read takes one lock-free snapshot and answers everything from it, so the
envelope's ``graph.seq`` names the state that was read. A pinned artifact
that does not match its hash is read with ``ontology.pin_mismatch_read``
(refused when it shows markings: only the pin vouches for them), an archived
graph with ``graph.archived_read``. ``as_of`` reads the snapshot
at the seq it names (B8.8): it needs ``read.history`` besides the read's own
action, a backend with ``snapshot_at_seq``, and every answer carries
``temporal.as_of_read``; the model stays the current pin, and revisions written under
an older major are shown under its names (``services.names``, migration plan M7; current
revisions never are). A successful ``get``,
``history``, ``find``, ``lookup``, ``search``, ``neighbors`` or ``query`` hands the
entities it returns, and the edges ``neighbors`` walked or ``query`` rows hold, to the
activity sink with the command and actor that opened the graph (a transient signal for
``graph-admin view``, no log).
``history`` shows of a tx's actor only whether it is the caller's own
session: who wrote a revision is audit content (B8.15). ``changes`` (the change
feed) is the merged history of every entity the caller sees, in tx order, and
signals no entities (a poller would flood the views' trails).
The command's timings get the phases ``authorize``, ``audit`` (a denial),
``model``, ``snapshot`` and ``query`` (answering from the snapshot); a
Cypher ``query`` has ``parse``, ``resolve`` and ``execute`` instead.
The Cypher engine is imported by ``parse_query`` and ``query`` only: every
other command starts without it.

The code is split by concern, each in its own module, and ``ReadService`` (in ``service``)
is the composition of them: ``state`` holds what they share, ``access`` the authorization,
``catalogs`` the published functions, actions and views, and ``opening``, ``entities``,
``finding``, ``history``, ``queries``, ``views``, ``skills`` and ``installation`` the reads.
The modules import each other by their own paths, never this package; this file only
re-exports the public names.
"""

from __future__ import annotations

from graph_cli.services.read.access import denial_error
from graph_cli.services.read.change_feed import change_page
from graph_cli.services.read.entities import refuse_affordances_with_rev
from graph_cli.services.read.graph_read import ActionCatalog, GraphRead, Page, graph_json
from graph_cli.services.read.json_shapes import edge_json, entity_json
from graph_cli.services.read.queries import QuerySpec
from graph_cli.services.read.service import ReadService

__all__ = [
    "ActionCatalog",
    "GraphRead",
    "Page",
    "QuerySpec",
    "ReadService",
    "change_page",
    "denial_error",
    "edge_json",
    "entity_json",
    "graph_json",
    "refuse_affordances_with_rev",
]
