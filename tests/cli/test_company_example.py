"""S: the tracked example graph ``examples/company`` stays buildable.

The example is a user-facing artifact, separate from the ``company`` test fixture: two
versions of a modular ontology, synthetic seeds per version, a curated graph skill and the
scripts that build it. These tests keep every part of it consistent with the current code.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, REPO_ROOT, CliRunner
from support.admin import Outcome, admin, init
from support.app import app

from graph_cli.kernel.changeset import read_documents

EXAMPLE = REPO_ROOT / "examples" / "company"
ONTOLOGY = EXAMPLE / "ontology"
SEEDS = EXAMPLE / "seeds"
SKILL = EXAMPLE / "skill" / "graph-company"
ENV = {"GRAPH_CLI_GRAPH": "company"}
V100_MODULES = ["company", "operations", "organization", "people"]
V110_MODULES = ["company", "legal", "operations", "organization", "people"]


def seed_documents(version: str) -> list[dict[str, Any]]:
    documents = read_documents((SEEDS / f"{version}.jsonl").read_bytes())
    return [dict(document) for document in documents]  # type: ignore[arg-type]


def created_types(version: str) -> list[str]:
    return [
        op["type"]
        for document in seed_documents(version)
        for op in document["ops"]
        if op["op"] == "create"
    ]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(run_cli: CliRunner) -> Outcome:
    outcome = admin(
        run_cli,
        *("graph", "create", "company"),
        *("--ontology", str(ONTOLOGY / "1.0.0"), "--module", "company"),
    )
    assert outcome.code == 0, outcome.envelope
    return outcome


def release(run_cli: CliRunner) -> Outcome:
    outcome = admin(
        run_cli,
        *("ontology", "release", "company", str(ONTOLOGY / "1.1.0"), "--module", "company"),
    )
    assert outcome.code == 0, outcome.envelope
    return outcome


def apply_seeds(run_cli: CliRunner, version: str) -> Outcome:
    outcome = app(run_cli, "apply", "-", env=ENV, input=(SEEDS / f"{version}.jsonl").read_text())
    assert outcome.code == 0, outcome.envelope
    for result in outcome.data["results"]:
        assert result["result"]["findings"] == [], result
    return outcome


@pytest.fixture
def company(run_cli: CliRunner, root: Path) -> None:
    create(run_cli)
    apply_seeds(run_cli, "1.0.0")
    release(run_cli)
    apply_seeds(run_cli, "1.1.0")


def test_each_version_validates_with_all_its_modules(run_cli: CliRunner) -> None:
    for version, modules in (("1.0.0", V100_MODULES), ("1.1.0", V110_MODULES)):
        directory = ONTOLOGY / version
        assert sorted(p.stem for p in directory.glob("*.yaml")) == modules
        outcome = admin(run_cli, "ontology", "validate", str(directory), "--module", "company")
        assert outcome.code == 0, outcome.envelope
        assert outcome.data["findings"] == []
        assert (outcome.data["bundle"], outcome.data["version"]) == ("company", version)
        assert sorted(outcome.data["modules"]) == sorted(["core", *modules])


def test_the_new_version_is_a_minor_release(run_cli: CliRunner) -> None:
    outcome = admin(
        run_cli,
        *("ontology", "diff", str(ONTOLOGY / "1.1.0")),
        *("--against", str(ONTOLOGY / "1.0.0"), "--module", "company"),
    )
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["level"], outcome.data["bump"]) == ("minor", "ok")
    kinds = {(c["kind"], c["path"]) for c in outcome.data["changes"]}
    assert ("add_import", "modules.legal") in kinds
    assert {c["level"] for c in outcome.data["changes"]} == {"minor"}
    # Only the new module is added; the modules of 1.0.0 are byte for byte the same.
    for module in ("people", "organization", "operations"):
        assert (ONTOLOGY / "1.0.0" / f"{module}.yaml").read_bytes() == (
            ONTOLOGY / "1.1.0" / f"{module}.yaml"
        ).read_bytes()


def test_the_seeds_are_synthetic_stable_and_of_a_useful_size() -> None:
    first, second = seed_documents("1.0.0"), seed_documents("1.1.0")
    keys = [d["idempotency_key"] for d in [*first, *second]]
    assert len(keys) == len(set(keys))
    assert all(re.fullmatch(r"company-1\.[01]\.0-\d\d", k) for k in keys)
    assert 60 <= len(created_types("1.0.0")) + len(created_types("1.1.0")) <= 120
    assert {t.split(":")[0] for t in created_types("1.1.0")} == {"leg"}
    text = (SEEDS / "1.0.0.jsonl").read_text() + (SEEDS / "1.1.0.jsonl").read_text()
    assert "@marrowdale.example" in text
    assert not re.search(r"@(?!marrowdale\.example)[\w.-]+\.(com|org|net)\b", text)


def test_both_seed_files_apply_and_the_batch_check_is_clean(run_cli: CliRunner, root: Path) -> None:
    create(run_cli)
    apply_seeds(run_cli, "1.0.0")
    checked = admin(run_cli, "graph", "check", "company")
    assert checked.code == 0, checked.envelope
    assert checked.data["findings"] == []
    assert checked.data["ontology"] == {"bundle": "company", "version": "1.0.0"}

    release(run_cli)
    apply_seeds(run_cli, "1.1.0")
    checked = admin(run_cli, "graph", "check", "company")
    assert checked.code == 0, checked.envelope
    assert checked.data["findings"] == []
    assert checked.data["ontology"] == {"bundle": "company", "version": "1.1.0"}
    assert admin(run_cli, "audit", "verify").code == 0


def test_the_seeds_hold_the_structure_the_example_shows(run_cli: CliRunner, company: None) -> None:
    def rows(query: str, **params: str) -> list[dict[str, Any]]:
        args = [a for k, v in params.items() for a in ("--param", f"{k}={v}")]
        outcome = app(run_cli, "query", query, *args, env=ENV)
        assert outcome.code == 0, outcome.envelope
        items: list[dict[str, Any]] = outcome.data["items"]
        return items

    assert rows("MATCH (p:Employee)-[:holds]->(:Position) RETURN count(p) AS n") == [{"n": 18}]
    chain = rows(
        "MATCH (a:Position)-[:reports_to*1..4]->(b:Position) WHERE a.title = $t RETURN b.title",
        t="Site Reliability Engineer",
    )
    assert [r["b.title"] for r in chain] == [
        "Head of Infrastructure",
        "Chief Technology Officer",
        "Chief Executive Officer",
    ]
    owners = rows(
        "MATCH (s:Shareholding)-[:stake_in]->(e:LegalEntity) "
        "WHERE e.name = $n RETURN count(s) AS n",
        n="Marrowdale Holding AG",
    )
    assert owners == [{"n": 3}]
    unemployed = rows(
        "MATCH (e:Employee) WHERE NOT (e)-[:employed_by]->() RETURN e.status AS status",
    )
    assert unemployed == [{"status": "left"}]


def test_the_modules_read_top_down(run_cli: CliRunner, company: None) -> None:
    modules = app(run_cli, "describe", "modules", env=ENV).data["modules"]
    authored = {m["id"]: m for m in modules if not m["builtin"]}
    assert sorted(authored) == V110_MODULES
    assert authored["company"]["types"] == 0 and authored["company"]["relations"] == 0
    assert {"module": "organization", "out": 0, "in": 1} in authored["legal"]["links"]
    legal = app(run_cli, "describe", "module", "leg", env=ENV).data["module"]
    assert {t["curie"] for t in legal["types"]} == {"leg:LegalEntity", "leg:Shareholding"}
    assert {k["relation"] for k in legal["links_in"]} == {"leg:employed_by", "leg:operated_by"}
    found = app(run_cli, "describe", "search", "shareholder", "--module", "legal", env=ENV)
    assert found.code == 0, found.envelope
    assert found.data["items"][0]["curie"] == "leg:shareholder"
    only = app(run_cli, "describe", "types", "--module", "ops", env=ENV)
    assert {t["curie"] for t in only.data["types"]} == {
        "ops:Instruction",
        "ops:Policy",
        "ops:Process",
        "ops:Task",
        "ops:Training",
    }


def test_a_refused_link_names_the_relations_that_connect_the_types(
    run_cli: CliRunner, company: None
) -> None:
    refused = app(run_cli, "link", "EMP-0003", "org:in_unit", "DEPT-0006", env=ENV)
    assert refused.code == 1, refused.envelope
    [finding] = refused.error["details"]["findings"]
    assert finding["code"] == "data.referential.domain_violation"
    assert finding["connecting"]["paths"] == [
        [
            {"relation": "org:holds", "from": "ppl:Employee", "to": "org:Position"},
            {"relation": "org:in_unit", "from": "org:Position", "to": "org:Department"},
        ]
    ]


def refused_messages(outcome: Outcome) -> set[str]:
    assert outcome.code == 1, outcome.envelope
    assert outcome.error["code"] == "write.invalid"
    return {f["message"] for f in outcome.error["details"]["findings"]}


def test_every_guard_and_rule_of_the_example_fires(run_cli: CliRunner, company: None) -> None:
    """Each guard and rule of the five modules is made to fire once (review checklist)."""

    def transition(entity: str, state: str) -> Outcome:
        return app(run_cli, "transition", entity, state, "--dry-run", env=ENV)

    # org.not_held
    assert refused_messages(transition("POS-0001", "abolished")) == {
        "A position that is held by a person cannot be abolished."
    }
    # ops.has_assignee (the open task without a person)
    assert refused_messages(transition("TASK-0004", "in_progress")) == {
        "A task that is started has an assignee."
    }
    # ops.policy_in_force_has_date (the draft policy has a scope but no date)
    assert refused_messages(transition("POL-0004", "in_force")) == {
        "A policy in force states from when it is binding."
    }
    # ops.has_scope (a new draft policy that applies to no unit)
    probe = {
        "ops": [
            {
                "op": "create",
                "type": "ops:Policy",
                "local": "probe",
                "attrs": {"title": "Probe policy", "effective_from": "2026-01-01"},
                "edges": [{"rel": "ops:owned_by", "target": "POS-0001"}],
            },
            {"op": "transition", "id": "@probe", "to": "in_force"},
        ]
    }
    scope = app(run_cli, "apply", "-", "--dry-run", env=ENV, input=json.dumps(probe))
    assert refused_messages(scope) == {"A policy in force applies to at least one unit."}
    # leg.no_staff and leg.no_units
    assert refused_messages(transition("LE-0002", "dissolved")) == {
        "A dissolved entity employs nobody.",
        "A dissolved entity runs no unit.",
    }
    # org.left_employee_holds_no_position and leg.left_employee_has_no_employer
    assert refused_messages(transition("EMP-0001", "left")) == {
        "An employee who has left holds no position.",
        "An employee who has left is employed by no legal entity.",
    }
    # org.team_in_department
    stray = app(
        run_cli,
        *("create", "org:Team", "--set-json", 'name="Stray team"'),
        *("--edge", "org:part_of=TEAM-0001", "--dry-run"),
        env=ENV,
    )
    assert refused_messages(stray) == {"A team is part of exactly one department."}
    # leg.stakes_within_capital (a warning: the write goes through)
    over = app(
        run_cli,
        *("create", "leg:Shareholding", "--set", "percent=30"),
        *("--edge", "leg:shareholder=EMP-0003", "--edge", "leg:stake_in=LE-0001"),
        *("--dry-run",),
        env=ENV,
    )
    assert over.code == 0, over.envelope
    [warning] = over.data["findings"]
    assert (warning["severity"], warning["rule_id"]) == ("warning", "leg.stakes_within_capital")


def test_the_batch_rules_of_the_example_fire(run_cli: CliRunner, company: None) -> None:
    """ops.process_documented and leg.employee_has_employer only show in the batch check."""
    clean = admin(run_cli, "graph", "check", "company")
    assert clean.data["findings"] == []
    process = app(
        run_cli,
        *("create", "ops:Process", "--set", "name=Probe process"),
        *("--edge", "ops:owned_by=POS-0001"),
        env=ENV,
    )
    employee = app(
        run_cli,
        *("create", "ppl:Employee", "--set", "name=Probe Person"),
        *("--set", "employee_number=E-900"),
        env=ENV,
    )
    assert process.code == employee.code == 0, (process.envelope, employee.envelope)
    checked = admin(run_cli, "graph", "check", "company")
    rules = {f["rule_id"]: f["severity"] for f in checked.data["findings"]}
    assert rules == {"ops.process_documented": "warning", "leg.employee_has_employer": "warning"}


def test_the_graph_skill_renders_with_every_module(
    run_cli: CliRunner, company: None, tmp_path: Path
) -> None:
    out = tmp_path / "preview"
    out.mkdir()
    rendered = admin(run_cli, "graph", "skill", "render", "company", str(SKILL), "--out", str(out))
    assert rendered.code == 0, rendered.envelope
    assert rendered.data["problems"] == []
    files = set(rendered.data["files"])
    assert {"SKILL.md", "references/model.md"} <= files
    assert {f"references/modules/{m}.md" for m in V110_MODULES} <= files
    text = (out / "graph-company" / "SKILL.md").read_text()
    assert "## Subject areas" in text and "| `legal` | `leg` |" in text
    assert "graph_cli:block" not in text


def run_script(
    script: str, isolated_env: dict[str, str], tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = dict(isolated_env)
    env.update(PATH=f"{BIN_DIR}{os.pathsep}{env['PATH']}", BASE=str(tmp_path / "base"))
    return subprocess.run(
        ["bash", str(EXAMPLE / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=300,
    )


def state_dir_of(base: Path) -> Path:
    match = re.search(r"^export XDG_STATE_HOME=(\S+)$", (base / "env.sh").read_text(), re.M)
    assert match is not None
    return Path(match.group(1))


@pytest.mark.slow
@pytest.mark.parametrize("upto", ["1.0.0", "1.1.0"])
def test_setup_builds_a_throwaway_installation(
    upto: str, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    base = tmp_path / "base"
    try:
        done = run_script("setup.sh", isolated_env, tmp_path, upto)
        assert done.returncode == 0, done.stdout + done.stderr
        state = state_dir_of(base)
        assert len(str(state)) < 40
        env = {
            **isolated_env,
            "PATH": f"{BIN_DIR}{os.pathsep}{isolated_env['PATH']}",
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_STATE_HOME": str(state),
        }
        shown = subprocess.run(
            ["graph-admin", "graph", "show", "company", "--json"],
            capture_output=True,
            text=True,
            env=env,
            cwd=base / "work",
        )
        assert shown.returncode == 0, shown.stderr
        assert f'"version": "{upto}"' in shown.stdout
        assert (json.loads(shown.stdout)["data"]["skill"] is not None) == (upto == "1.1.0")
        # Nothing leaked into the caller's home and XDG directories.
        home = Path(isolated_env["HOME"])
        assert not (home / ".claude").exists()
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            assert list(Path(isolated_env[key]).iterdir()) == [], key
        # load.sh can be run again in the same installation.
        rerun = subprocess.run(
            ["bash", str(EXAMPLE / "load.sh"), upto],
            capture_output=True,
            text=True,
            env=env,
            cwd=base / "work",
        )
        assert rerun.returncode == 0, rerun.stdout + rerun.stderr
        # The installation is not a second run's target.
        again = run_script("setup.sh", isolated_env, tmp_path, upto)
        assert again.returncode == 1 and "is not empty" in again.stderr
    finally:
        env_file = base / "env.sh"
        if env_file.exists():
            shutil.rmtree(state_dir_of(base), ignore_errors=True)


def test_setup_needs_an_absolute_base(isolated_env: dict[str, str], tmp_path: Path) -> None:
    env = {**isolated_env, "PATH": f"{BIN_DIR}{os.pathsep}{isolated_env['PATH']}", "BASE": "rel"}
    refused = subprocess.run(
        ["bash", str(EXAMPLE / "setup.sh")], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert refused.returncode == 2
    env.pop("BASE")
    missing = subprocess.run(
        ["bash", str(EXAMPLE / "setup.sh")], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert missing.returncode != 0 and "BASE" in missing.stderr
