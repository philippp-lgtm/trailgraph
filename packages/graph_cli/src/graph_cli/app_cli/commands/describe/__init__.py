"""The ``graph describe`` commands except ``describe commands`` (contracts B11)."""

from __future__ import annotations

from graph_cli.app_cli.commands.describe import kinetic
from graph_cli.cli_kit.spec import CommandSpec

SPECS: tuple[CommandSpec, ...] = kinetic.SPECS
