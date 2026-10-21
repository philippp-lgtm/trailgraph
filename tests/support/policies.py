"""Authored policies for the S and T tests: toy graphs, neutral project names."""

from pathlib import Path

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
scopes:
  example_project: {example_graph: write, other_graph: read}
  another_project: {other_graph: write}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""

GRAPH = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Location": read
obligations:
  - {action: ["write.*"], max_batch: 50}
"""


def write_policy(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
