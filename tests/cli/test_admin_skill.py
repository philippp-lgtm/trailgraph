"""S: graph-admin graph skill publish (D15 Q2; contracts B6 ``maint.graph_configure``, W.e).

The publish event pair (graph intent plus installation anchor) verifies
clean, a re-publish of the same files changes nothing, every broken tree
rule is reported, and a skill with maintenance vocabulary is refused (the
L positive control for graph skills).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES, ONTOLOGIES
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.catalog import skill_hash
from graph_cli.skills.tree import read_tree
from graph_cli.storage.files.audit_log import FileAuditLog

SKILL = FIXTURES / "skills" / "graph-example-graph"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    return create_graph(run_cli, "example_graph")


def publish(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "skill", "publish", "example_graph", str(source), *args)


def copy(tmp_path: Path, name: str = "graph-example-graph") -> Path:
    target = tmp_path / "work" / name
    shutil.copytree(SKILL, target)
    return target


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def installation_events(root: Path) -> list[dict[str, Any]]:
    log = FileAuditLog(root).read_lines(INSTALLATION_STREAM)
    report = verify_stream(log, stream=INSTALLATION_STREAM)
    assert report.findings == ()
    return [dict(event) for event in report.events]


def test_publish_records_the_change_and_verifies_clean(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    assert admin(run_cli, "graph", "show", "example_graph").data["skill"] is None
    files, _ = read_tree(SKILL)
    outcome = publish(run_cli, SKILL, "--reason", "first skill")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["published"], data["dry_run"], data["from"]) == (True, False, None)
    assert data["skill"] == "graph-example-graph"
    assert data["files"] == ["SKILL.md", "references/model.md"]
    assert data["to"]["hash"] == skill_hash(files)
    assert data["to"]["kind"] == "static"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["skill"] == data["to"]
    assert shown["skill"]["published_against"]["version"] == shown["ontology"]["version"]

    intent = graph_events(root, graph_id)[-1]
    assert intent["actions"] == ["maint.graph_configure"]
    assert intent["change"] == {"skill": {"from": None, "to": skill_hash(files)}}
    assert intent["reason"] == "first skill"
    companion = installation_events(root)[-1]
    assert companion["actions"] == ["maint.graph_configure"]
    assert companion["anchors"] == [
        {"graph_id": graph_id, "seq": intent["seq"], "hash": intent["hash"]}
    ]
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_republishing_the_same_files_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    assert publish(run_cli, SKILL).code == 0
    before = tree_hash(root)
    again = publish(run_cli, copy(tmp_path))  # same files from another place
    assert again.code == 0 and again.data["published"] is False
    assert again.data["from"] == again.data["to"]
    assert tree_hash(root) == before


def test_republishing_changed_files_names_both_hashes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    first = publish(run_cli, SKILL).data["to"]["hash"]
    changed = copy(tmp_path)
    (changed / "references" / "model.md").write_text("# Model\n\nRewritten.\n")
    outcome = publish(run_cli, changed)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == first
    assert graph_events(root, graph_id)[-1]["change"]["skill"]["from"] == first


def test_a_new_release_makes_the_same_files_publishable_again(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    publish(run_cli, SKILL)
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    outcome = publish(run_cli, SKILL)
    assert outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == outcome.data["to"]["hash"]
    assert outcome.data["to"]["published_against"]["version"] == "1.0.1"
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_writes_nothing(run_cli: CliRunner, root: Path, graph_id: str) -> None:
    before = tree_hash(root)
    outcome = publish(run_cli, SKILL, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["published"], outcome.data["dry_run"]) == (False, True)
    assert tree_hash(root) == before


def test_invalid_trees_list_every_broken_rule(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    broken = copy(tmp_path, "graph-other")
    skill_md = broken / "SKILL.md"
    skill_md.write_text(skill_md.read_text().replace("Read the", "Ask graph-admin or read the"))
    (broken / ".notes").write_text("x")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert outcome.code == 1
    assert outcome.error["code"] == "admin.skill_invalid"
    assert outcome.error["category"] == "invalid" and outcome.error["hint"] is None
    problems = outcome.error["details"]["problems"]
    assert sorted(p["rule"] for p in problems) == ["file", "leak", "name"]
    (leak,) = [p for p in problems if p["rule"] == "leak"]
    assert leak["path"] == "SKILL.md" and "graph-admin" in leak["message"]
    assert tree_hash(root) == before


def test_a_clean_copy_publishes(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    """Positive control for the refusal above: the same tree without the breakage."""
    assert publish(run_cli, copy(tmp_path)).code == 0


def test_text_mode_names_the_problems(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    broken = copy(tmp_path)
    (broken / "SKILL.md").write_text("no frontmatter\n")
    result = run_cli("graph-admin", "graph", "skill", "publish", "example_graph", str(broken))
    assert result.returncode == 1
    assert "admin.skill_invalid" in result.stderr and "frontmatter" in result.stderr


# ---- generated graph skills (templates) -------------------------------------------------

TEMPLATE = FIXTURES / "skills" / "template" / "graph-example-graph"


def copy_template(tmp_path: Path) -> Path:
    target = tmp_path / "template" / "graph-example-graph"
    shutil.copytree(TEMPLATE, target)
    return target


def render(run_cli: CliRunner, source: Path, out: Path, *args: str) -> Outcome:
    return admin(
        run_cli, "graph", "skill", "render", "example_graph", str(source), "--out", str(out), *args
    )


def test_publish_a_template_as_generated(run_cli: CliRunner, root: Path, graph_id: str) -> None:
    files, _ = read_tree(TEMPLATE)
    outcome = publish(run_cli, TEMPLATE, "--reason", "generated skill")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["to"]["kind"] == "generated"
    assert outcome.data["to"]["hash"] == skill_hash(files)
    assert outcome.data["files"] == ["SKILL.md", "references/recipes.md"]  # the template as is
    assert admin(run_cli, "graph", "show", "example_graph").data["skill"]["kind"] == "generated"
    intent = graph_events(root, graph_id)[-1]
    assert intent["change"] == {"skill": {"from": None, "to": skill_hash(files)}}
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}

    static = publish(run_cli, SKILL)  # switching back to a static skill is one more change
    assert static.code == 0 and static.data["to"]["kind"] == "static"
    assert static.data["from"]["kind"] == "generated"


def test_an_invalid_template_lists_one_problem_per_rule(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    broken = copy_template(tmp_path)
    skill_md = broken / "SKILL.md"
    text = skill_md.read_text()
    text = text.replace("<!-- graph_cli:block model -->", "<!-- graph_cli:block samples -->")
    skill_md.write_text(text)
    (broken / "references" / "model.md").write_text("# Mine\n")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.skill_invalid")
    problems = outcome.error["details"]["problems"]
    assert [(p["rule"], p["path"]) for p in problems] == [
        ("template", "references/model.md"),
        ("template", "SKILL.md"),
    ]
    assert "unknown block 'samples'" in problems[1]["message"]

    curated = copy_template(tmp_path / "second")
    skill_md = curated / "SKILL.md"
    text = skill_md.read_text().replace("`inv:Item` is stored", "`inv:Thing` is stored")
    text = text.replace("--direction out", "--towards out").replace("ITEM-0001", "ITEM-1")
    skill_md.write_text(text)
    outcome = publish(run_cli, curated)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.skill_invalid")
    rules = [p["rule"] for p in outcome.error["details"]["problems"]]
    assert sorted(rules) == ["alias", "command", "term"]
    assert tree_hash(root) == before


def test_a_template_with_maintenance_vocabulary_is_refused(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    """L positive control for generated skills: curated text is leak-checked too."""
    broken = copy_template(tmp_path)
    recipes = broken / "references" / "recipes.md"
    recipes.write_text(recipes.read_text() + "\nAsk for graph-admin access.\n")
    outcome = publish(run_cli, broken)
    assert outcome.error["code"] == "admin.skill_invalid"
    (leak,) = outcome.error["details"]["problems"]
    assert (leak["rule"], leak["path"]) == ("leak", "references/recipes.md")
    assert publish(run_cli, copy_template(tmp_path / "clean")).code == 0  # positive control


def test_a_template_must_render_under_the_size_limit(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    from graph_cli.skills.tree import MAX_SKILL_LINES

    big = copy_template(tmp_path)
    skill_md = big / "SKILL.md"
    lines = MAX_SKILL_LINES - skill_md.read_text().count("\n") - 5
    skill_md.write_text(skill_md.read_text() + "Filler.\n" * lines)  # the template still fits
    outcome = publish(run_cli, big)
    assert outcome.error["code"] == "admin.skill_invalid"
    (problem,) = outcome.error["details"]["problems"]
    assert (problem["rule"], problem["path"]) == ("size", "SKILL.md")


def test_render_writes_the_preview_and_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    out = tmp_path / "preview"
    out.mkdir()
    before = tree_hash(root)
    outcome = render(run_cli, TEMPLATE, out)
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["skill"], data["kind"], data["problems"]) == (
        "graph-example-graph",
        "generated",
        [],
    )
    assert data["files"] == ["SKILL.md", "references/model.md", "references/recipes.md"]
    assert data["model"]["version"] == "1.0.0"
    files, problems = read_tree(out / "graph-example-graph")
    assert problems == [] and sorted(files) == data["files"]
    text = files["SKILL.md"].decode()
    assert "graph_cli:block" not in text and "`inv:Item` (`ITEM-0001`)" in text
    assert "the ontology inventory 1.0.0" in text  # the stamp names the pin it rendered
    assert tree_hash(root) == before  # no slot change, no event
    assert admin(run_cli, "graph", "show", "example_graph").data["skill"] is None

    refused = render(run_cli, TEMPLATE, out)
    assert (refused.code, refused.error["code"]) == (1, "admin.skill_render_exists")
    assert refused.error["hint"] == (
        f"graph-admin graph skill render example_graph {TEMPLATE} --out {out.resolve()} --force"
    )
    forced = render(run_cli, TEMPLATE, out, "--force")
    assert forced.code == 0, forced.envelope
    assert read_tree(out / "graph-example-graph")[0] == files


def test_render_reports_what_publish_would_refuse(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    curated = copy_template(tmp_path)
    skill_md = curated / "SKILL.md"
    skill_md.write_text(skill_md.read_text().replace("ITEM-0001", "ITEM-1"))
    out = tmp_path / "preview"
    out.mkdir()
    outcome = render(run_cli, curated, out)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.skill_invalid")
    assert [p["rule"] for p in outcome.data["problems"]] == ["alias"]  # partial result
    assert (out / "graph-example-graph" / "SKILL.md").is_file()

    broken = copy_template(tmp_path / "broken")
    (broken / "SKILL.md").write_text("no frontmatter\n<!-- graph_cli:block types -->\n")
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    refused = render(run_cli, broken, nothing)
    assert refused.error["code"] == "admin.skill_invalid" and refused.data is None
    assert list(nothing.iterdir()) == []


def test_render_of_a_static_skill_is_its_files(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    out = tmp_path / "preview"
    out.mkdir()
    outcome = render(run_cli, SKILL, out)
    assert outcome.code == 0 and outcome.data["kind"] == "static"
    assert read_tree(out / "graph-example-graph")[0] == read_tree(SKILL)[0]


def test_a_template_naming_an_unpublished_function_is_refused(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    """`graph run <name>` in curated text must name a published function (K12)."""
    curated = copy_template(tmp_path)
    recipes = curated / "references" / "recipes.md"
    recipes.write_text(recipes.read_text() + "\nRun `graph run ghost_function --param key=1`.\n")
    refused = publish(run_cli, curated)
    assert (refused.code, refused.error["code"]) == (1, "admin.skill_invalid")
    (problem,) = refused.error["details"]["problems"]
    assert problem["rule"] == "function" and "ghost_function" in problem["message"]


def test_publish_checks_the_template_against_the_current_pin(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    """A curated name the pinned model lacks is refused; after a release that has it, the
    same template publishes (inventory 1.1.0 adds inv:Supplier)."""
    curated = copy_template(tmp_path)
    recipes = curated / "references" / "recipes.md"
    recipes.write_text(recipes.read_text() + "\nSuppliers are `inv:Supplier` entities.\n")
    refused = publish(run_cli, curated)
    assert [p["rule"] for p in refused.error["details"]["problems"]] == ["term"]
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.1.0")
    )
    assert released.code == 0, released.envelope
    assert publish(run_cli, curated).code == 0


def test_a_template_naming_an_action_must_name_a_published_one(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    """`graph do <name>` in curated text must name a published action (actions plan A11)."""
    curated = copy_template(tmp_path)
    recipes = curated / "references" / "recipes.md"
    recipes.write_text(recipes.read_text() + "\nRun `graph do move_item --param item=<id>`.\n")
    refused = publish(run_cli, curated)
    assert (refused.code, refused.error["code"]) == (1, "admin.skill_invalid")
    (problem,) = refused.error["details"]["problems"]
    assert problem["rule"] == "action" and "move_item" in problem["message"]
    actions = FIXTURES / "actions" / "inventory" / "valid"
    assert admin(run_cli, "graph", "actions", "publish", "example_graph", str(actions)).code == 0
    assert publish(run_cli, curated).code == 0
