"""Names of installed skills shared by the installer and the freshness check.

``graph_cli.skills`` installs skills and writes their marker;
``graph_cli.catalog.skill_claims`` reads the markers back to judge whether a
copy is stale (contracts B14.17). Neither layer may import the other, so the
names both rely on live here.
"""

from __future__ import annotations

from typing import Final

SKILLS_DIR: Final = "skills"  # below an agent configuration directory
MARKER_FILE: Final = ".graph_cli_skill.json"
MARKER_SCHEMA: Final = "graph_cli.skill_install/1"
APP_SKILL: Final = "graph-cli"
GRAPH_SKILL_PREFIX: Final = "graph-"
STATIC: Final = "static"  # slot kinds: the files are the skill as is
GENERATED: Final = "generated"  # a template, rendered with the model of the graph's pin


def graph_skill_name(graph_name: str) -> str:
    """``graph-<graph>`` with ``_`` -> ``-`` (P1.5 Q5 naming rule)."""
    return GRAPH_SKILL_PREFIX + graph_name.replace("_", "-")
