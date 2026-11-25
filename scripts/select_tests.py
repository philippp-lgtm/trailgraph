#!/usr/bin/env python3
"""select_tests.py - the test files that a change can affect.

    select_tests.py --base REF [--head REF]    the change is `git diff REF...HEAD` (or `...--head`)
    select_tests.py --changed-files FILE       the change is the NUL-separated paths in FILE
                                                (`-`: standard input), read from --root

Prints either the single word `full` or the pytest paths to run, one per line, and a summary of
the reasons on standard error (`--explain` adds the first reason of every selected file). It never
fails: any doubt or internal error prints `full`. Standard library only, Python 3.12 or later.
CONTRIBUTING.md ("Which tests to run") describes the rules for users; this is how they work.

* Every changed path is classified (`classify`). A change to the test infrastructure (pytest
  configuration, the root `conftest.py`, `tests/harness.py`, the pytest plugins), the dependencies,
  the image, the CI plumbing that makes the selection, this script, and any file that no rule
  places selects everything: `full`.
* Python modules form a static import graph: every `import` statement of every module under
  `packages/*/src` and `tests/` (also inside functions and under `TYPE_CHECKING`), relative imports
  resolved, plus the module names and `<name>.py` file names written as string literals
  (`python -m x.y`, `pytest_plugins`, `f"x.workers.{name}"`). A test file is selected when a
  changed module is in its transitive closure: what it imports, the `conftest.py` files in the
  directories above it, and the entry module of every program it runs as a process (`graph`,
  `graph-admin`, `graph-server`, from the packages' `[project.scripts]`). A test runs a program
  when it, or a helper it imports, uses `run_cli`, `BIN_DIR` or the launcher and names the program
  as a string literal; when it does and names none, it runs all three.
* A file that is no module selects the tests that name it. A file of the repository (goldens,
  fixtures, documents, skills, examples) is named by tests, by a string literal with its name or
  with a directory it is in; only when no test does, with the data root (`goldens`, `docs`, ...).
  A file inside a package is read by the modules that name its directory, else by those next to
  it, and by the tests that name the directory. A document also selects the tests that back its
  claims (`doc_claim`) and the documentation checks.
* A change under `packages/` also selects the tests that scan the source tree; any change under
  `tests/` the test that reads the claim markers. A few cheap guard tests are always added.
* When the selection holds more than `FULL_SHARE` of all test files, the answer is `full`: the
  same cost, and simpler.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tomllib
import traceback
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

FULL_SHARE = 0.70

# Loaded for every test through pytest (plugins, fixtures): not part of any test's closure, because
# it imports both programs for the schema checks. A change to it is `full`; a module that imports
# a plugin helper itself (e.g. `check_text`) depends on it like on any module.
ROOT_CONFTEST = "tests/conftest.py"

# Run with every selection: cheap, they scan the whole tree or guard the rules selection relies on.
GUARDS = (
    "tests/test_architecture.py",
    "tests/test_source_rules.py",
    "tests/test_vocabulary_leak.py",
)
# Run with every change to a document: they read the documents and compare them with the code.
DOC_CHECKS = (
    "tests/test_docs.py",
    "tests/test_doc_facts.py",
    "tests/test_doc_claims.py",
    "tests/test_doc_sets.py",
)
# Files whose change can affect any test, or the way this script works: the answer is `full`.
FULL_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        ".dockerignore",
        ".python-version",
        ".gitignore",
        ROOT_CONFTEST,
        "tests/harness.py",
        "tests/support/launcher.py",
        "tests/support/output_schemas.py",
        "tests/support/doc_claims.py",
        "scripts/select_tests.py",
    }
)
# The CI plumbing that makes and applies the selection must itself be tested in full.
FULL_PREFIXES = ("scripts/ci-", "tests/ci-")
FULL_WORKFLOW = ".github/workflows/check.yml"
# Only a path of this shape is ever printed: it ends up in a shell word and a pytest argument.
SAFE_PATH = re.compile(r"tests/[A-Za-z0-9_./-]+\.py")

PROGRAMS = ("graph", "graph-admin", "graph-server")
# Test-side modules that run programs for the tests: the tests that use them are the runners.
RUNNER_INFRA = frozenset({ROOT_CONFTEST, "tests/harness.py", "tests/support/launcher.py"})
RUNNER_SIGNALS = frozenset({"run_cli", "BIN_DIR", "launcher"})
# Directory names that say nothing about which test reads a file.
GENERIC_DIRS = frozenset({"tests", "packages", "src"})
# The roots of data trees: below them the directories name a group of files.
DATA_ROOTS = ("tests/goldens", "tests/fixtures", "docs", "skills", "examples")
VERSION = re.compile(r"\d+(\.\d+)*")
DOTTED = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*\.?")
PY_FILE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.py\b")
MAX_LITERAL = 400  # a longer string literal is prose, not a name
SUBPROCESS_CALLS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


# ---------------------------------------------------------------------------------------------
# The files of a tree


class Tree:
    """The files of a checkout or of a commit: paths and contents."""

    paths: list[str]

    def read(self, path: str) -> str:
        raise NotImplementedError


class FsTree(Tree):
    """A checkout on disk."""

    SKIP = frozenset(
        {"__pycache__", ".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}
        | {".pytest_cache", ".import_linter_cache"}
    )

    def __init__(self, root: str) -> None:
        self.root = root
        self.paths = sorted(self._walk())

    def _walk(self) -> Iterator[str]:
        for top, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in self.SKIP and not d.endswith(".egg-info")]
            for name in files:
                yield os.path.relpath(os.path.join(top, name), self.root).replace(os.sep, "/")

    def read(self, path: str) -> str:
        with open(os.path.join(self.root, path), encoding="utf-8", errors="replace") as handle:
            return handle.read()


class GitTree(Tree):
    """The tree of a commit, read through git without a checkout (one `git cat-file` process)."""

    def __init__(self, ref: str) -> None:
        self.oids: dict[str, str] = {}
        for entry in _git("ls-tree", "-r", "-z", ref).split("\0"):
            meta, _, path = entry.partition("\t")
            if path:
                self.oids[path] = meta.split()[2]
        self.paths = sorted(self.oids)
        self._batch = subprocess.Popen(
            ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

    def read(self, path: str) -> str:
        assert self._batch.stdin is not None and self._batch.stdout is not None
        self._batch.stdin.write(self.oids[path].encode() + b"\n")
        self._batch.stdin.flush()
        _, kind, size = self._batch.stdout.readline().split()
        data = self._batch.stdout.read(int(size) + 1)[:-1]
        if kind != b"blob":
            raise RuntimeError(f"{path} is not a file")
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        if self._batch.stdin:
            self._batch.stdin.close()
        self._batch.wait()


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


# ---------------------------------------------------------------------------------------------
# The module graph


@dataclass
class Module:
    path: str
    name: str
    is_package: bool
    imports: set[str] = field(default_factory=set)  # paths of the modules it imports or names
    literals: set[str] = field(default_factory=set)  # its string literals and their `/` segments
    claims: set[str] = field(default_factory=set)  # the doc_claim ids of the tests in the file
    uses_runner: bool = False
    programs: set[str] = field(default_factory=set)  # the programs it names, when it uses_runner


def module_name(path: str) -> str | None:
    """The dotted name a Python file is imported under; None for a file outside the import graph."""
    parts = path.split("/")
    if not path.endswith(".py"):
        return None
    if len(parts) > 3 and parts[0] == "packages" and parts[2] == "src":
        rest = parts[3:]
    elif parts[0] == "tests" and len(parts) > 1:
        rest = parts[1:]  # pytest's `pythonpath` is tests/
    else:
        return None
    rest[-1] = rest[-1].removesuffix(".py")
    if rest[-1] == "__init__":
        rest.pop()
    return ".".join(rest)


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and path.endswith(".py") and name.startswith("test_")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _claims(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    out: set[str] = set()
    for deco in node.decorator_list:
        if isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") == "doc_claim":
            out.update(
                a.value
                for a in deco.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )
    return out


def _calls_subprocess(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "subprocess"
        and n.attr in SUBPROCESS_CALLS
        for n in ast.walk(tree)
    )


class Graph:
    """The import graph of the packages and the tests, and what each module names."""

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        self.modules: dict[str, Module] = {}
        self.by_name: dict[str, str] = {}
        for path in tree.paths:
            name = module_name(path)
            if name is not None:
                self.modules[path] = Module(path, name, path.endswith("/__init__.py"))
                self.by_name[name] = path
        self.below: dict[str, list[str]] = defaultdict(list)  # a package name -> modules under it
        for path, mod in self.modules.items():
            parts = mod.name.split(".")
            for i in range(1, len(parts)):
                self.below[".".join(parts[:i])].append(path)
        self.tests = sorted(p for p in self.modules if is_test_file(p))
        self.by_stem: dict[str, list[str]] = defaultdict(list)  # test-side file stems
        for path in self.modules:
            if path.startswith("tests/"):
                self.by_stem[path.rsplit("/", 1)[1].removesuffix(".py")].append(path)
        for mod in self.modules.values():
            self._scan(mod)
        self.entries = self._entry_modules()

    def _entry_modules(self) -> dict[str, str]:
        """Program name -> the module its console script runs."""
        entries: dict[str, str] = {}
        for path in self.tree.paths:
            if path.startswith("packages/") and path.endswith("/pyproject.toml"):
                if path.count("/") != 2:
                    continue
                scripts = tomllib.loads(self.tree.read(path)).get("project", {}).get("scripts", {})
                for program, target in scripts.items():
                    node = self.by_name.get(str(target).split(":", 1)[0])
                    if node is not None:
                        entries[program] = node
        return entries

    def _running(self, dotted: str) -> Iterator[str]:
        """The modules that importing `dotted` runs: the packages on the way, and the module."""
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            path = self.by_name.get(".".join(parts[:i]))
            if path is not None:
                yield path

    def _scan(self, mod: Module) -> None:
        try:
            tree = ast.parse(self.tree.read(mod.path), filename=mod.path)
        except (SyntaxError, ValueError):
            raise RuntimeError(f"cannot parse {mod.path}") from None
        package = mod.name if mod.is_package else mod.name.rpartition(".")[0]
        docstrings = _docstring_ids(tree)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod.imports.update(self._running(alias.name))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:  # `from . import x`: the package, `..` its parent, ...
                    keep = package.split(".") if package else []
                    keep = keep[: max(len(keep) - (node.level - 1), 0)]
                    base = ".".join([*keep, base] if base else keep)
                mod.imports.update(self._running(base))
                for alias in node.names:
                    names.add(alias.name)
                    mod.imports.update(
                        self._running(f"{base}.{alias.name}" if base else alias.name)
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    self._literal(mod, node.value)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                mod.claims.update(_claims(node))
        mod.imports.discard(mod.path)
        if mod.path.startswith("tests/") and mod.path not in RUNNER_INFRA:
            mod.programs = {
                p
                for p in PROGRAMS
                if p in mod.literals or any(t.startswith(p + " ") for t in mod.literals)
            }
            # `subprocess` alone starts anything (git, uv): a program run only if one is named.
            mod.uses_runner = bool(names & RUNNER_SIGNALS) or (
                bool(mod.programs) and _calls_subprocess(tree)
            )

    def _literal(self, mod: Module, text: str) -> None:
        if len(text) > MAX_LITERAL:
            return
        mod.literals.add(text)
        mod.literals.update(segment for segment in text.split("/") if segment)
        for match in DOTTED.findall(text):  # a module name, or a package name that ends the prefix
            dotted = match.rstrip(".")  # of an f-string (`f"support.workers.{name}"`: all below)
            mod.imports.update(self._running(dotted))
            if match.endswith("."):
                mod.imports.update(self.below.get(dotted, ()))
        for stem in PY_FILE.findall(text):
            mod.imports.update(self.by_stem.get(stem, ()))


# ---------------------------------------------------------------------------------------------
# What each test depends on


class Closures:
    """The modules each test file depends on, transitively."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.cache: dict[str, frozenset[str]] = {}

    def _edges(self, path: str) -> set[str]:
        mod = self.graph.modules[path]
        edges = set(mod.imports)
        if mod.uses_runner:
            programs = set(mod.programs)
            # No program named: all of them, unless a helper it imports names them.
            if not programs and not any(self._names_programs(i) for i in mod.imports):
                programs = set(self.graph.entries)
            edges.update(self.graph.entries[p] for p in programs if p in self.graph.entries)
        return edges

    def _names_programs(self, path: str) -> bool:
        mod = self.graph.modules[path]
        return mod.uses_runner and bool(mod.programs)

    def of(self, test: str) -> frozenset[str]:
        if test not in self.cache:
            seen: set[str] = set()
            todo = [test, *self._conftests(test)]
            while todo:
                path = todo.pop()
                if path not in seen:
                    seen.add(path)
                    todo.extend(self._edges(path))
            self.cache[test] = frozenset(seen)
        return self.cache[test]

    def _conftests(self, test: str) -> list[str]:
        """The conftest.py files of the directories above the test (pytest loads them)."""
        found: list[str] = []
        parts = test.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            candidate = "/".join([*parts[:i], "conftest.py"])
            if candidate in self.graph.modules and candidate != ROOT_CONFTEST:
                found.append(candidate)
        return found


# ---------------------------------------------------------------------------------------------
# The change


@dataclass
class Selection:
    full: bool
    reason: str = ""
    tests: dict[str, str] = field(default_factory=dict)  # selected test file -> first reason
    notes: list[str] = field(default_factory=list)  # how many changed paths of each kind
    total: int = 0


def classify(path: str) -> str:
    """The kind of a changed path: how the selector treats it."""
    name = path.rsplit("/", 1)[-1]
    if path in FULL_FILES or path == FULL_WORKFLOW or path.startswith(FULL_PREFIXES):
        return "full"
    if path.startswith("packages/"):
        if name == "pyproject.toml":
            return "full"
        if name == "py.typed":
            return "tooling"
        return "module" if path.endswith(".py") else "package_file"
    if path.startswith("tests/"):
        if path.endswith(".py"):
            return "test" if is_test_file(path) else "module"
        if path.endswith(".sh") and path.count("/") == 2:
            return "tooling"  # tests/<area>/run.sh: a CI step runs it, not pytest
        return "data"
    if path.startswith("docs/") or (path.endswith(".md") and "/" not in path):
        return "document"
    if path.startswith(("skills/", "examples/", "docker/")) or path == "compose.yaml":
        return "data"
    if path.startswith((".github/", "scripts/")):
        return "tooling"
    return "unplaced"


def _data_tokens(path: str) -> tuple[str, list[str], list[str]]:
    """(file name, directory names, data root) that a test names to read a file of the repository.

    The directories are those below the data root, without version numbers."""
    parts = path.split("/")
    root = next((r for r in DATA_ROOTS if path.startswith(r + "/")), None)
    if root is None:
        return parts[-1], [d for d in parts[:-1] if d not in GENERIC_DIRS], []
    below = parts[len(root.split("/")) : -1]
    return parts[-1], [d for d in below if not VERSION.fullmatch(d)], [root.rsplit("/", 1)[-1]]


class Selector:
    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.closures = Closures(graph)
        self.test_set = set(graph.tests)
        self.sel = Selection(full=False, total=len(graph.tests))

    def _add(self, tests: Iterable[str], reason: str) -> int:
        """Selects `tests`; returns how many test files that is (also those selected before)."""
        count = 0
        for test in tests:
            if test in self.test_set:
                count += 1
                self.sel.tests.setdefault(test, reason)
        return count

    def _depending_on(self, modules: set[str]) -> list[str]:
        return [t for t in self.graph.tests if modules & self.closures.of(t)] if modules else []

    def _naming(
        self, names: Iterable[str], *, part_of: str | None = None, test_side: bool = True
    ) -> set[str]:
        """The modules with a string literal that is one of `names` (or a `/` segment of one), or
        that contains `part_of`. `test_side`: only modules of tests/: a file of the repository is
        read by tests, the packages never see it."""
        wanted = set(names)
        found: set[str] = set()
        for path, mod in self.graph.modules.items():
            if test_side and not path.startswith("tests/"):
                continue
            if wanted & mod.literals or (part_of and any(part_of in t for t in mod.literals)):
                found.add(path)
        return found

    def _repository_file(self, path: str) -> int:
        """Selects the tests that name a file of the repository (outside the packages): by its
        name or a directory it is in, else by the data root (`goldens`, `docs`, ...), whose name
        a helper that half of the suite imports mostly holds. Returns how many."""
        name, directories, root = _data_tokens(path)
        for readers in (self._naming(directories, part_of=name), self._naming(root)):
            count = self._add(self._depending_on(readers), f"names {path}")
            if count:
                return count
        return 0

    def _package_file(self, path: str) -> int:
        """A file inside a package that is no module: the modules that name the directory it is in
        are its readers (and the tests that name it); with none, the modules next to it."""
        directory = path.rsplit("/", 1)[0]
        modules = {p for p in self.graph.modules if p.rsplit("/", 1)[0] == directory}
        named = None
        while not modules and directory.count("/") > 3:  # up to packages/<dist>/src/<package>
            named = directory.rsplit("/", 1)[1]
            directory = directory.rsplit("/", 1)[0]
            modules = {p for p in self.graph.modules if p.rsplit("/", 1)[0] == directory}
        readers = self._naming([named], test_side=False) if named else set()
        return self._add(self._depending_on(readers or modules), f"reads {path}")

    def _document(self, path: str) -> None:
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        if path.startswith("docs/"):
            backing = [
                t
                for t in self.graph.tests
                if any(c.split(".", 1)[0] == stem for c in self.graph.modules[t].claims)
            ]
            self._add(backing, f"backs a claim of {path}")
        self._repository_file(path)

    def _full(self, reason: str) -> Selection:
        return Selection(full=True, reason=reason, notes=self.sel.notes, total=self.sel.total)

    def run(self, changed: list[str]) -> Selection:
        sel = self.sel
        present = set(self.graph.tree.paths)
        modules: set[str] = set()
        counts: dict[str, int] = defaultdict(int)
        unplaced: list[str] = []
        documents = source = test_side = False

        for path in sorted(set(changed)):
            kind = classify(path)
            counts[kind] += 1
            sel.notes = [f"{n} {k}" for k, n in sorted(counts.items())]
            if kind == "full":
                return self._full(f"{path} can affect any test")
            if kind == "module":
                modules.add(path)
                source = source or path.startswith("packages/")
                test_side = test_side or path.startswith("tests/")
            elif kind == "test":
                test_side = True
                if path in present:
                    self._add([path], f"{path} changed")
            elif kind == "document":
                documents = True
                self._document(path)
            elif kind == "package_file":
                self._package_file(path)
            elif kind == "data":
                if not self._repository_file(path):
                    unplaced.append(path)
            elif kind == "tooling":
                self._repository_file(path)  # the tests that name it, if any
            else:
                unplaced.append(path)

        if unplaced:
            more = " ..." if len(unplaced) > 5 else ""
            return self._full("no test names " + ", ".join(unplaced[:5]) + more)
        known = {p for p in modules if p in self.graph.modules}
        self._add(self._depending_on(known), "imports a changed module")
        if source:  # a module that nothing imports (new, deleted) is still read by tree scanners
            self._add(self._depending_on(self._naming(["packages"])), "scans the source tree")
        if test_side:
            self._add(["tests/test_doc_claims.py"], "scans the tests for claim markers")
        self._add(GUARDS, "guard")
        if documents:
            self._add(DOC_CHECKS, "documentation check")

        if sel.total and len(sel.tests) > FULL_SHARE * sel.total:
            share = f"{len(sel.tests)} of {sel.total} test files selected, over {FULL_SHARE:.0%}"
            return self._full(share)
        unsafe = [t for t in sel.tests if not SAFE_PATH.fullmatch(t)]
        if unsafe:
            return self._full(f"unsafe test path {unsafe[0]!r}")
        return sel


# ---------------------------------------------------------------------------------------------
# Entry points


def changed_files(args: argparse.Namespace) -> list[str]:
    if args.changed_files:
        if args.changed_files == "-":
            raw = sys.stdin.buffer.read()
        else:
            with open(args.changed_files, "rb") as handle:
                raw = handle.read()
        return [p for p in raw.decode("utf-8", "replace").split("\0") if p.strip()]
    if args.head:
        out = _git("diff", "--name-only", "--no-renames", "-z", f"{args.base}...{args.head}")
        return [p for p in out.split("\0") if p]
    # The working tree against the merge base: committed, staged, unstaged and untracked files.
    base = _git("merge-base", args.base, "HEAD").strip()
    out = _git("diff", "--name-only", "--no-renames", "-z", base)
    out += "\0" + _git("ls-files", "-z", "--others", "--exclude-standard")
    return [p for p in out.split("\0") if p]


def select(tree: Tree, changed: list[str]) -> Selection:
    return Selector(Graph(tree)).run(changed)


def render(sel: Selection, changed: list[str], explain: bool) -> tuple[str, str]:
    """(standard output, standard error)"""
    head = f"select_tests: {len(changed)} changed files"
    if sel.notes:
        head += " (" + ", ".join(sel.notes) + ")"
    if sel.full:
        return "full\n", f"{head}\nselect_tests: full: {sel.reason}\n"
    err = [head, f"select_tests: {len(sel.tests)} of {sel.total} test files"]
    if explain:
        err += [f"  {test}: {why}" for test, why in sorted(sel.tests.items())]
    return "".join(f"{t}\n" for t in sorted(sel.tests)), "\n".join(err) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prints `full` or the test files a change affects."
    )
    parser.add_argument("--base", help="the ref the change is measured from, e.g. origin/main")
    parser.add_argument("--head", help="the ref the change ends at (default: the working tree)")
    parser.add_argument("--changed-files", metavar="FILE", help="NUL-separated paths (- for stdin)")
    parser.add_argument("--root", help="the checkout to read (default: that of this script)")
    parser.add_argument("--explain", action="store_true", help="list why each file is selected")
    args = parser.parse_args(argv)
    if not args.base and not args.changed_files:
        parser.error("one of --base and --changed-files is required")
    changed: list[str] = []
    tree: Tree | None = None
    try:
        os.chdir(args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        changed = changed_files(args)
        tree = GitTree(args.head) if args.head and not args.changed_files else FsTree(".")
        sel = select(tree, changed)
    except Exception:  # fail closed: whatever went wrong, the whole suite runs
        traceback.print_exc()
        sel = Selection(full=True, reason="internal error")
    finally:
        if isinstance(tree, GitTree):
            tree.close()
    out, err = render(sel, changed, args.explain)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
