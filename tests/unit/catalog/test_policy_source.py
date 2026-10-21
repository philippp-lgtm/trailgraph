"""FilePolicySource: installed files, built-in fallback, fail closed (contracts B6.8, B6.14)."""

import json
from pathlib import Path

import pytest

from graph_cli.access_control.policy import BUILTIN_POLICY, BUILTIN_POLICY_SET, policy_set
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.kernel.canonical import JsonValue, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id

GRAPH = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
INSTALLATION: dict[str, JsonValue] = {
    "schema": "graph_cli.policy/1",
    "id": "installation",
    "layer": "installation",
    "projects": [],
    "rules": [
        {
            "id": "grant.1",
            "effect": "allow",
            "principal": {"entrypoint": "app"},
            "action": ["read.get"],
            "resource": ["graph:*"],
        }
    ],
}
LAYER: dict[str, JsonValue] = {
    "schema": "graph_cli.policy/1",
    "id": "graph",
    "layer": "graph",
    "graph": str(GRAPH),
    "types": {},
    "rules": [],
}


def install(root: Path, document: JsonValue, *, graph: GraphId | None = None) -> Path:
    path = (
        root / "graphs" / str(graph) / "policy.json"
        if graph is not None
        else root / "policy" / "installation.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pretty_bytes(document))
    return path


def test_nothing_installed_is_the_builtin_policy(tmp_path: Path) -> None:
    source = FilePolicySource(tmp_path / "missing_root")
    assert source.current(None) == BUILTIN_POLICY_SET
    assert source.current(GRAPH) == BUILTIN_POLICY_SET


def test_installed_layers(tmp_path: Path) -> None:
    install(tmp_path, INSTALLATION)
    source = FilePolicySource(tmp_path)
    assert source.current(None) == policy_set(INSTALLATION)
    install(tmp_path, LAYER, graph=GRAPH)
    fresh = FilePolicySource(tmp_path)
    assert fresh.current(GRAPH) == policy_set(INSTALLATION, LAYER)
    assert fresh.current(None) == policy_set(INSTALLATION)


def test_files_are_read_once_per_invocation(tmp_path: Path) -> None:
    path = install(tmp_path, INSTALLATION)
    source = FilePolicySource(tmp_path)
    first = source.current(None)
    path.unlink()
    assert source.current(None) == first


@pytest.mark.parametrize(
    ("document", "graph"),
    [
        (b"{not json", None),
        (b"[]", None),
        ({**INSTALLATION, "rules": "all"}, None),
        (LAYER, None),  # a graph layer in the installation file
        ({**LAYER, "graph": "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"}, GRAPH),  # another graph's
        (INSTALLATION, GRAPH),
    ],
)
def test_invalid_files_fail_closed(
    tmp_path: Path, document: JsonValue | bytes, graph: GraphId | None
) -> None:
    path = (
        tmp_path / "graphs" / str(graph) / "policy.json"
        if graph is not None
        else tmp_path / "policy" / "installation.json"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(document if isinstance(document, bytes) else pretty_bytes(document))
    with pytest.raises(GraphCliError) as info:
        FilePolicySource(tmp_path).current(GRAPH)
    assert info.value.code == "policy.invalid"
    assert info.value.details["file"] == str(path.relative_to(tmp_path))
    assert str(tmp_path) not in info.value.message


def test_fallback_leaves_the_invalid_layer_out(tmp_path: Path) -> None:
    install(tmp_path, {**INSTALLATION, "rules": "all"})
    install(tmp_path, {**LAYER, "rules": "all"}, graph=GRAPH)
    seen: list[GraphCliError] = []
    source = FilePolicySource(tmp_path, fallback=seen.append)
    assert source.current(GRAPH) == BUILTIN_POLICY_SET
    assert source.installation() is BUILTIN_POLICY
    assert [e.details["file"] for e in seen] == [
        "policy/installation.json",
        f"graphs/{GRAPH}/policy.json",
    ]
    assert json.loads(pretty_bytes(dict(BUILTIN_POLICY))) == BUILTIN_POLICY
