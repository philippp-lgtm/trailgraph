"""S: markings in the installation policy and what a release says about them (field visibility
plan F2, F3, step 2): ``policy check`` against the pinned model, and the warnings of ``ontology
release``, ``ontology diff --graph`` and ``graph push``. Graph and markings are those of the
``personnel`` series."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.fixtures import ONTOLOGIES
from support.policies import write_policy

PERSONNEL = ONTOLOGIES / "personnel"

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of people"}
grants:
  - entrypoint: server
    principal: {role: hr}
    graphs: [example_graph]
    level: read
    markings: [%s]
%s
no_project: {"*": read}
"""
WRITES = """\
    writes:
      ex:Person:
        create: [name, email]
        update: [email, team]
"""


def policy(markings: str = "ex:personal", writes: str = "") -> str:
    return POLICY % (markings, writes)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(PERSONNEL / "1.0.0")
    )
    assert created.code == 0, created.envelope
    graph_id: str = created.data["id"]
    return graph_id


def check(run_cli: CliRunner, tmp_path: Path, text: str) -> Outcome:
    path = write_policy(tmp_path / "work", "installation.yaml", text)
    return admin(run_cli, "policy", "check", str(path))


def apply(run_cli: CliRunner, tmp_path: Path, text: str) -> Outcome:
    path = write_policy(tmp_path / "work", "installation.yaml", text)
    return admin(run_cli, "policy", "apply", str(path), "--reason", "test policy")


def details(outcome: Outcome, code: str) -> list[dict[str, Any]]:
    return [w["details"] for w in outcome.warnings if w["code"] == code]


def release_dir(tmp_path: Path, number: str, edit: dict[str, str] | None = None) -> Path:
    """The 1.0.0 module as ``number`` with ``edit`` applied (old text -> new text)."""
    text = (PERSONNEL / "1.0.0" / "personnel.yaml").read_text()
    text = text.replace("version: 1.0.0", f"version: {number}")
    for old, new in (edit or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    target = tmp_path / number
    target.mkdir()
    (target / "personnel.yaml").write_text(text)
    return target


# ---- policy check -------------------------------------------------------------------------


def test_a_policy_that_grants_the_markings_in_use_is_clean(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    checked = check(run_cli, tmp_path, policy("ex:personal", WRITES))
    assert checked.code == 0, checked.envelope
    assert checked.warnings == []
    assert apply(run_cli, tmp_path, policy("ex:personal", WRITES)).code == 0


def test_an_undeclared_marking_warns_and_still_applies(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    text = policy("ex:personal, ex:team_internal")  # 1.0.0 declares only ex:personal
    checked = check(run_cli, tmp_path, text)
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["admin.policy_unknown_marking"]
    assert details(checked, "admin.policy_unknown_marking") == [
        {"graph": "example_graph", "marking": "ex:team_internal"}
    ]
    assert "declares no marking ex:team_internal" in checked.warnings[0]["message"]
    assert apply(run_cli, tmp_path, text).code == 0  # warnings never block


def test_a_write_list_naming_a_marked_attribute_without_its_marking_warns(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    no_marking = policy("ex:personal", WRITES).replace("    markings: [ex:personal]\n", "")
    checked = check(run_cli, tmp_path, no_marking)
    assert checked.code == 0, checked.envelope
    assert checked.warning_codes == ["admin.policy_unusable_write"] * 2
    assert details(checked, "admin.policy_unusable_write") == [
        {
            "graph": "example_graph",
            "type": "ex:Person",
            "verb": "create",
            "attributes": ["email"],
            "markings": ["ex:personal"],
        },
        {
            "graph": "example_graph",
            "type": "ex:Person",
            "verb": "update",
            "attributes": ["email"],
            "markings": ["ex:personal"],
        },
    ]
    message = checked.warnings[0]["message"]
    assert "does not hold ex:personal" in message and "rights add up" in message
    # explain compiles the file with the same checks
    explained = admin(
        run_cli,
        "policy",
        "explain",
        "--graph",
        "example_graph",
        "--file",
        str(tmp_path / "work" / "installation.yaml"),
    )
    assert explained.code == 0, explained.envelope
    assert explained.warning_codes == checked.warning_codes


def test_markings_need_named_graphs(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    text = policy().replace("graphs: [example_graph]", 'graphs: "*"')
    refused = check(run_cli, tmp_path, text)
    assert (refused.code, refused.error["code"]) == (5, "policy.invalid")
    problems = refused.error["details"]["problems"]
    assert [p["path"] for p in problems] == ["grants[0].graphs"]
    assert "markings belong to an ontology" in problems[0]["message"]


# ---- the release ----------------------------------------------------------------------------


def test_a_release_warns_about_markings_no_installed_grant_names(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    assert apply(run_cli, tmp_path, policy("ex:personal")).code == 0
    released = admin(run_cli, "ontology", "release", "example_graph", str(PERSONNEL / "1.1.0"))
    assert released.code == 0, released.envelope
    [found] = details(released, "admin.marking_ungranted")
    assert found == {
        "graph": "example_graph",
        "marking": "ex:team_internal",
        "count": 1,
        "attributes": ["ex:Person.remark"],
    }
    assert "only maintainer surfaces show them" in released.warnings[-1]["message"]
    # naming it silences the warning on the next release
    assert apply(run_cli, tmp_path, policy("ex:personal, ex:team_internal")).code == 0
    patch = release_dir(tmp_path, "1.1.1")
    text = (PERSONNEL / "1.1.0" / "personnel.yaml").read_text()
    (patch / "personnel.yaml").write_text(
        text.replace("version: 1.1.0", "version: 1.1.1").replace("Full name.", "The full name.")
    )
    again = admin(run_cli, "ontology", "release", "example_graph", str(patch))
    assert again.code == 0, again.envelope
    assert "admin.marking_ungranted" not in again.warning_codes


def test_without_an_installation_policy_the_warning_says_no_app_caller_reads_them(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    released = admin(run_cli, "ontology", "release", "example_graph", str(PERSONNEL / "1.1.0"))
    assert released.code == 0, released.envelope
    found = details(released, "admin.marking_ungranted")
    assert [d["marking"] for d in found] == ["ex:personal", "ex:team_internal"]
    message = next(w["message"] for w in released.warnings if w["code"].endswith("ungranted"))
    assert "no installation policy is installed" in message


def test_a_release_that_drops_a_granted_marking_warns(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    released = admin(run_cli, "ontology", "release", "example_graph", str(PERSONNEL / "1.1.0"))
    assert released.code == 0, released.envelope
    assert apply(run_cli, tmp_path, policy("ex:personal, ex:team_internal")).code == 0
    # 2.0.0 drops the declaration of ex:team_internal
    dropped = admin(run_cli, "ontology", "release", "example_graph", str(PERSONNEL / "2.0.0"))
    assert dropped.code == 0, dropped.envelope
    assert details(dropped, "admin.policy_unknown_marking") == [
        {"graph": "example_graph", "marking": "ex:team_internal"}
    ]
    assert "admin.marking_ungranted" not in dropped.warning_codes


RULE = """
rules:
  - id: email_needs_team
    summary: A person with an e-mail address has a team.
    severity: warning
    when: write
    scope: Person
    if:
      - {attr: email, value_presence: PRESENT}
    then:
      - {attr: team, value_presence: PRESENT}
"""
TEAM = "      team:\n        summary: Name of the team the person works in.\n        type: string\n"
PHONE = (
    "      phone:\n        summary: Phone number.\n        type: string\n"
    "        required: true\n        markings: [ex:personal]\n"
)
REQUIRED = {TEAM: TEAM + PHONE}


def with_rule(tmp_path: Path, number: str, edit: dict[str, str] | None = None) -> Path:
    source = release_dir(tmp_path, number, edit)
    with (source / "personnel.yaml").open("a") as handle:
        handle.write(RULE)
    return source


def test_a_rule_over_a_marked_attribute_and_a_required_marked_attribute_warn(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    assert apply(run_cli, tmp_path, policy("ex:personal")).code == 0
    source = with_rule(tmp_path, "2.0.0", REQUIRED)
    released = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert released.code == 0, released.envelope
    [expression] = details(released, "admin.marking_in_expression")
    assert expression == {
        "graph": "example_graph",
        "kind": "rule",
        "name": "ex.email_needs_team",
        "attributes": ["ex:Person.email"],
        "markings": ["ex:personal"],
    }
    assert details(released, "admin.marking_blocks_create") == [
        {
            "graph": "example_graph",
            "type": "ex:Person",
            "attribute": "phone",
            "markings": ["ex:personal"],
        }
    ]
    assert "dry runs" in next(
        w["message"] for w in released.warnings if w["code"] == "admin.marking_in_expression"
    )


def test_diff_with_a_graph_previews_the_same_warnings(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    assert apply(run_cli, tmp_path, policy("ex:personal")).code == 0
    source = with_rule(tmp_path, "2.0.0", REQUIRED)
    previewed = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    assert previewed.code == 0, previewed.envelope
    assert "admin.marking_in_expression" in previewed.warning_codes
    assert "admin.marking_blocks_create" in previewed.warning_codes
    released = admin(run_cli, "ontology", "release", "example_graph", str(source))
    previewed_codes = sorted(c for c in previewed.warning_codes if c.startswith("admin.marking_"))
    released_codes = sorted(c for c in released.warning_codes if c.startswith("admin.marking_"))
    assert previewed_codes == released_codes


def test_push_warns_like_the_release(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    assert apply(run_cli, tmp_path, policy("ex:personal")).code == 0
    work = tmp_path / "work_tree"
    pulled = admin(run_cli, "graph", "pull", "example_graph", str(work))
    assert pulled.code == 0, pulled.envelope
    shutil.copytree(with_rule(tmp_path, "2.0.0", REQUIRED), work / "ontology" / "2.0.0")
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert pushed.code == 0, pushed.envelope
    assert "admin.marking_in_expression" in pushed.warning_codes
    assert "admin.marking_blocks_create" in pushed.warning_codes


ACTION = """\
schema: graph_cli.action/1
name: tidy_person
summary: Record the team of a person that has an e-mail address.
use_when: A person changes the team.
params:
  person:
    type: ref
    class: ex:Person
    summary: The person.
  team:
    type: string
    summary: The new team.
preconditions:
  - id: has_mail
    on: person
    summary: The person has an e-mail address.
    require:
      - {attr: email, value_presence: PRESENT}
ops:
  - {op: update, id: $person, set: {team: $team}}
reason: required
examples:
  - request: Ana moves to the platform team.
"""


def test_a_published_action_precondition_over_a_marked_attribute_warns(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    folder = tmp_path / "actions"
    folder.mkdir()
    (folder / "tidy_person.yaml").write_text(ACTION)
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope
    assert apply(run_cli, tmp_path, policy("ex:personal")).code == 0
    patch = release_dir(tmp_path, "1.0.1", {"Full name.": "The full name."})
    previewed = admin(run_cli, "ontology", "diff", str(patch), "--graph", "example_graph")
    released = admin(run_cli, "ontology", "release", "example_graph", str(patch))
    for outcome in (previewed, released):
        assert outcome.code == 0, outcome.envelope
        assert details(outcome, "admin.marking_in_expression") == [
            {
                "graph": "example_graph",
                "kind": "precondition",
                "name": "tidy_person.has_mail",
                "attributes": ["ex:Person.email"],
                "markings": ["ex:personal"],
            }
        ]
