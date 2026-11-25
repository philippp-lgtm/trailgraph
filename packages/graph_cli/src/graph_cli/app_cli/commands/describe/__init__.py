"""The ``graph describe`` commands except ``describe commands`` (contracts B11).

``model`` holds the ontology commands, ``kinetic`` the function, action and view commands,
``tools`` the tool manifest and ``shared`` what they use.
"""

from __future__ import annotations

from graph_cli.app_cli.commands.describe import kinetic, model, tools
from graph_cli.cli_kit.spec import CommandSpec

SPECS: tuple[CommandSpec, ...] = (*model.SPECS, *kinetic.SPECS, *tools.SPECS)
