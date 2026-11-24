"""docs/storage.md matches what the file store writes.

- The directory tree of "FileStore" names every path of a real data root: an installation
  with a graph, an entity, a token and an enrollment code is built with the CLIs, and each
  of its paths must be an entry of the tree, a directory on the way to one, or lie in an
  entry that is a directory without children in the tree (its content is described in
  words). Entries the run does not create (stored files, a migration marker) are not
  required to exist.
- The format-2 manifest example of "Manifest" equals `tests/goldens/storage/manifest_s3.toml`
  (kept equal to the writer by `tests/unit/storage/test_manifest.py`): the same tables,
  those named in its leading comment included, the same keys, and the same values except
  placeholders (`"<uuid>"`).
"""

import re
import tomllib
from typing import Any

from harness import REPO_ROOT, CliRunner
from support.admin import admin, init
from support.app import app
from support.fixtures import GOLDENS

STORAGE_DOC = REPO_ROOT / "docs" / "storage.md"
ONTOLOGY = REPO_ROOT / "tests" / "fixtures" / "ontologies" / "notes_and_sources" / "1.1.0"
TREE_BLOCK = re.compile(r"^```\n(.*?)^```", re.MULTILINE | re.DOTALL)
TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.MULTILINE | re.DOTALL)
COLUMN_GAP = re.compile(r" {2,}")
PLACEHOLDER = re.compile(r"<[^>]+>")
COMMENTED_TABLE = re.compile(r"\[([a-z.]+)\]")

Entry = tuple[tuple[str, ...], bool]  # (segments, leaf directory)


def _section(text: str, heading: str) -> str:
    _, found, rest = text.partition(f"\n## {heading}\n")
    assert found, f"no section {heading!r}"
    return rest.split("\n## ", 1)[0]


def _is_path(token: str) -> bool:
    """A path has no space outside its placeholders and a `/` or a `.`."""
    return " " not in PLACEHOLDER.sub("x", token) and ("/" in token or "." in token)


def _tree(block: str) -> list[Entry]:
    """The entries of a directory tree drawn with two spaces per level: per entry its path
    segments below the root and whether it is a directory without children in the tree.
    A line may hold several entries; its first token that is no path starts the comment."""
    lines = block.splitlines()[1:]  # the first line is the root
    parsed: list[tuple[int, list[str]]] = []
    for line in lines:
        depth = (len(line) - len(line.lstrip(" "))) // 2
        tokens = COLUMN_GAP.split(line.strip())
        names = []
        for token in tokens:
            if not _is_path(token):
                break
            names.append(token)
        parsed.append((depth, names))
    entries: list[Entry] = []
    directories: dict[int, tuple[str, ...]] = {0: ()}  # per depth, the last directory
    for index, (depth, names) in enumerate(parsed):
        parent = directories[depth - 1]
        has_children = index + 1 < len(parsed) and parsed[index + 1][0] > depth
        for name in names:
            segments = (*parent, *name.rstrip("/").split("/"))
            entries.append((segments, name.endswith("/") and not has_children))
        if names and names[-1].endswith("/"):
            directories[depth] = (*parent, *names[-1].rstrip("/").split("/"))
    return entries


def _matches(pattern: str, name: str) -> bool:
    regex = "".join(
        "[^/]+" if PLACEHOLDER.fullmatch(part) else re.escape(part)
        for part in re.split(r"(<[^>]+>)", pattern)
        if part
    )
    return re.fullmatch(regex, name) is not None


def _uncovered(paths: list[tuple[str, ...]], entries: list[Entry]) -> list[str]:
    def covered(path: tuple[str, ...]) -> bool:
        for segments, leaf in entries:
            n = min(len(path), len(segments))
            if not all(_matches(p, s) for p, s in zip(segments[:n], path[:n], strict=True)):
                continue
            if len(path) <= len(segments) or leaf:
                return True
        return False

    return ["/".join(path) for path in paths if not covered(path)]


def _manifest_example(section: str) -> tuple[dict[str, Any], set[str]]:
    """The TOML example and the tables its comments say it shares with format 1."""
    (block,) = TOML_BLOCK.findall(section)
    shared = {
        table
        for line in block.splitlines()
        if line.startswith("#")
        for table in COMMENTED_TABLE.findall(line)
    }
    return tomllib.loads(block), shared


def _differences(example: Any, golden: Any, path: str = "") -> list[str]:
    if isinstance(example, dict) and isinstance(golden, dict):
        found = [f"{path}{k}: missing" for k in sorted(set(golden) - set(example))]
        found += [f"{path}{k}: unknown" for k in sorted(set(example) - set(golden))]
        for key in sorted(set(example) & set(golden)):
            found += _differences(example[key], golden[key], f"{path}{key}.")
        return found
    if isinstance(example, str) and PLACEHOLDER.fullmatch(example):
        return []
    return [] if example == golden else [f"{path.rstrip('.')}: {example!r} != {golden!r}"]


def _drop(document: dict[str, Any], dotted: str) -> None:
    """Remove a table by its dotted name, and parents it leaves empty."""
    key, _, rest = dotted.partition(".")
    if rest and isinstance(document.get(key), dict):
        _drop(document[key], rest)
        if not document[key]:
            del document[key]
    elif not rest:
        document.pop(key, None)


def test_tree_names_every_path_of_a_data_root(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    root = init(run_cli, isolated_env)
    assert admin(run_cli, "graph", "create", "notes", "--ontology", str(ONTOLOGY)).code == 0
    created = app(
        run_cli, "create", "nas:Topic", "--set", "label=A", env={"GRAPH_CLI_GRAPH": "notes"}
    )
    assert created.code == 0, created.envelope
    assert admin(run_cli, "token", "create", "reader", "--reason", "test").code == 0
    assert admin(run_cli, "enroll", "create", "--project", "example_project").code == 0
    paths = [p.relative_to(root).parts for p in sorted(root.rglob("*"))]
    (block,) = TREE_BLOCK.findall(_section(STORAGE_DOC.read_text(), "FileStore"))
    assert _uncovered(paths, _tree(block)) == []


def test_manifest_example_matches_the_golden() -> None:
    example, shared = _manifest_example(_section(STORAGE_DOC.read_text(), "Manifest"))
    golden = tomllib.loads((GOLDENS / "storage" / "manifest_s3.toml").read_text())
    for table in shared:
        _drop(golden, table)
    assert shared
    assert _differences(example, golden) == []


def test_positive_controls() -> None:
    tree = (
        "<root>/\n"
        "  .lock                 the lock\n"
        "  a/                    a directory\n"
        "  b/\n"
        "    x.json  y/   two entries\n"
        "    c/<id>/             leaf, derived\n"
        "    d/e/<hash>          files\n"
        "  g/<some id>/   (events-<first seq>.jsonl segments)\n"
    )
    entries = _tree(tree)
    assert entries == [
        ((".lock",), False),
        (("a",), True),
        (("b",), False),
        (("b", "x.json"), False),
        (("b", "y"), True),
        (("b", "c", "<id>"), True),
        (("b", "d", "e", "<hash>"), False),
        (("g", "<some id>"), True),
    ]
    paths = [
        (".lock",),
        ("a", "deep", "file"),
        ("b", "c", "1", "r1.json"),
        ("b", "d"),
        ("b", "d", "e", "h1"),
        ("b", "d", "e", "h1", "extra"),
        ("b", "z.json"),
        ("other",),
    ]
    assert _uncovered(paths, entries) == ["b/d/e/h1/extra", "b/z.json", "other"]
    assert _matches("events-<seq>.jsonl", "events-1.jsonl")
    assert not _matches("events-<seq>.jsonl", "events-1.json")

    section = (
        "```toml\nformat_version = 2\n# [graph] and [alias.prefixes] as before\n"
        '[store]\nid = "<uuid>"\nn = 1\n```\n'
    )
    example, shared = _manifest_example(section)
    assert shared == {"graph", "alias.prefixes"}
    golden = {"format_version": 2, "store": {"id": "real", "n": 2, "m": 0}}
    assert _differences(example, golden) == ["store.m: missing", "store.n: 1 != 2"]
    nested: dict[str, Any] = {"graph": {}, "alias": {"prefixes": {}, "other": 1}}
    _drop(nested, "alias.prefixes")
    assert nested == {"graph": {}, "alias": {"other": 1}}
    _drop(nested, "alias.other")
    _drop(nested, "graph")
    assert nested == {}
