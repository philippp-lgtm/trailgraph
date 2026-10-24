"""Client work around the hooked commands of the remote mode (contracts B14.10).

Filled in by the hooked commands; a command without a hook is passed through.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from graph_cli.app_cli.remote import Hook

HOOKS: Final[Mapping[tuple[str, ...], Hook]] = {}
