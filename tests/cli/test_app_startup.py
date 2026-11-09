"""S: a command imports only what it runs (startup time).

Each ``graph`` call is a new process, so the modules it imports are a large part
of its time. Reads must start without the write, file and skill modules and
without the Cypher engine; ``graph query`` loading the engine shows that the
check sees lazy imports. Asserts module names, not milliseconds: timings are
too noisy for a test.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.app import ENV, create_graph
from support.fixtures import FIXTURES
from support.roles import load_roles

from graph_cli.kernel.changeset import read_documents

ONTOLOGY = "inventory"
# Run main() and write the names of all modules imported by exit to argv[1].
PROBE = """\
import atexit, sys
out = sys.argv.pop(1)
atexit.register(lambda: open(out, "w").write("\\n".join(sorted(sys.modules))))
from graph_cli.app_cli.main import main
main()
"""
NOT_FOR_READS = (
    "graph_cli.services.write",
    "graph_cli.services.files",
    "graph_cli.skills",
    "graph_cli.query.cypher.parser",
    "graph_cli.query.cypher.execute",
)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ONTOLOGY)
    seeds = read_documents((FIXTURES / "seeds" / ONTOLOGY / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


def loaded(env: dict[str, str], tmp_path: Path, *args: str) -> set[str]:
    out = tmp_path / "modules.txt"
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(out), *args, "--json"],
        capture_output=True,
        text=True,
        env={**env, **ENV},
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return set(out.read_text().splitlines())


def test_reads_start_without_write_file_skill_and_cypher_modules(
    root: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    kind = load_roles(ONTOLOGY).term("lifecycle_type")
    for args in (
        ("graphs",),
        ("find", "--type", kind, "--limit", "1"),
        ("describe", "types"),
        ("describe", "type", kind),
        ("whoami",),
    ):
        modules = loaded(isolated_env, tmp_path, *args)
        assert "graph_cli.services.read" in modules, args
        assert not {m for m in modules if m.startswith(NOT_FOR_READS)}, args


def test_query_loads_the_cypher_engine(
    root: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    modules = loaded(isolated_env, tmp_path, "query", "MATCH (n) RETURN count(n) AS n")
    assert {"graph_cli.query.cypher.parser", "graph_cli.query.cypher.execute"} <= modules


def test_only_reads_of_sealed_data_load_the_cipher_library(
    root: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """Plan T16: the cipher library costs startup time; commands that open no store skip it."""
    for args in (("graphs",), ("whoami",)):
        assert "cryptography" not in loaded(isolated_env, tmp_path, *args), args
    kind = load_roles(ONTOLOGY).term("lifecycle_type")
    assert "cryptography" in loaded(isolated_env, tmp_path, "find", "--type", kind)
