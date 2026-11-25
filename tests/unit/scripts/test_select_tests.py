"""``scripts/select_tests.py``: which test files a change selects.

CONTRIBUTING.md, "Which tests to run", describes the rules. Small synthetic trees show each rule;
the real tree shows that they hold on it. Selecting too much costs time, selecting too little lets a
broken change through: every rule that narrows has a test that a change just beyond the rule's
reach selects the test again, ``full`` included.
"""

import importlib.util
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "select_tests", REPO_ROOT / "scripts" / "select_tests.py"
)
assert _spec is not None and _spec.loader is not None
st = importlib.util.module_from_spec(_spec)
sys.modules["select_tests"] = st
_spec.loader.exec_module(st)


class MemTree(st.Tree):
    """A tree held in memory."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.paths = sorted(files)

    def read(self, path: str) -> str:
        return self.files[path]


def _pyproject(name: str, program: str, entry: str) -> str:
    return f'[project]\nname = "{name}"\n[project.scripts]\n{program} = "{entry}"\n'


PYPROJECTS = {
    "packages/app/pyproject.toml": _pyproject("app", "graph", "app.cli:main"),
    "packages/adm/pyproject.toml": _pyproject("adm", "graph-admin", "adm.cli:main"),
    "packages/srv/pyproject.toml": _pyproject("srv", "graph-server", "srv.main:main"),
}


def tree(files: dict[str, str], *, filler: int = 12) -> MemTree:
    """Three distributions with a console script each, `filler` tests that need nothing, `files`."""
    base = {
        **PYPROJECTS,
        "packages/app/src/app/__init__.py": "",
        "packages/app/src/app/cli.py": "from app import core\n",
        "packages/app/src/app/core.py": "",
        "packages/adm/src/adm/__init__.py": "",
        "packages/adm/src/adm/cli.py": "import app.core\n",
        "packages/srv/src/srv/__init__.py": "",
        "packages/srv/src/srv/main.py": "import app.core\n",
        "tests/conftest.py": "",
        **{f"tests/test_filler_{i}.py": "def test_it() -> None: ...\n" for i in range(filler)},
        **{path: "def test_it() -> None: ...\n" for path in (*st.GUARDS, *st.DOC_CHECKS)},
    }
    return MemTree({**base, **files})


def selected(files: dict[str, str], changed: list[str], **kw: int) -> list[str] | str:
    """The sorted tests the change selects, or "full: <reason>"."""
    sel = st.select(tree(files, **kw), changed)
    return f"full: {sel.reason}" if sel.full else sorted(sel.tests)


def only(result: list[str] | str) -> list[str]:
    """The selection without the guards and the documentation checks."""
    assert isinstance(result, list), result
    return [t for t in result if t not in (*st.GUARDS, *st.DOC_CHECKS)]


# ---- imports -------------------------------------------------------------------------------------


def test_a_module_selects_the_tests_that_import_it_and_the_guards() -> None:
    files = {"tests/test_a.py": "import app.core\n", "tests/test_b.py": "import adm\n"}
    result = selected(files, ["packages/app/src/app/core.py"])
    assert only(result) == ["tests/test_a.py"]
    assert set(st.GUARDS) <= set(result)


def test_imports_inside_functions_and_under_type_checking_count() -> None:
    files = {
        "packages/app/src/app/lazy.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from app import typed\n"
            "def f() -> None:\n    import app.deep\n"
        ),
        "packages/app/src/app/deep.py": "",
        "packages/app/src/app/typed.py": "",
        "tests/test_lazy.py": "from app import lazy\n",
    }
    for module in ("deep", "typed", "lazy"):
        assert only(selected(files, [f"packages/app/src/app/{module}.py"])) == [
            "tests/test_lazy.py"
        ]


def test_relative_imports_and_submodules_imported_by_name_count() -> None:
    files = {
        "packages/app/src/app/sub/__init__.py": "from . import leaf\nfrom ..core import X\n",
        "packages/app/src/app/sub/leaf.py": "",
        "tests/test_rel.py": "from app import sub\n",
        "tests/test_by_name.py": "from app.sub import leaf\n",
        "tests/test_other.py": "import adm\n",
    }
    assert only(selected(files, ["packages/app/src/app/sub/leaf.py"])) == [
        "tests/test_by_name.py",
        "tests/test_rel.py",
    ]
    assert "tests/test_rel.py" in only(selected(files, ["packages/app/src/app/core.py"]))


def test_a_package_init_runs_with_its_modules() -> None:
    files = {"tests/test_deep.py": "import app.core\n", "tests/test_adm.py": "import adm.cli\n"}
    assert only(selected(files, ["packages/app/src/app/__init__.py"])) == [
        "tests/test_adm.py",  # adm.cli imports app.core
        "tests/test_deep.py",
    ]
    assert only(selected(files, ["packages/adm/src/adm/__init__.py"])) == ["tests/test_adm.py"]


def test_a_module_named_in_a_string_counts() -> None:
    files = {
        "packages/app/src/app/tool.py": "",
        "packages/app/src/app/workers/__init__.py": "",
        "packages/app/src/app/workers/one.py": "",
        "tests/test_m.py": 'import subprocess\nsubprocess.run(["python", "-m", "app.tool"])\n',
        "tests/test_f.py": 'NAME = "one"\nSPEC = f"app.workers.{NAME}"\n',
        "tests/test_doc.py": '"""app.tool is named only in a docstring."""\n',
    }
    assert only(selected(files, ["packages/app/src/app/tool.py"])) == ["tests/test_m.py"]
    assert only(selected(files, ["packages/app/src/app/workers/one.py"])) == ["tests/test_f.py"]


def test_a_script_file_named_in_a_string_counts() -> None:
    files = {
        "tests/support/__init__.py": "",
        "tests/support/child.py": "import app.core\n",
        "tests/test_c.py": 'CHILD = "support" + "/child.py"\n',
        "tests/test_d.py": "",
    }
    assert only(selected(files, ["tests/support/child.py"])) == ["tests/test_c.py"]


def test_conftest_files_above_a_test_count_but_not_the_root_one() -> None:
    files = {
        "tests/area/conftest.py": "import app.core\n",
        "tests/area/test_in.py": "",
        "tests/test_out.py": "",
    }
    assert only(selected(files, ["packages/app/src/app/core.py"])) == ["tests/area/test_in.py"]
    # the root conftest.py is loaded for every test, so a change to it is the whole suite
    assert str(selected(files, ["tests/conftest.py"])).startswith("full")


def test_tests_are_selected_when_a_helper_they_import_changes() -> None:
    files = {
        "tests/support/__init__.py": "",
        "tests/support/helper.py": "",
        "tests/test_h.py": "from support import helper\n",
        "tests/test_i.py": "",
    }
    assert only(selected(files, ["tests/support/helper.py"])) == ["tests/test_h.py"]


# ---- programs run as processes -------------------------------------------------------------------


RUN_GRAPH = 'def test_it(run_cli) -> None:\n    run_cli("graph", "x")\n'
RUN_ADMIN = 'def test_it(run_cli) -> None:\n    run_cli("graph-admin", "x")\n'
RUN_SERVER = "from harness import BIN_DIR\nCOMMAND = [BIN_DIR / 'graph-server']\n"
RUN_NONE = "def test_it(run_cli) -> None:\n    run_cli(PROGRAM)\n"


def program_tree(**tests: str) -> dict[str, str]:
    return {f"tests/test_{name}.py": text for name, text in tests.items()}


def test_a_test_that_runs_a_program_depends_on_its_entry_module() -> None:
    files = program_tree(graph=RUN_GRAPH, admin=RUN_ADMIN, server=RUN_SERVER)
    srv = ["packages/srv/src/srv/main.py"]
    adm = ["packages/adm/src/adm/cli.py"]
    app = ["packages/app/src/app/cli.py"]
    assert only(selected(files, srv)) == ["tests/test_server.py"]
    assert only(selected(files, adm)) == ["tests/test_admin.py"]
    assert only(selected(files, app)) == ["tests/test_graph.py"]
    # what the entry module imports is just as much a dependency
    assert only(selected(files, ["packages/app/src/app/core.py"])) == [
        "tests/test_admin.py",
        "tests/test_graph.py",
        "tests/test_server.py",
    ]


def test_a_runner_that_names_no_program_runs_all_of_them() -> None:
    files = program_tree(none=RUN_NONE, graph=RUN_GRAPH)
    for module in ("srv/src/srv/main", "adm/src/adm/cli", "app/src/app/cli"):
        assert "tests/test_none.py" in only(selected(files, [f"packages/{module}.py"]))


def test_a_runner_takes_the_programs_of_the_helper_it_imports() -> None:
    files = {
        "tests/support/__init__.py": "",
        "tests/support/admin.py": RUN_ADMIN,
        "tests/test_via.py": "from support import admin\ndef test_it(run_cli) -> None: ...\n",
    }
    assert only(selected(files, ["packages/srv/src/srv/main.py"])) == []
    assert only(selected(files, ["packages/adm/src/adm/cli.py"])) == ["tests/test_via.py"]


def test_subprocess_alone_is_no_program() -> None:
    files = {"tests/test_git.py": 'import subprocess\nsubprocess.run(["git", "status"])\n'}
    assert only(selected(files, ["packages/srv/src/srv/main.py"])) == []


def test_a_program_named_in_a_command_line_string() -> None:
    files = {
        "tests/test_line.py": (
            "import subprocess\n"
            'LINE = "graph-admin ontology validate"\n'
            "subprocess.run(LINE.split())\n"
        )
    }
    assert only(selected(files, ["packages/adm/src/adm/cli.py"])) == ["tests/test_line.py"]
    assert only(selected(files, ["packages/srv/src/srv/main.py"])) == []


# ---- data files ----------------------------------------------------------------------------------


def test_a_golden_selects_the_tests_that_name_it_or_its_directory() -> None:
    files = {
        "tests/goldens/storage/head.json": "{}",
        "tests/test_gold.py": 'GOLDENS = "goldens"\nPATH = GOLDENS + "/storage/head.json"\n',
        "tests/test_dir.py": 'GOLDEN = "storage"\n',
        "tests/test_other.py": 'X = "elsewhere"\n',
    }
    assert only(selected(files, ["tests/goldens/storage/head.json"])) == [
        "tests/test_dir.py",
        "tests/test_gold.py",
    ]


def test_a_data_file_that_no_test_names_is_the_whole_suite() -> None:
    files = {"tests/fixtures/odd/x.dat": "x"}
    result = selected(files, ["tests/fixtures/odd/x.dat"])
    assert isinstance(result, str) and result.startswith("full")
    assert "no test names tests/fixtures/odd/x.dat" in result


def test_the_data_root_is_the_last_resort() -> None:
    files = {"tests/goldens/odd/x.json": "{}", "tests/test_root.py": 'ROOT = "goldens"\n'}
    assert only(selected(files, ["tests/goldens/odd/x.json"])) == ["tests/test_root.py"]


CLAIM = '@pytest.mark.doc_claim("{}.speed")\ndef test_it() -> None: ...\n'


def test_a_document_selects_its_tests_the_claims_and_the_checks() -> None:
    files = {
        "docs/guide.md": "# Guide\n",
        "tests/test_reads.py": 'from pathlib import Path\nTEXT = Path("docs") / "guide.md"\n',
        "tests/test_claims.py": CLAIM.format("guide"),
        "tests/test_nope.py": "",
    }
    result = selected(files, ["docs/guide.md"])
    assert isinstance(result, list)
    assert {"tests/test_reads.py", "tests/test_claims.py", *st.DOC_CHECKS} <= set(result)
    assert "tests/test_nope.py" not in result


def test_a_claim_of_another_document_does_not_select() -> None:
    files = {
        "docs/guide.md": "# Guide\n",
        "tests/test_claims.py": CLAIM.format("other"),
    }
    assert "tests/test_claims.py" not in selected(files, ["docs/guide.md"])


def test_a_file_in_a_package_selects_the_modules_that_name_its_directory() -> None:
    files = {
        "packages/app/src/app/assets/page.html": "<html>",
        "packages/app/src/app/page.py": 'ASSETS = "assets"\n',
        "tests/test_page.py": "import app.page\n",
        "tests/test_core.py": "import app.core\n",
    }
    assert only(selected(files, ["packages/app/src/app/assets/page.html"])) == [
        "tests/test_page.py"
    ]


def test_a_file_in_a_package_that_nothing_names_selects_the_modules_next_to_it() -> None:
    files = {"packages/app/src/app/data.yaml": "a: 1\n", "tests/test_core.py": "import app.core\n"}
    assert only(selected(files, ["packages/app/src/app/data.yaml"])) == ["tests/test_core.py"]


# ---- tests, guards, tree scanners ----------------------------------------------------------------


def test_a_changed_test_runs_itself_and_the_claims_check() -> None:
    result = selected({"tests/test_new.py": ""}, ["tests/test_new.py"])
    assert isinstance(result, list)
    assert "tests/test_new.py" in result and "tests/test_doc_claims.py" in result
    assert only(selected({}, ["tests/test_filler_3.py"])) == ["tests/test_filler_3.py"]


def test_a_deleted_test_selects_no_missing_file() -> None:
    result = selected({}, ["tests/test_gone.py"])
    assert isinstance(result, list) and "tests/test_gone.py" not in result


def test_a_new_module_that_nothing_imports_is_read_by_the_tree_scanners() -> None:
    files = {
        "packages/app/src/app/new.py": "",
        "tests/test_scan.py": 'from pathlib import Path\nSRC = Path("packages")\n',
        "tests/test_no.py": "",
    }
    assert only(selected(files, ["packages/app/src/app/new.py"])) == ["tests/test_scan.py"]


def test_the_guards_and_the_documentation_checks_exist_in_the_repository() -> None:
    for path in (*st.GUARDS, *st.DOC_CHECKS):
        assert (REPO_ROOT / path).is_file(), path


def test_a_changed_test_selects_the_tests_that_import_it() -> None:
    files = {
        "tests/test_leak.py": "DENY = ['x']\n",
        "tests/test_user.py": "from test_leak import DENY\n",
        "tests/test_other.py": "",
    }
    assert only(selected(files, ["tests/test_leak.py"])) == [
        "tests/test_leak.py",
        "tests/test_user.py",
    ]


def test_the_skills_root_counts_beside_the_files_that_are_named() -> None:
    files = {
        "skills/s/SKILL.md": "# skill\n",
        "skills/s/references/a.md": "a\n",
        "tests/support/__init__.py": "",
        "tests/support/skills.py": 'DIR = "skills"\n',
        "tests/test_fresh.py": "from support import skills\n",
        "tests/test_named.py": 'X = "a.md"\n',
    }
    result = only(selected(files, ["skills/s/references/a.md"]))
    assert result == ["tests/test_fresh.py", "tests/test_named.py"]


def test_the_goldens_root_counts_only_when_nothing_else_names_the_file() -> None:
    files = {
        "tests/goldens/g/x.json": "{}",
        "tests/support/__init__.py": "",
        "tests/support/fixtures.py": 'GOLDENS = "goldens"\n',
        "tests/test_hub.py": "from support import fixtures\n",
        "tests/test_x.py": 'NAME = "x.json"\n',
    }
    assert only(selected(files, ["tests/goldens/g/x.json"])) == ["tests/test_x.py"]


def test_a_module_that_is_gone_is_the_whole_suite() -> None:
    files = {"tests/support/__init__.py": "", "tests/test_h.py": ""}
    for path in (
        "packages/app/src/app/removed.py",  # the importers cannot be followed in the new tree
        "tests/support/clock.py",
        "tests/area/conftest.py",  # the fixtures of the tests below it are gone
    ):
        result = selected(files, [path])
        assert isinstance(result, str) and f"{path} no longer exists" in result, path


def test_a_deleted_test_file_is_no_reason_for_the_whole_suite() -> None:
    assert isinstance(selected({}, ["tests/test_removed.py"]), list)


def test_a_file_in_a_package_that_nothing_reads_is_the_whole_suite() -> None:
    for path in ("packages/app/README.md", "packages/app/src/new.txt"):
        result = selected({}, [path])
        assert isinstance(result, str) and "no test names" in result, path


# ---- the whole suite -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        ".dockerignore",
        "tests/conftest.py",
        "tests/harness.py",
        "tests/support/launcher.py",
        "tests/support/output_schemas.py",
        "tests/support/doc_claims.py",
        "packages/app/pyproject.toml",
        "scripts/select_tests.py",
        "scripts/ci-select-tests.sh",
        "scripts/ci-reuse-pr-run.sh",
        ".github/workflows/check.yml",
        ".github/actions/setup/action.yml",
        "tests/ci-select/run.sh",
        "Makefile",
        "unknown/place.txt",
    ],
)
def test_changes_that_can_affect_any_test_or_that_nothing_places_run_everything(path: str) -> None:
    result = selected({}, [path])
    assert isinstance(result, str) and result.startswith("full"), path


def test_other_workflows_and_scripts_select_only_the_guards() -> None:
    for path in (
        ".github/workflows/build.yml",
        "scripts/smoke_image.sh",
        "tests/integration/run.sh",
    ):
        result = selected({}, [path])
        assert isinstance(result, list) and set(result) == set(st.GUARDS), path


def test_more_than_the_share_selected_is_the_whole_suite() -> None:
    files = {f"tests/test_n{i}.py": "import app.core\n" for i in range(40)}
    result = selected(files, ["packages/app/src/app/core.py"])
    assert isinstance(result, str) and "over 70%" in result


def test_the_share_is_of_all_test_files() -> None:
    files = {f"tests/test_n{i}.py": "import app.core\n" for i in range(5)}
    assert isinstance(selected(files, ["packages/app/src/app/core.py"], filler=40), list)


def test_a_path_that_is_not_plain_is_the_whole_suite() -> None:
    result = selected({"tests/test_a b.py": ""}, ["tests/test_a b.py"])
    assert isinstance(result, str) and "unsafe test path" in result


def test_a_relative_path_of_the_changed_files_works_with_another_root(tmp_path: Path) -> None:
    (tmp_path / "root" / "tests").mkdir(parents=True)
    (tmp_path / "root" / "tests" / "test_a.py").write_text("")
    (tmp_path / "changed").write_bytes(b"README.md\0")
    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "select_tests.py"),
            "--changed-files",
            "changed",
            "--root",
            "root",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert b"internal error" not in done.stderr, done.stderr
    assert done.returncode == 0


def test_every_selected_path_is_plain() -> None:
    real = st.select(st.FsTree(str(REPO_ROOT)), ["docs/storage.md", "tests/test_docs.py"])
    assert not real.full
    assert all(st.SAFE_PATH.fullmatch(t) for t in real.tests)


def test_a_file_that_cannot_be_parsed_is_the_whole_suite(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def broken(:\n")
    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "select_tests.py"),
            "--changed-files",
            "-",
            "--root",
            str(tmp_path),
        ],
        input=b"docs/x.md\0",
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout == b"full\n"
    assert b"internal error" in done.stderr


# ---- the real tree -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real() -> Iterator["st.Graph"]:
    yield st.Graph(st.FsTree(str(REPO_ROOT)))


def run_real(graph: "st.Graph", changed: list[str]) -> "st.Selection":
    return st.Selector(graph).run(changed)


def test_the_entry_modules_are_those_of_the_three_programs(real: "st.Graph") -> None:
    assert sorted(real.entries) == ["graph", "graph-admin", "graph-server"]


def test_a_document_selects_a_handful_of_tests(real: "st.Graph") -> None:
    sel = run_real(real, ["docs/storage.md"])
    assert not sel.full
    assert set(st.DOC_CHECKS) <= set(sel.tests)
    assert len(sel.tests) < 0.05 * sel.total
    assert not any(t.startswith(("tests/server/", "tests/unit/kernel/")) for t in sel.tests)


def test_a_server_module_does_not_select_the_tests_that_run_only_the_app(real: "st.Graph") -> None:
    sel = run_real(real, ["packages/graph_cli_server/src/graph_cli_server/uploads.py"])
    assert not sel.full
    assert "tests/unit/server/test_uploads.py" in sel.tests
    assert "tests/server/test_enroll.py" in sel.tests  # runs graph-server
    for app_only in ("tests/cli/test_app_write.py", "tests/unit/kinetic/test_recipes.py"):
        assert app_only not in sel.tests


def test_a_module_of_the_app_selects_the_tests_that_import_or_run_it(real: "st.Graph") -> None:
    sel = run_real(real, ["packages/graph_cli/src/graph_cli/kinetic/recipes.py"])
    assert not sel.full
    assert "tests/unit/kinetic/test_recipes.py" in sel.tests  # imports it
    assert "tests/cli/test_app_write.py" in sel.tests  # runs `graph`
    assert "tests/unit/storage/test_file_store.py" not in sel.tests


def test_the_deepest_modules_are_the_whole_suite(real: "st.Graph") -> None:
    sel = run_real(real, ["packages/graph_cli/src/graph_cli/kernel/ids.py"])
    assert sel.full and "over 70%" in sel.reason


def test_every_selected_test_has_a_reason_it_depends_on_the_change(real: "st.Graph") -> None:
    module = "packages/graph_cli_admin/src/graph_cli_admin/services_admin/usage.py"
    sel = run_real(real, [module])
    closures = st.Closures(real)
    for test, why in sel.tests.items():
        if why == "imports a changed module":
            assert module in closures.of(test), test


def test_every_data_file_of_the_repository_is_placed_or_runs_everything(real: "st.Graph") -> None:
    """No file of a data tree falls through the classification (it is named by a test, or full)."""
    stray = [
        p
        for p in real.tree.paths
        if p.startswith(("tests/goldens/", "tests/fixtures/", "docs/", "skills/", "examples/"))
        and not p.endswith(".py")
        and st.classify(p) not in {"data", "document"}
    ]
    assert stray == []


def test_the_generated_app_skill_selects_its_freshness_check(real: "st.Graph") -> None:
    sel = run_real(real, ["skills/graph-cli/SKILL.md"])
    assert not sel.full
    assert "tests/skills/test_app_skill.py" in sel.tests
    assert "tests/test_docs.py" in sel.tests


def test_a_changed_test_module_selects_the_tests_that_import_it_in_the_repository(
    real: "st.Graph",
) -> None:
    sel = run_real(real, ["tests/test_vocabulary_leak.py"])
    assert not sel.full
    assert "tests/cli/test_admin_init.py" in sel.tests
