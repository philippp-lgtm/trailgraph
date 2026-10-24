"""Pointer file and graph selection (contracts B12.5, B12.6; plan A2)."""

from pathlib import Path
from uuid import UUID

import pytest

from graph_cli.catalog.pointer import (
    PointerFile,
    find_pointer,
    parse_pointer,
    select_graph,
    visible_graphs,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin

PIN = OntologyPin("b", "1.0.0", "sha256:" + "0" * 64)


def entry(name: str, n: int, state: str = "active") -> GraphEntry:
    return GraphEntry(GraphId(UUID(f"0192f000-0000-7000-8000-{n:012d}")), name, state, PIN, {}, ())  # type: ignore[arg-type]


class Catalog:
    def __init__(self, *entries: GraphEntry) -> None:
        self.entries = entries

    def list_graphs(self, *, include_archived: bool = False) -> list[GraphEntry]:
        return [e for e in self.entries if include_archived or e.state != "archived"]

    def resolve(self, ref: str) -> GraphEntry:
        for e in self.entries:
            if ref in (e.name, str(e.id)):
                return e
        raise GraphCliError("graph.not_found", f"no graph {ref!r}")


ALPHA, BETA, GAMMA = entry("alpha", 1), entry("beta", 2), entry("gamma", 3)
CATALOG = Catalog(ALPHA, BETA, GAMMA)


def pointer(text: str, path: Path = Path("/p/.graph_cli.toml")) -> PointerFile:
    found, warnings = parse_pointer(text.encode(), path=path)
    assert warnings == ()
    return found


def test_parse_and_walk_up(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "sub" / "deep").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text(
        "format_version = 1\n"
        'project = "example_project"\n'
        'graphs = ["alpha", "beta"]\n'
        'default = "alpha"\n'
    )
    (tmp_path / ".graph_cli.toml").write_text('format_version = 1\ngraphs = ["gamma"]\n')
    found, warnings = find_pointer(project / "sub" / "deep")
    assert found == PointerFile(
        project / ".graph_cli.toml", "example_project", ("alpha", "beta"), "alpha"
    )
    assert warnings == ()
    assert find_pointer(tmp_path)[0].graphs == ("gamma",)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('format_version = 1\ngraphs = ["a"]\ndata_root = "/x"\n', "config.forbidden_key"),
        ('format_version = 1\ngraphs = ["a"]\nroot = "/x"\n', "config.forbidden_key"),
        ('format_version = 1\ngraphs = ["a"]\npath_prefix = "x"\n', "config.forbidden_key"),
        ('format_version = 1\ngraphs = ["a"]\nendpoint = "https://x"\n', "config.forbidden_key"),
        ('format_version = 1\ngraphs = ["a"]\ntoken = "gct_x"\n', "config.forbidden_key"),
        ('format_version = 1\ngraphs = ["a"]\ntoken_file = "/x"\n', "config.forbidden_key"),
        ('graphs = ["a"]\n', "config.invalid"),
        ('format_version = 2\ngraphs = ["a"]\n', "format.unsupported_version"),
        ("format_version = 1\n", "config.invalid"),
        ("format_version = 1\ngraphs = []\n", "config.invalid"),
        ('format_version = 1\ngraphs = ["a"]\ndefault = "b"\n', "config.invalid"),
        ('format_version = 1\ngraphs = ["a"]\nproject = 3\n', "config.invalid"),
        ("not toml [", "config.invalid"),
    ],
)
def test_pointer_errors(text: str, code: str) -> None:
    with pytest.raises(GraphCliError) as info:
        parse_pointer(text.encode(), path=Path("/p/.graph_cli.toml"))
    assert info.value.code == code


def test_unknown_key_warns() -> None:
    _, warnings = parse_pointer(
        b'format_version = 1\ngraphs = ["a"]\ncolour = "x"\n', path=Path("/p")
    )
    assert [w.code for w in warnings] == ["config.unknown_key"]


def test_selection_order() -> None:
    point = pointer('format_version = 1\ngraphs = ["alpha", "beta"]\ndefault = "alpha"\n')
    assert select_graph(CATALOG, flag="beta", env_value="alpha", pointer=None) == BETA
    assert select_graph(CATALOG, flag=None, env_value="gamma", pointer=None) == GAMMA
    assert select_graph(CATALOG, flag=None, env_value=None, pointer=point) == ALPHA
    assert select_graph(CATALOG, flag=str(BETA.id), env_value=None, pointer=point) == BETA


def test_no_implicit_default() -> None:
    with pytest.raises(GraphCliError) as info:
        select_graph(Catalog(ALPHA), flag=None, env_value=None, pointer=None)
    assert info.value.code == "graph.not_selected"
    assert info.value.details["candidates"] == ["alpha"]


def test_pointer_allowlist() -> None:
    point = pointer('format_version = 1\ngraphs = ["alpha", "beta"]\n')
    with pytest.raises(GraphCliError) as info:
        select_graph(CATALOG, flag="gamma", env_value=None, pointer=point)
    assert info.value.code == "graph.not_in_project"
    with pytest.raises(GraphCliError) as info:
        select_graph(CATALOG, flag=None, env_value=None, pointer=point)
    assert info.value.details == {"candidates": ["alpha", "beta"], "hidden_count": 1}
    assert visible_graphs(CATALOG.list_graphs(), point) == ([ALPHA, BETA], 1)
