"""The ``generic_env`` identity provider: harness-neutral ``GRAPH_CLI_*`` variables (B2.6)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.catalog.identity.facets import Supplied, asserted_facet

NAME: Final = "generic_env"
AGENT_ID: Final = "GRAPH_CLI_AGENT_ID"
AGENT_TYPE: Final = "GRAPH_CLI_AGENT_TYPE"
SESSION_ID: Final = "GRAPH_CLI_SESSION_ID"
PARENT_SESSION_ID: Final = "GRAPH_CLI_PARENT_SESSION_ID"


def facets(env: Mapping[str, str]) -> dict[str, Supplied | None]:
    return {
        "agent": asserted_facet(
            env, id_var=AGENT_ID, type_var=AGENT_TYPE, source=f"env:{AGENT_ID}"
        ),
        "session": asserted_facet(
            env, id_var=SESSION_ID, parent_var=PARENT_SESSION_ID, source=f"env:{SESSION_ID}"
        ),
    }
