"""The ``company`` example served by graph-server in-process, for the acceptance client of the tool
manifest: the graph with its seeds, every function, action and view of the example plus the
fixtures a test needs on top (a function that takes a list and a date, set views), and three
callers: ``maintainer`` writes, ``reader`` only reads (every action is visible but not allowed to
it), ``narrow`` reads a few types only (the entries that name other types are hidden from it) and
``catalog`` works through two entries (a function and an entity view) and holds nothing else.
"""

import io
import shutil
from dataclasses import dataclass
from pathlib import Path

from harness import CliRunner

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment
from support.admin import admin, init
from support.app import app
from support.fixtures import EXAMPLES, FIXTURES
from support.functions import VALID as FUNCTION_FIXTURES
from support.policies import write_policy
from support.server import new_token

GRAPH = "company"
COMPANY = EXAMPLES / "company"
FIXTURE_VIEWS = ("unit_overview", "members_report", "tasks_by_state", "legacy_unit_overview")
# The concrete types of the modules a catalog caller reads: a catalog grant lists exact types, so
# that a type a later release adds never widens it
ORGANIZATION = ("org:Department", "org:Team", "org:Position", "org:Role", "org:CostCenter")
PEOPLE = ("ppl:Employee", "ppl:External", "ppl:Identity")
OPERATIONS = ("ops:Process", "ops:Instruction", "ops:Policy", "ops:Training", "ops:Task")
CATALOG_READS = (*ORGANIZATION, *PEOPLE, *OPERATIONS)
NARROW_TYPES = ("ppl:Employee", "ppl:External", "org:Department", "org:Team", "org:Position")

POLICY = f"""\
schema: graph_cli.policy/1
id: installation
roles:
  maintainer: {{description: "Reads and writes the graph"}}
  reader: {{description: "Reads the graph"}}
  narrow: {{description: "Reads some types of the graph"}}
  catalog: {{description: "Works through one function and one view only"}}
grants:
  - {{entrypoint: app, graphs: "*", level: write}}
  - {{entrypoint: server, principal: {{role: maintainer}}, graphs: [{GRAPH}], level: write}}
  - {{entrypoint: server, principal: {{role: reader}}, graphs: [{GRAPH}], level: read}}
  - entrypoint: server
    principal: {{role: narrow}}
    graphs: [{GRAPH}]
    level: read
    reads: [{", ".join(NARROW_TYPES)}]
  - entrypoint: server
    principal: {{role: catalog}}
    graphs: [{GRAPH}]
    level: catalog
    catalog: {{functions: [members_of_unit], views: [unit_overview]}}
    reads: [{", ".join(CATALOG_READS)}]
scopes:
  company_project: {{{GRAPH}: write}}
no_project: {{"*": none}}
"""


@dataclass(frozen=True)
class Installation:
    server: ServerApp
    tokens: dict[str, str]  # token name -> bearer token
    root: Path  # the data root, for tests that read the audit trail


EXTERNALS_BY_KIND = """\
schema: graph_cli.function/1
name: externals_by_kind
module: people
summary: The external people of one kind, with the end of their engagement.
use_when: Which contractors, consultants or auditors work with the company and until when.
params:
  kind:
    type: enum
    enum: ppl:ExternalKind
    summary: Why the person works with the company.
query: |
  MATCH (e:External)
  WHERE e.kind = $kind
  RETURN e.name AS name, e.kind AS kind, e.engaged_until AS engaged_until
  ORDER BY name
returns:
  - column: name
    summary: Name of the person.
  - column: kind
    summary: The kind asked for.
  - column: engaged_until
    summary: Date the engagement ends, if it has an end.
examples:
  - question: Which consultants work with the company?
"""


def publish_set(run_cli: CliRunner, tmp_path: Path) -> None:
    """The functions, actions and views of the example, with what a test needs on top:
    ``training_completions`` (a list and a date) and the views of the fixture folder that the
    example does not have (a set view with a reference parameter, one with defaults, a deprecated
    one, an entity view of a unit), and ``externals_by_kind`` (an enum), written here."""
    functions, views = tmp_path / "functions", tmp_path / "views"
    shutil.copytree(COMPANY / "functions", functions)
    shutil.copy(FUNCTION_FIXTURES / "training_completions.yaml", functions)
    (functions / "externals_by_kind.yaml").write_text(EXTERNALS_BY_KIND)
    shutil.copytree(COMPANY / "views", views)
    for name in FIXTURE_VIEWS:
        shutil.copy(FIXTURES / "views" / "company" / "valid" / f"{name}.yaml", views)
    for kind, folder in (
        ("functions", functions),
        ("actions", COMPANY / "actions"),
        ("views", views),
    ):
        published = admin(run_cli, "graph", kind, "publish", GRAPH, str(folder))
        assert published.code == 0, published.envelope


ROLE_TOKENS: dict[str, tuple[str, str]] = {
    role: (role, "company_project") for role in ("maintainer", "reader", "narrow", "catalog")
}


def build(
    run_cli: CliRunner,
    isolated_env: dict[str, str],
    tmp_path: Path,
    *,
    policy: str = POLICY,
    tokens: dict[str, tuple[str, str]] | None = None,
) -> Installation:
    """The example graph the way ``examples/company/load.sh`` builds it, served with a token per
    caller. ``policy`` is the installation policy; ``tokens`` maps a token name to its role and
    project (default: one token per role of ``POLICY``, named like the role)."""
    root = init(run_cli, isolated_env)
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    created = admin(
        run_cli,
        *("graph", "create", GRAPH),
        *("--ontology", str(COMPANY / "ontology" / "1.0.0"), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    env = {"GRAPH_CLI_GRAPH": GRAPH}
    applied = app(run_cli, "apply", str(COMPANY / "seeds" / "1.0.0.jsonl"), env=env)
    assert applied.code == 0, applied.envelope
    released = admin(
        run_cli,
        *("ontology", "release", GRAPH, str(COMPANY / "ontology" / "1.1.0")),
        *("--module", "company"),
    )
    assert released.code == 0, released.envelope
    applied = app(run_cli, "apply", str(COMPANY / "seeds" / "1.1.0.jsonl"), env=env)
    assert applied.code == 0, applied.envelope
    publish_set(run_cli, tmp_path)
    path = write_policy(tmp_path / "policies", "installation.yaml", policy)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "serve the example")
    assert applied.code == 0, applied.envelope
    issued = {
        name: new_token(run_cli, f"app_{name}", "--project", project, "--role", role)
        for name, (role, project) in (tokens or ROLE_TOKENS).items()
    }
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    return Installation(server, issued, root)
