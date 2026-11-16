"""S: releases that reuse or downgrade the names of marked attributes (field visibility plan F2
as amended by R2), through ``ontology release``, ``graph check --ontology``, ``ontology diff
--graph`` and ``graph push``, on the ``personnel`` series."""

import shutil
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.fixtures import ONTOLOGIES
from support.policies import write_policy

from graph_cli_admin.services_admin.marking_names import traces_markings

PERSONNEL = ONTOLOGIES / "personnel"
HOLDER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [ex:personal]}
no_project: {"*": write}
"""
SHARED = """\
format: graph_cli.ir/1
module:
  id: shared
  prefix: shr
  version: 1.0.0
  summary: Markings several modules use.
markings:
  personal:
    summary: Personal data of a natural person.
"""


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


def version(tmp_path: Path, number: str, edit: dict[str, str] | None = None) -> Path:
    """The 1.0.0 module as ``number``, with ``edit`` applied (old text -> new text)."""
    text = (PERSONNEL / "1.0.0" / "personnel.yaml").read_text()
    text = text.replace("version: 1.0.0", f"version: {number}")
    for old, new in (edit or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    target = tmp_path / number
    target.mkdir()
    (target / "personnel.yaml").write_text(text)
    return target


def migration(source: Path, from_version: str, steps: str = "  []\n") -> None:
    (source / "migration.yaml").write_text(
        f"format: graph_cli.migration/1\nfrom: {from_version}\nsummary: Test.\nsteps:\n{steps}"
    )


def release(run_cli: CliRunner, source: Path, *extra: str) -> Outcome:
    return admin(run_cli, "ontology", "release", "example_graph", str(source), *extra)


def released(run_cli: CliRunner, source: Path) -> Outcome:
    outcome = release(run_cli, source)
    assert outcome.code == 0, outcome.envelope
    return outcome


def warnings(outcome: Outcome, code: str) -> list[dict[str, object]]:
    return [w["details"] for w in outcome.envelope["warnings"] if w["code"] == code]


EMAIL = """      email:
        summary: Private e-mail address.
        type: string
        markings: [ex:personal]
"""


def test_marking_removed_in_place_warns(run_cli: CliRunner, graph_id: str) -> None:
    outcome = released(run_cli, PERSONNEL / "1.1.0")
    assert warnings(outcome, "admin.marking_removed") == [
        {
            "graph": "example_graph",
            "type": "ex:Person",
            "attribute": "birth_date",
            "markings": ["ex:personal"],
            "remaining": [],
            "via": "in_place",
        }
    ]
    assert "including history" in outcome.envelope["warnings"][0]["message"]


def test_drop_then_reuse_is_refused_everywhere(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    released(run_cli, version(tmp_path, "2.0.0", {EMAIL: ""}))
    reused = version(tmp_path, "2.1.0", {"        markings: [ex:personal]\n": ""})
    expected = {
        "version": "1.0.0",
        "type": "ex:Person",
        "attribute": "email",
        "markings": ["ex:personal"],
        "to": {"type": "ex:Person", "attribute": "email", "markings": []},
    }
    refused = release(run_cli, reused)
    assert (refused.code, refused.error["code"]) == (1, "ontology.marking_name_reused")
    assert refused.error["details"]["reuses"] == [expected]
    assert refused.error["details"]["to"] == "2.1.0"
    assert "ex:Person.email" in refused.error["message"]
    checked = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(reused))
    assert checked.error["code"] == "ontology.marking_name_reused", checked.envelope
    assert checked.error["details"]["reuses"] == [expected]
    diffed = admin(run_cli, "ontology", "diff", str(reused), "--graph", "example_graph")
    assert diffed.error["code"] == "ontology.marking_name_reused", diffed.envelope
    # with the marking the name may come back
    released(run_cli, version(tmp_path, "2.1.1"))


def test_push_refuses_a_reused_name(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    released(run_cli, version(tmp_path, "2.0.0", {EMAIL: ""}))
    work = tmp_path / "work"
    pulled = admin(run_cli, "graph", "pull", "example_graph", str(work))
    assert pulled.code == 0, pulled.envelope
    reused = version(tmp_path, "2.1.0", {"        markings: [ex:personal]\n": ""})
    shutil.copytree(reused, work / "ontology" / "2.1.0")
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert pushed.error["code"] == "ontology.marking_name_reused", pushed.envelope


def test_retype_carry_is_refused(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    contact = """  Contact:
    summary: Someone to reach.
    display_prefix: CON
    attributes:
      email:
        summary: E-mail address.
        type: string
  Team:"""
    released(run_cli, version(tmp_path, "1.1.0", {"  Team:": contact}))
    person = PERSONNEL / "1.0.0" / "personnel.yaml"
    body = person.read_text()
    start = body.index("  Person:")
    without_person = body[:start] + body[body.index("  Team:") :]
    target = tmp_path / "2.0.0"
    target.mkdir()
    (target / "personnel.yaml").write_text(
        without_person.replace("version: 1.0.0", "version: 2.0.0").replace("  Team:", contact)
    )
    migration(target, "1.1.0", "  - retype: {from: ex:Person, to: ex:Contact}\n")
    refused = release(run_cli, target)
    assert refused.error["code"] == "ontology.marking_name_reused", refused.envelope
    reuses = refused.error["details"]["reuses"]
    assert [(r["type"], r["attribute"], r["to"]["type"]) for r in reuses] == [
        ("ex:Person", "email", "ex:Contact")
    ]


def test_rename_then_reuse_warns_and_history_keeps_the_value_away(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    person = {
        "op": "create",
        "type": "ex:Person",
        "attrs": {"name": "Example person", "email": "person@example.org"},
    }
    write_documents(root, "example_graph", [{"ops": [person]}])
    renamed = version(
        tmp_path,
        "2.0.0",
        {"      email:\n": "      mail:\n        renamed_from: email\n"},
    )
    migration(renamed, "1.0.0")
    released(run_cli, renamed)
    work = "      email:\n        summary: Work e-mail.\n        type: string\n"
    returned = version(tmp_path, "2.1.0", {EMAIL: EMAIL.replace("email", "mail") + work})
    outcome = released(run_cli, returned)
    assert warnings(outcome, "ontology.marking_name_returned") == [
        {
            "graph": "example_graph",
            "type": "ex:Person",
            "attribute": "email",
            "version": "1.0.0",
            "markings": ["ex:personal"],
            "maps_to": "ex:Person.mail",
        }
    ]
    found = app(run_cli, "find", "--type", "ex:Person", env=ENV)
    entity = found.data["items"][0]["id"]
    first = app(run_cli, "get", entity, "--rev", "1", env=ENV)
    assert first.code == 0, first.envelope
    attrs = first.data["entity"]["attrs"]
    # Without an installation policy no app caller holds ex:personal: the value shows under
    # neither name; a holder sees it under the marked current name, never under ``email``.
    assert "mail" not in attrs and "email" not in attrs
    path = write_policy(tmp_path / "work", "installation.yaml", HOLDER)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    first = app(run_cli, "get", entity, "--rev", "1", env=ENV)
    assert first.code == 0, first.envelope
    attrs = first.data["entity"]["attrs"]
    assert attrs.get("mail") == "person@example.org" and "email" not in attrs


def test_unreadable_release_in_the_chain_refuses(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    released(run_cli, PERSONNEL / "1.1.0")
    (root / "graphs" / graph_id / "ontology" / "1.0.0" / "compiled.json").unlink()
    patch = tmp_path / "1.1.1"
    patch.mkdir()
    text = (PERSONNEL / "1.1.0" / "personnel.yaml").read_text()
    text = text.replace("version: 1.1.0", "version: 1.1.1").replace("Full name.", "Full name!")
    (patch / "personnel.yaml").write_text(text)
    refused = release(run_cli, patch)
    assert refused.error["code"] == "ontology.marking_name_reused", refused.envelope
    assert refused.error["details"]["unreadable"] == "1.0.0"
    assert "fail closed" in refused.error["message"]


def test_marking_moved_to_another_module_is_a_new_marking(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    moved = version(
        tmp_path,
        "2.0.0",
        {
            "  summary: People and the teams they belong to.\n": (
                "  summary: People and the teams they belong to.\n"
                "  imports:\n    - {module: shared, version: '^1.0.0'}\n"
            ),
            "markings:\n  personal:\n    summary: Personal data of a natural person.\n"
            "    description: Visible only to callers that hold this marking.\n": "",
            "markings: [ex:personal]": "markings: [shr:personal]",
            "markings: [personal]": "markings: [shr:personal]",
        },
    )
    (moved / "shared.yaml").write_text(SHARED)
    outcome = admin(
        run_cli, "ontology", "release", "example_graph", str(moved), "--module", "personnel"
    )
    assert outcome.code == 0, outcome.envelope
    removed = warnings(outcome, "admin.marking_removed")
    # grants name the marking by its CURIE: a moved marking is another one (fail closed)
    assert [(w["attribute"], w["markings"], w["remaining"]) for w in removed] == [
        ("birth_date", ["ex:personal"], ["shr:personal"]),
        ("email", ["ex:personal"], ["shr:personal"]),
    ]
    kinds = {c["kind"] for c in outcome.data["diff"]["changes"]}
    assert {"add_marking", "remove_marking", "remove_marking_term"} <= kinds


INVENTORY = ONTOLOGIES / "inventory"


def unmarked_graph(run_cli: CliRunner) -> str:
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.0.0")
    )
    assert created.code == 0, created.envelope
    graph_id: str = created.data["id"]
    released(run_cli, INVENTORY / "1.0.1")
    return graph_id


def test_unmarked_graph_with_a_missing_release_still_releases(
    run_cli: CliRunner, root: Path
) -> None:
    graph_id = unmarked_graph(run_cli)
    (root / "graphs" / graph_id / "ontology" / "1.0.0" / "compiled.json").unlink()
    outcome = released(run_cli, INVENTORY / "1.1.0")
    assert not [w for w in outcome.envelope["warnings"] if "marking" in w["code"]]


def test_unreadable_release_that_shows_markings_refuses_an_unmarked_release(
    run_cli: CliRunner, root: Path
) -> None:
    graph_id = unmarked_graph(run_cli)
    path = root / "graphs" / graph_id / "ontology" / "1.0.0" / "compiled.json"
    path.write_text('{"schema": "graph_cli.compiled_ontology/2", "requires": ["markings"]}')
    refused = release(run_cli, INVENTORY / "1.1.0")
    assert refused.error["code"] == "ontology.marking_name_reused", refused.envelope
    assert refused.error["details"]["unreadable"] == "1.0.0"


def test_raw_traces_of_markings() -> None:
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/2"}')
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/1", "markings": {}}')
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/2", "trunc')
    assert not traces_markings(b'{"schema": "graph_cli.compiled_ontology/1"}')
    assert not traces_markings(b"\x00 not json")
