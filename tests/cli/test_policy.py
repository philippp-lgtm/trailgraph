"""S: installed policies in both programs (contracts B6.13-B6.16, B5.15, B12.6).

Toy graphs and the neutral project names ``example_project`` and
``another_project`` only.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.policies import GRAPH, INSTALLATION, write_policy

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditStream, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def test_an_invalid_installed_policy_fails_closed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    assert app(run_cli, "graphs").code == 0
    policy = root / "policy" / "installation.json"
    policy.parent.mkdir()
    policy.write_text('{"schema": "graph_cli.policy/1", "rules": "everything"}\n')
    for args in (("graphs",), ("whoami",), ("find", "--graph", "example_graph")):
        outcome = app(run_cli, *args)
        assert outcome.code == 5, (args, outcome.envelope)
        assert outcome.error["code"] == "policy.invalid"
        assert outcome.error["details"]["file"] == "policy/installation.json"
    # graph-admin decides without the broken file, so the maintainer can replace it.
    listed = admin(run_cli, "graph", "list")
    assert listed.code == 0, listed.envelope
    assert "admin.policy_fallback" in listed.warning_codes


def apply(run_cli: CliRunner, path: Path, *args: str) -> Outcome:
    return admin(run_cli, "policy", "apply", str(path), *args, "--reason", "test policy")


def events(root: Path, stream: str) -> list[dict[str, Any]]:
    log = FileAuditLog(root).read_lines(AuditStream(stream))
    report = verify_stream(log, stream=AuditStream(stream))
    assert report.findings == ()
    return [dict(event) for event in report.events]


def test_check_show_diff_and_apply_the_installation_policy(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    checked = admin(run_cli, "policy", "check", str(path))
    assert checked.code == 0, checked.envelope
    assert (checked.data["layer"], checked.data["graph"]) == ("installation", None)
    shown = admin(run_cli, "policy", "show").data
    assert (shown["installed"], shown["document"]["id"]) == (False, "builtin_default")
    diff = admin(run_cli, "policy", "diff", str(path)).data
    assert diff["changed"] and diff["to"] == checked.data["hash"]
    assert "builtin.allow_app" in diff["rules"]["removed"]
    assert "grant.1" in diff["rules"]["added"]
    applied = apply(run_cli, path)
    assert applied.code == 0, applied.envelope
    assert applied.data == {
        "layer": "installation",
        "graph": None,
        "from": None,
        "to": checked.data["hash"],
        "applied": True,
    }
    shown = admin(run_cli, "policy", "show").data
    assert (shown["installed"], shown["hash"]) == (True, checked.data["hash"])
    again = apply(run_cli, path)
    assert again.data["applied"] is False  # the installed document: nothing recorded
    changes = [
        e for e in events(root, INSTALLATION_STREAM) if e["actions"] == ["maint.policy_change"]
    ]
    assert len(changes) == 2  # the intent, then the confirmation after the switch
    assert changes[0]["change"] == {"policy": {"from": None, "to": checked.data["hash"]}}
    assert "change" not in changes[1]
    assert changes[0]["reason"] == "test policy"
    assert changes[0]["authz"]["decisions"][0]["resource"] == "installation"
    assert admin(run_cli, "audit", "verify").code == 0


def test_graph_policy_is_a_graph_scoped_change(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    graph_id = create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "work", "example_graph.yaml", GRAPH)
    missing = admin(run_cli, "policy", "check", str(path))
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")
    applied = apply(run_cli, path, "--graph", "example_graph")
    assert applied.code == 0, applied.envelope
    intent = events(root, graph_stream(graph_id))[-1]
    assert intent["actions"] == ["maint.policy_change"]
    assert intent["change"]["policy"]["to"] == applied.data["to"]
    companion = events(root, INSTALLATION_STREAM)[-1]
    assert companion["anchors"][0]["seq"] == intent["seq"]
    assert "change" not in companion
    shown = admin(run_cli, "policy", "show", "--graph", "example_graph").data
    assert shown["document"]["types"] == {"inv:Location": "read"}
    assert admin(run_cli, "audit", "verify").code == 0
    installation = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    wrong = admin(run_cli, "policy", "check", str(installation), "--graph", "example_graph")
    assert wrong.error["code"] == "usage.invalid_argument"


def test_unknown_types_warn(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "work", "g.yaml", GRAPH.replace("inv:Location", "files:File"))
    checked = admin(run_cli, "policy", "check", str(path), "--graph", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["admin.policy_unknown_type"]


WRITES_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: notes and topics"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph, other_graph]
    level: read
    writes:
      nas:Note:
        create: [title]
        update: [body, status, nope]
        link: [nas:broader]
        transition: [title]
      nas:Scan: {create: [scanned_on]}
      inv:Item: {update: true}
no_project: {"*": read}
"""


def test_writes_are_checked_against_the_model_of_each_graph(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """``writes`` grants warn per graph they name: a type its model lacks, a term its type
    lacks and a write that cannot succeed; the policy is still valid and compiles as written."""
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    create_graph(run_cli, "other_graph")  # inventory: no nas:* types
    path = write_policy(tmp_path / "work", "installation.yaml", WRITES_POLICY)
    checked = admin(run_cli, "policy", "check", str(path))
    assert checked.code == 0, checked.envelope
    assert sorted(set(checked.warning_codes)) == [
        "admin.policy_unknown_term",
        "admin.policy_unknown_type",
        "admin.policy_unusable_write",
    ]
    by_code: dict[str, list[dict[str, Any]]] = {}
    for warning in checked.warnings:
        by_code.setdefault(warning["code"], []).append(warning["details"])
    assert by_code["admin.policy_unknown_type"] == [
        {"graph": "example_graph", "type": "inv:Item"},
        {"graph": "other_graph", "type": "nas:Note"},
        {"graph": "other_graph", "type": "nas:Scan"},
    ]
    assert by_code["admin.policy_unknown_term"] == [
        {"graph": "example_graph", "type": "nas:Note", "term": "nas:broader", "kind": "relation"},
        {"graph": "example_graph", "type": "nas:Note", "term": "nope", "kind": "attribute"},
        {"graph": "example_graph", "type": "nas:Note", "term": "title", "kind": "lifecycle"},
    ]
    assert by_code["admin.policy_unusable_write"] == [
        {
            "graph": "example_graph",
            "type": "nas:Note",
            "verb": "create",
            "attributes": ["body", "kind"],
        },
        {
            "graph": "example_graph",
            "type": "nas:Note",
            "verb": "update",
            "attributes": ["status"],
        },
        {
            "graph": "example_graph",
            "type": "nas:Scan",
            "verb": "create",
            "attributes": ["content", "file_name", "media_type", "title"],
        },
    ]
    unknown_term = next(w for w in checked.warnings if w["code"] == "admin.policy_unknown_term")
    assert "fail closed" in unknown_term["message"]
    scan = next(
        w for w in checked.warnings if "nas:Scan" in w["message"] and "create" in w["message"]
    )
    assert "graph file add" in scan["message"]
    # the same warnings come with an explanation against the file
    explained = admin(run_cli, "policy", "explain", "--graph", "example_graph", "--file", str(path))
    assert explained.code == 0, explained.envelope
    assert explained.warning_codes == checked.warning_codes
    # it applies: warnings never block
    assert apply(run_cli, path).code == 0


def test_writes_on_an_abstract_type_are_refused(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    create_graph(run_cli, "other_graph")
    source = WRITES_POLICY.replace("nas:Scan: {create: [scanned_on]}", "nas:Source: {create: true}")
    checked = admin(
        run_cli,
        "policy",
        "check",
        str(write_policy(tmp_path / "work", "installation.yaml", source)),
    )
    assert checked.error["code"] == "policy.invalid"
    problems = checked.error["details"]["problems"]
    assert [p["path"] for p in problems] == ["grants[0].writes.nas:Source"]
    assert "abstract or has subclasses in graph example_graph" in problems[0]["message"]


def test_a_policy_without_writes_has_no_model_warnings(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    checked = admin(run_cli, "policy", "check", str(path))
    assert (checked.code, checked.warning_codes) == (0, [])


def test_graph_term_deny_rules_warn_about_unknown_terms(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    source = (
        "schema: graph_cli.policy/1\nid: graph\ndeny:\n"
        "  - {id: no_body, type: [nas:Note], attribute: [body, nope], action: [write.update]}\n"
        "  - {id: no_link, type: [nas:Gone], relation: [nas:about], action: [write.link]}\n"
    )
    path = write_policy(tmp_path / "work", "g.yaml", source)
    checked = admin(run_cli, "policy", "check", str(path), "--graph", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["admin.policy_unknown_type", "admin.policy_unknown_term"]
    assert checked.warnings[0]["details"] == {"graph": "example_graph", "type": "nas:Gone"}
    assert checked.warnings[1]["details"] == {
        "graph": "example_graph",
        "type": "nas:Note",
        "term": "nope",
        "kind": "attribute",
    }


def test_unknown_roles_in_a_graph_policy_warn(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A graph policy is applied independently: a role the installed installation policy
    does not declare is a warning (``policy.role_unknown``), a declared one is not."""
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    work = tmp_path / "work"
    graph = write_policy(
        work,
        "g.yaml",
        GRAPH + "deny:\n  - {id: importer_no_retract, role: [importer], action: [write.retract]}\n",
    )
    checked = admin(run_cli, "policy", "check", str(graph), "--graph", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["policy.role_unknown"]
    assert checked.warnings[0]["details"] == {"roles": ["importer"]}
    assert "matches only tokens that still hold it" in checked.warnings[0]["message"]
    declared = INSTALLATION.replace(
        "grants:",
        'roles:\n  importer: {description: "Importers"}\ngrants:',
    )
    installed = write_policy(work, "installation.yaml", declared)
    assert apply(run_cli, installed).code == 0
    again = admin(run_cli, "policy", "check", str(graph), "--graph", "example_graph")
    assert (again.code, again.warning_codes) == (0, []), again.envelope
    shown = admin(run_cli, "policy", "show").data
    assert shown["document"]["roles"] == {"importer": {"description": "Importers"}}


def test_dropping_a_role_that_is_still_used_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """``admin.role_orphaned``: one warning per dropped role that a token holds or an
    installed graph policy names; a dropped role nothing uses does not warn."""
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    work = tmp_path / "work"
    declared = INSTALLATION.replace(
        "grants:",
        'roles:\n  importer: {description: "Importers"}\n  reader: {description: "Readers"}\n'
        '  spare: {description: "Unused"}\n  keep: {description: "Kept"}\ngrants:',
    )
    assert apply(run_cli, write_policy(work, "installation.yaml", declared)).warnings == []
    graph = write_policy(
        work,
        "g.yaml",
        GRAPH + "deny:\n  - {id: importer_no_retract, role: [importer], action: [write.retract]}\n",
    )
    assert apply(run_cli, graph, "--graph", "example_graph").warnings == []
    for name, role in (("a", "importer"), ("b", "importer"), ("c", "reader")):
        assert admin(run_cli, "token", "create", name, "--role", role).code == 0

    narrowed = declared.replace('  importer: {description: "Importers"}\n', "")
    narrowed = narrowed.replace('  reader: {description: "Readers"}\n', "")
    narrowed = narrowed.replace('  spare: {description: "Unused"}\n', "")  # keep stays
    dropped = apply(run_cli, write_policy(work, "narrow.yaml", narrowed))
    assert dropped.code == 0, dropped.envelope
    by_role = {w["details"]["role"]: w for w in dropped.warnings}
    assert [w["code"] for w in dropped.warnings] == ["admin.role_orphaned"] * 2
    assert by_role["importer"]["details"] == {
        "role": "importer",
        "tokens": ["a", "b"],
        "graphs": ["example_graph"],
    }
    assert by_role["reader"]["details"] == {"role": "reader", "tokens": ["c"], "graphs": []}
    assert "spare" not in by_role
    # the membership stays, and applying the same policy again warns of nothing new
    assert apply(run_cli, write_policy(work, "narrow.yaml", narrowed)).warnings == []
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_type_limit_on_a_superclass_is_refused(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """``nas:Source`` is abstract with subclasses; a limit on it would hold for none of them."""
    create_graph(run_cli, "example_graph", "notes_and_sources")
    work = tmp_path / "work"
    source = write_policy(work, "source.yaml", GRAPH.replace("inv:Location", "nas:Source"))
    refused = admin(run_cli, "policy", "check", str(source), "--graph", "example_graph")
    assert (refused.code, refused.error["code"]) == (5, "policy.invalid")
    assert [p["path"] for p in refused.error["details"]["problems"]] == ["types.nas:Source"]
    page = write_policy(work, "page.yaml", GRAPH.replace("inv:Location", "nas:WebPage"))
    checked = admin(run_cli, "policy", "check", str(page), "--graph", "example_graph")
    assert (checked.code, checked.warning_codes) == (0, []), checked.envelope


def test_invalid_policy_lists_every_problem(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    broken = INSTALLATION.replace('no_project: {"*": read}\n', "")
    path = write_policy(tmp_path / "work", "installation.yaml", broken)
    checked = admin(run_cli, "policy", "check", str(path))
    assert (checked.code, checked.error["code"]) == (5, "policy.invalid")
    paths = {p["path"] for p in checked.error["details"]["problems"]}
    assert {"no_project", "scopes.example_project.other_graph"} <= paths  # no other_graph here
    assert not (root / "policy").exists()


def test_apply_refuses_a_lockout(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    locked = INSTALLATION + "maintainers:\n  - {name: somebody_else}\n"
    path = write_policy(tmp_path / "work", "installation.yaml", locked)
    refused = apply(run_cli, path)
    assert (refused.code, refused.error["code"]) == (1, "admin.policy_lockout")
    assert refused.error["details"] == {"rule": "default.deny"}
    assert not (root / "policy").exists()
    assert not [
        e for e in events(root, INSTALLATION_STREAM) if "maint.policy_change" in e["actions"]
    ]


def test_explain(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", INSTALLATION)
    before = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--file", str(path))
    assert before.code == 0, before.envelope
    assert (before.data["scope"], before.data["level"]) == ("no_project", "read")
    in_project = admin(
        run_cli,
        "policy",
        "explain",
        "--graph",
        "other_graph",
        "--project",
        "example_project",
        "--action",
        "write.create",
        "--type",
        "inv:Item",
        "--file",
        str(path),
    )
    decision = in_project.data["decisions"]
    assert decision == [
        {
            "action": "write.create",
            "effect": "deny",
            "rule": "scope.example_project.other_graph",
            "code": "access.denied",
            "obligations": None,
        }
    ]
    unknown = admin(
        run_cli,
        "policy",
        "explain",
        "--graph",
        "example_graph",
        "--project",
        "unknown_project",
        "--file",
        str(path),
    )
    assert unknown.data["scope"] == "no_project"
    assert unknown.warning_codes == ["access.project_unknown"]
    installed = admin(run_cli, "policy", "explain", "--graph", "example_graph")
    assert (installed.data["scope"], installed.data["level"]) == (None, "write")  # built-in
    assert installed.data["withheld"] == []


EXPLAIN_ROLES = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps"}
  importer: {description: "Importers"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: "*", level: read}
  - {entrypoint: server, principal: {role: importer}, graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {role: importer, project: example_project}, graphs: "*",
     level: write}
no_project: {"*": write}
deny:
  - {id: importer_no_retract, role: [importer], action: [write.retract]}
"""


def test_explain_for_roles(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """``policy explain --role``: a server token that holds the roles, the project verified;
    an undeclared role warns, a policy file that declares it does not."""
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    path = write_policy(tmp_path / "work", "installation.yaml", EXPLAIN_ROLES)

    def explain(graph: str, *args: str, file: bool = True) -> Outcome:
        extra = ("--file", str(path)) if file else ()
        return admin(run_cli, "policy", "explain", "--graph", graph, *args, *extra)

    reader = explain("example_graph", "--role", "reader", "--action", "write.create")
    assert reader.code == 0, reader.envelope
    assert reader.warnings == []  # the file declares the role
    assert (reader.data["roles"], reader.data["level"]) == (["reader"], "read")
    (decision,) = reader.data["decisions"]
    assert (decision["effect"], decision["rule"]) == ("deny", "default.deny")
    both = explain("example_graph", "--role", "reader", "--role", "importer", "--role", "reader")
    assert both.data["roles"] == ["importer", "reader"]  # sorted, unique
    create = explain("example_graph", "--role", "importer", "--action", "write.create")
    (decision,) = create.data["decisions"]
    assert (decision["effect"], decision["rule"]) == ("allow", "grant.2")
    retract = explain("example_graph", "--role", "importer", "--action", "write.retract")
    (decision,) = retract.data["decisions"]
    assert (decision["effect"], decision["rule"]) == ("deny", "deny.importer_no_retract")
    # a role grant for one project only: the project is verified like a bound token's
    on_other = ("other_graph", "--role", "importer", "--action", "write.create")
    bound = explain(*on_other, "--project", "example_project")
    unbound = explain(*on_other)
    assert bound.data["project"] == "example_project"
    assert [d["effect"] for d in bound.data["decisions"]] == ["allow"]
    assert [(d["effect"], d["rule"]) for d in unbound.data["decisions"]] == [
        ("deny", "default.deny")
    ]
    assert explain("other_graph", "--project", "example_project").data["roles"] == []

    installed = explain("example_graph", "--role", "reader", "--role", "typo", file=False)
    assert installed.code == 0, installed.envelope  # still explained
    assert installed.warning_codes == ["policy.role_unknown"]
    assert installed.warnings[0]["details"] == {"roles": ["reader", "typo"]}
    assert installed.data["roles"] == ["reader", "typo"]
    assert explain("example_graph", "--role", "typo").warning_codes == ["policy.role_unknown"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--role", "reader", "--token", "ci"), 2),
        (("--role", "Not A Role"), 2),
        (("--role", "gct_" + "a" * 40), 2),
    ],
)
def test_explain_refuses_unusable_role_arguments(
    run_cli: CliRunner, root: Path, args: tuple[str, ...], code: int
) -> None:
    create_graph(run_cli, "example_graph")
    refused = admin(run_cli, "policy", "explain", "--graph", "example_graph", *args)
    assert refused.code == code and refused.error["code"] == "usage.invalid_argument"
    assert refused.error["details"]["argument"] in {"role", "--role"}
    assert "gct_" not in json.dumps(refused.envelope)


# ---- the app under an installed policy (B6.15, B2.10, Q3-Q5) -----------------------

NO_EXPLORE_HISTORY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_history, agent_type: [explore], action: ["read.history"]}
"""


def test_a_deny_rule_withholds_history(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """B8.15: history is part of read; a deny rule on read.history takes only it away."""
    create_graph(run_cli, "example_graph")
    assert apply(run_cli, write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_HISTORY)).code == 0
    made = app(run_cli, *LOCATION, "--graph", "example_graph")
    assert made.code == 0, made.envelope
    entity = made.data["entities"][0]["id"]
    owner = app(run_cli, "history", entity, "--graph", "example_graph")
    assert owner.code == 0, owner.envelope
    explore = {
        "GRAPH_CLI_AGENT_ID": "agent-1",
        "GRAPH_CLI_AGENT_TYPE": "explore",
        "GRAPH_CLI_GRAPH": "example_graph",
    }
    for args in (
        ("history", entity),
        ("get", entity, "--rev", "1"),
        ("find", "--as-of", "tx:1"),
        ("get", entity, "--as-of", "tx:1"),
    ):
        refused = app(run_cli, *args, env=explore)
        assert (refused.code, refused.error["code"]) == (3, "access.denied"), args
        assert refused.error["details"]["action"] == "read.history"
        assert refused.error["details"]["rule"] == "deny.no_explore_history"
    assert app(run_cli, "get", entity, env=explore).code == 0  # current state stays readable
    who = app(run_cli, "whoami", env=explore).data["permissions"]["graphs"]
    assert [(g["level"], g["withheld"]) for g in who] == [("none", ["read.history"])]
    recorded = [d["authz"]["decisions"][0]["action"] for d in denials(root)[-4:]]
    assert recorded == ["read.history"] * 4


POINTER = 'format_version = 1\nproject = "{project}"\ngraphs = ["example_graph", "other_graph"]\n'
LOCATION = ("create", "inv:Location", "--set", "name=Shelf", "--set", "code=S1")


@pytest.fixture
def scoped(run_cli: CliRunner, root: Path, tmp_path: Path) -> dict[str, str]:
    """Two graphs and the toy installation policy applied; graph name -> id."""
    ids = {name: create_graph(run_cli, name) for name in ("example_graph", "other_graph")}
    assert apply(run_cli, write_policy(tmp_path / "work", "i.yaml", INSTALLATION)).code == 0
    return ids


def project_dir(tmp_path: Path, project: str) -> Path:
    directory = tmp_path / project
    directory.mkdir()
    (directory / ".graph_cli.toml").write_text(POINTER.format(project=project))
    return directory


def denials(root: Path) -> list[dict[str, Any]]:
    return [e for e in events(root, INSTALLATION_STREAM) if e["result"]["status"] == "denied"]


def test_from_a_project_with_a_scope(
    run_cli: CliRunner, root: Path, tmp_path: Path, scoped: dict[str, str]
) -> None:
    here = project_dir(tmp_path, "example_project")
    listed = app(run_cli, "graphs", cwd=here)
    assert [g["name"] for g in listed.data["items"]] == ["example_graph", "other_graph"]
    who = app(run_cli, "whoami", cwd=here).data["permissions"]
    assert who["policy"] == "installation" and who["scope"] == "example_project"
    assert [(g["graph"], g["level"]) for g in who["graphs"]] == [
        ("example_graph", "write"),
        ("other_graph", "read"),
    ]
    written = app(run_cli, *LOCATION, "--graph", "example_graph", cwd=here)
    assert written.code == 0, written.envelope
    refused = app(run_cli, *LOCATION, "--graph", "other_graph", cwd=here)
    assert (refused.code, refused.error["code"]) == (3, "access.denied")
    assert refused.error["details"]["rule"] == "scope.example_project.other_graph"
    assert refused.error["hint"] == "graph whoami"
    assert "graph-admin" not in refused.error["message"]
    assert denials(root)[-1]["actor"]["project"]["id"] == "example_project"


def test_an_invisible_graph_does_not_exist_for_the_caller(
    run_cli: CliRunner, root: Path, tmp_path: Path, scoped: dict[str, str]
) -> None:
    here = project_dir(tmp_path, "another_project")  # sees other_graph only
    listed = app(run_cli, "graphs", cwd=here)
    assert [g["name"] for g in listed.data["items"]] == ["other_graph"]
    assert listed.data["hidden_count"] == 0  # policy-hidden graphs are never counted
    hidden = app(run_cli, "find", "--graph", "example_graph", cwd=here)
    missing = app(run_cli, "find", "--graph", "no_such_graph", cwd=here)
    assert (hidden.code, hidden.error["code"]) == (1, "graph.not_found")
    assert hidden.error["message"] == missing.error["message"].replace(
        "no_such_graph", "example_graph"
    )
    assert hidden.error["details"] == {"ref": "example_graph"}
    by_id = app(run_cli, "find", "--graph", scoped["example_graph"], cwd=here)
    assert by_id.error["code"] == "graph.not_found"
    recorded = denials(root)[-1]
    assert recorded["authz"]["decisions"][0]["action"] == "read.list"
    assert recorded["graph"]["name"] == "example_graph"  # the maintainer sees what happened
    unselected = app(run_cli, "find", cwd=here)
    assert unselected.error["details"]["candidates"] == ["other_graph"]


@pytest.mark.parametrize("project", [None, "unknown_project"], ids=["no_pointer", "unknown"])
def test_without_a_scope_the_no_project_scope_applies(
    run_cli: CliRunner, root: Path, tmp_path: Path, scoped: dict[str, str], project: str | None
) -> None:
    here = project_dir(tmp_path, project) if project is not None else tmp_path
    listed = app(run_cli, "graphs", cwd=here)
    assert [g["name"] for g in listed.data["items"]] == ["example_graph", "other_graph"]
    expected = ["access.project_unknown"] if project is not None else []
    assert listed.warning_codes == expected
    assert app(run_cli, "find", "--graph", "example_graph", cwd=here).code == 0
    refused = app(run_cli, *LOCATION, "--graph", "example_graph", cwd=here)
    assert refused.error["details"]["rule"] == "no_project.*"
    who = app(run_cli, "whoami", cwd=here).data["permissions"]
    assert who["scope"] == "no_project"
    assert {g["level"] for g in who["graphs"]} == {"read"}


def test_graph_policy_limits_writes_per_type(
    run_cli: CliRunner, root: Path, tmp_path: Path, scoped: dict[str, str]
) -> None:
    applied = apply(
        run_cli, write_policy(tmp_path / "work", "g.yaml", GRAPH), "--graph", "example_graph"
    )
    assert applied.code == 0
    here = project_dir(tmp_path, "example_project")
    refused = app(run_cli, *LOCATION, "--graph", "example_graph", cwd=here)
    assert refused.error["details"]["rule"] == "type.inv:Location"
    assert (
        app(run_cli, "find", "--graph", "example_graph", "--type", "inv:Location", cwd=here).code
        == 0
    )
    graphs = app(run_cli, "whoami", cwd=here).data["permissions"]["graphs"]
    first = graphs[0]
    assert first["graph"] == "example_graph"
    assert first["types"] == {"inv:Location": "read"}
    assert first["obligations"]["write.create"] == {"max_batch": 50}
    explorer = {"GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
    blocked = app(
        run_cli,
        "create",
        "inv:Category",
        "--set",
        "name=Tools",
        "--graph",
        "example_graph",
        cwd=here,
        env=explorer,
    )
    assert blocked.error["details"]["rule"] == "deny.no_explore_writes"
    assert admin(run_cli, "audit", "verify").code == 0
