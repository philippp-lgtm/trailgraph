"""A ``company`` graph for the tests of ``lookup --for`` and of the lookup tool: its functions,
actions and views, and a department and a team of the same name, so what a lookup keeps shows the
class a slot takes (``members_report`` takes a department, ``unit_overview`` any unit and
``legacy_unit_overview`` a team).

``employer_of`` is a view that shows one department and is named like a function of the graph: the
name alone means the function, the kind says the view.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from harness import CliRunner

from support.admin import admin, init
from support.app import app
from support.fixtures import EXAMPLES, FIXTURES, example_model
from support.functions import VALID as FUNCTION_FIXTURES
from support.services import OpsFactory

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
FACTORY = OpsFactory(example_model("company", "1.1.0"))
NAME = "Alpha"
CLASHING_VIEW = """\
schema: graph_cli.view/1
name: employer_of
module: organization
summary: One department, named like the function that finds the employer of a person.
use_when: Show a department; a view may be named like a function.
takes: org:Department
sections:
  - kind: fields
    fields: [name]
"""


def named(cls: str, local: str) -> list[dict[str, Any]]:
    """Ops that create ``cls`` as ``@local`` named ``NAME`` (and what it needs)."""
    ops = FACTORY.create(cls, local)
    for op in ops:
        if op.get("local") == local:
            op["attrs"]["name"] = NAME
    return ops


def build(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """``example_graph``: the company ontology, its entries and a department and a team named
    ``NAME``. Returns the data root."""
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(EXAMPLES / "company" / "ontology" / "1.1.0")),
        *("--module", "company"),
    )
    assert created.code == 0, created.envelope
    functions, views = tmp_path / "functions", tmp_path / "views"
    shutil.copytree(EXAMPLES / "company" / "functions", functions)
    shutil.copy(FUNCTION_FIXTURES / "training_completions.yaml", functions)
    shutil.copytree(FIXTURES / "views" / "company" / "valid", views)
    (views / "employer_of.yaml").write_text(CLASHING_VIEW)
    for kind, folder in (
        ("functions", functions),
        ("actions", EXAMPLES / "company" / "actions"),
        ("views", views),
    ):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    ops = [*named("org:Department", "dept"), *named("org:Team", "team")]
    team = next(op for op in ops if op.get("local") == "team")
    team["edges"] = [*team.get("edges", []), {"rel": "org:part_of", "target": "@dept"}]
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    return root
