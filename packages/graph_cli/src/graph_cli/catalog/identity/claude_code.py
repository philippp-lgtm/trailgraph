"""The ``claude_code`` identity provider (contracts B2.6).

The variable names are harness internals, not a documented contract, so
they stay private to this adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.catalog.identity.facets import Supplied, asserted_facet

NAME: Final = "claude_code"
_SESSION_VARS: Final = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
_AGENT_ID: Final = "CLAUDE_AGENT_ID"
_AGENT_TYPE: Final = "CLAUDE_AGENT_TYPE"


def facets(env: Mapping[str, str]) -> dict[str, Supplied | None]:
    session = next(
        (
            asserted_facet(env, id_var=variable, source=NAME)
            for variable in _SESSION_VARS
            if env.get(variable)
        ),
        None,
    )
    return {
        "agent": asserted_facet(env, id_var=_AGENT_ID, type_var=_AGENT_TYPE, source=NAME),
        "session": session,
    }
