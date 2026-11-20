"""``graph get``, ``graph view``, ``graph history``, ``graph changes``, ``graph find``,
``graph lookup``, ``graph search``, ``graph neighbors``, ``graph query`` and ``graph run``.

Contracts §4.3, B8.8, B8.12, B8.15, B9.7, B13 (Cypher); ``run``: named functions.

One module per command group: ``entity`` (``get``, ``view``), ``history`` (``history``,
``changes``), ``find`` (``find``, ``lookup``, ``search``, ``neighbors``) and ``query``
(``query``, ``run``); ``shared`` holds what several of them use. The order of ``SPECS`` is
the order of the help, the generated skill and the inventory.
"""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.read import entity, find, history, query

SPECS: Final = (
    *entity.SPECS,
    *history.SPECS,
    *find.SPECS,
    *query.SPECS,
)
