"""Install plan, atomic swap and check statuses on a temporary target (D15 Q3-Q5, Q8)."""

import os
from pathlib import Path

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli.skills import install
from graph_cli.skills.install import (
    STAGING_DIR,
    RenderedSkill,
    SkillTarget,
    apply_install,
    check,
    plan_install,
    refusal,
    rendered,
    require_target,
    scan,
)
from graph_cli.skills.tree import LEGACY_MARKER_FILE, MARKER_FILE, SkillFiles, read_tree, write_tree

PROGRAM = {"name": "graph", "version": "0.1.0"}


def app_skill(text: str = "one") -> RenderedSkill:
    files = {"SKILL.md": f"---\nname: graph-cli\n---\n{text}\n".encode(), "references/a.md": b"a"}
    return rendered("graph-cli", "app", files, program=PROGRAM, source={"inventory_hash": text})


def graph_skill(text: str = "g") -> RenderedSkill:
    files = {"SKILL.md": f"---\nname: graph-example\n---\n{text}\n".encode()}
    return rendered("graph-example", "graph", files, program=PROGRAM, source={"hash": text})


def put(directory: Path, files: SkillFiles) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    write_tree(directory, files)


@pytest.fixture
def target(tmp_path: Path) -> SkillTarget:
    (tmp_path / "project" / ".claude").mkdir(parents=True)
    return SkillTarget("project", tmp_path / "project" / ".claude", tmp_path / "project")


def install_all(target: SkillTarget, *skills: RenderedSkill, force: bool = False) -> list[str]:
    plan = plan_install(target, scan(target), skills, force=force)
    error = refusal(plan)
    if error is not None:
        raise error
    apply_install(plan)
    return [step.action for step in plan.steps]


def actions(target: SkillTarget, *skills: RenderedSkill, force: bool = False) -> list[str]:
    return [s.action for s in plan_install(target, scan(target), skills, force=force).steps]


def statuses(target: SkillTarget, *skills: RenderedSkill, **kw: object) -> dict[str, str]:
    return {s.name: s.status for s in check(scan(target), skills, **kw)}  # type: ignore[arg-type]


def test_require_target(tmp_path: Path) -> None:
    missing = SkillTarget("project", tmp_path / "p" / ".claude", tmp_path / "p")
    with pytest.raises(GraphCliError) as caught:
        require_target(missing)
    assert caught.value.code == "skills.target_invalid"
    assert caught.value.details["cause"] == "missing"
    (tmp_path / "p" / ".claude").mkdir(parents=True)
    require_target(missing)
    (tmp_path / "p" / ".claude" / "skills").write_text("not a directory")
    with pytest.raises(GraphCliError) as caught:
        require_target(missing)
    assert caught.value.details["cause"] == "not_a_directory"


def test_create_then_unchanged_then_replace(target: SkillTarget) -> None:
    assert not target.skills.exists()
    assert install_all(target, app_skill()) == ["create"]
    files, _ = read_tree(target.skills / "graph-cli")
    assert files[MARKER_FILE] == app_skill().marker_bytes
    assert {k: v for k, v in files.items() if k != MARKER_FILE} == app_skill().files
    snapshot = sorted(target.config_dir.rglob("*"))
    assert install_all(target, app_skill()) == ["unchanged"]
    assert sorted(target.config_dir.rglob("*")) == snapshot
    assert install_all(target, app_skill("two")) == ["replace"]
    assert (
        read_tree(target.skills / "graph-cli")[0]["SKILL.md"] == app_skill("two").files["SKILL.md"]
    )
    assert not (target.config_dir / STAGING_DIR).exists()


def test_a_new_program_version_rewrites_only_the_marker(target: SkillTarget) -> None:
    install_all(target, app_skill())
    newer = rendered(
        "graph-cli",
        "app",
        app_skill().files,
        program={"name": "graph", "version": "0.2.0"},
        source=app_skill().marker.source,
    )
    assert install_all(target, newer) == ["replace"]
    assert statuses(target, newer) == {"graph-cli": "fresh"}


def test_foreign_directories_are_refused_unless_forced(target: SkillTarget) -> None:
    put(target.skills / "graph-cli", {"SKILL.md": b"mine"})
    assert actions(target, app_skill()) == ["refused"]
    with pytest.raises(GraphCliError) as caught:
        install_all(target, app_skill())
    assert caught.value.code == "skills.not_owned"
    assert caught.value.details["refused"] == [{"skill": "graph-cli", "installed": "foreign"}]
    assert caught.value.details["scope"] == "project"
    assert read_tree(target.skills / "graph-cli")[0] == {"SKILL.md": b"mine"}
    assert install_all(target, app_skill(), force=True) == ["replace"]
    assert statuses(target, app_skill()) == {"graph-cli": "fresh"}


def test_a_foreign_file_with_our_name_is_foreign(target: SkillTarget) -> None:
    target.skills.mkdir()
    (target.skills / "graph-cli").write_text("x")
    assert statuses(target, app_skill()) == {"graph-cli": "foreign"}
    assert install_all(target, app_skill(), force=True) == ["replace"]
    assert (target.skills / "graph-cli").is_dir()


def test_hand_edits_are_refused_unless_forced(target: SkillTarget) -> None:
    install_all(target, app_skill())
    (target.skills / "graph-cli" / "SKILL.md").write_text("edited")
    assert actions(target, app_skill()) == ["refused"]
    with pytest.raises(GraphCliError) as caught:
        install_all(target, app_skill())
    assert caught.value.code == "skills.modified"
    assert install_all(target, app_skill(), force=True) == ["replace"]


def test_a_refused_skill_holds_back_only_itself(target: SkillTarget) -> None:
    install_all(target, app_skill(), graph_skill())
    (target.skills / "graph-example" / "SKILL.md").write_text("edited")
    plan = plan_install(target, scan(target), (app_skill("two"), graph_skill("h")), force=False)
    assert [s.action for s in plan.steps] == ["replace", "refused"]
    error = refusal(plan)
    assert error is not None and error.code == "skills.modified"
    apply_install(plan)
    assert statuses(target, app_skill("two"), graph_skill("h")) == {
        "graph-cli": "fresh",
        "graph-example": "modified",
    }
    assert (target.skills / "graph-example" / "SKILL.md").read_text() == "edited"


def test_legacy_marker_counts_as_ours(target: SkillTarget) -> None:
    put(target.skills / "graph-cli", {**app_skill().files, LEGACY_MARKER_FILE: b"old"})
    assert statuses(target, app_skill()) == {"graph-cli": "stale"}
    assert install_all(target, app_skill()) == ["replace"]
    assert not (target.skills / "graph-cli" / LEGACY_MARKER_FILE).exists()
    assert statuses(target, app_skill()) == {"graph-cli": "fresh"}


def test_every_check_status(target: SkillTarget) -> None:
    assert statuses(target, app_skill()) == {"graph-cli": "missing"}
    install_all(target, app_skill(), graph_skill())
    assert statuses(target, app_skill(), graph_skill()) == {
        "graph-cli": "fresh",
        "graph-example": "fresh",
    }
    assert statuses(target, app_skill("two"), graph_skill()) == {
        "graph-cli": "stale",
        "graph-example": "fresh",
    }
    unavailable = {"graph-example": {"reason": "graph_not_found"}}
    assert statuses(target, app_skill(), unavailable=unavailable) == {
        "graph-cli": "fresh",
        "graph-example": "unavailable",
    }
    (target.skills / "graph-cli" / "extra.md").write_text("x")
    assert statuses(target, app_skill())["graph-cli"] == "unexpected"
    (target.skills / "graph-cli" / "SKILL.md").write_text("edited")
    assert statuses(target, app_skill())["graph-cli"] == "modified"
    (target.skills / "graph-cli" / MARKER_FILE).write_text("garbage")
    assert statuses(target, app_skill())["graph-cli"] == "modified"
    put(target.skills / "graph-other", {"SKILL.md": b"x"})
    assert statuses(target, app_skill())["graph-other"] == "foreign"


def test_unrelated_directories_are_ignored(target: SkillTarget) -> None:
    put(target.skills / "pdf", {"SKILL.md": b"x"})
    assert scan(target) == {}
    assert statuses(target, app_skill()) == {"graph-cli": "missing"}


def test_staging_leftovers_are_removed(target: SkillTarget) -> None:
    leftover = target.config_dir / STAGING_DIR / "graph-cli-dead"
    put(leftover, {"SKILL.md": b"half"})
    install_all(target, app_skill())
    assert not (target.config_dir / STAGING_DIR).exists()


def test_a_crash_between_swaps_leaves_each_skill_old_or_new(
    target: SkillTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(target, app_skill(), graph_skill())
    renames = 0
    real = os.rename

    def failing(src: object, dst: object) -> None:
        nonlocal renames
        renames += 1
        if renames == 3:  # first skill swapped (2 renames), second skill's first rename
            raise OSError(28, "No space left on device")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "rename", failing)
    with pytest.raises(GraphCliError) as caught:
        install_all(target, app_skill("two"), graph_skill("h"))
    assert caught.value.code == "skills.target_invalid"
    assert caught.value.details["cause"] == "io"
    monkeypatch.undo()
    assert statuses(target, app_skill("two"), graph_skill("h")) == {
        "graph-cli": "fresh",
        "graph-example": "stale",
    }
    assert statuses(target, app_skill("two"), graph_skill()) == {
        "graph-cli": "fresh",
        "graph-example": "fresh",
    }
    assert install_all(target, app_skill("two"), graph_skill("h")) == ["unchanged", "replace"]
    assert not (target.config_dir / STAGING_DIR).exists()
