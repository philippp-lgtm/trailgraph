"""``on_target_inactive``: a write that makes an edge's target inactive (retract, supersede).

The fixture ``data.referential.target_referenced`` covers a refused retract; these tests
cover the other policies, a supersede, change sets that fix the sources in the same go,
new edges to an inactive target and the batch check.
"""

from collections.abc import Mapping
from pathlib import Path

import pytest
from support.services import ServiceEnv

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.write import WriteRequest, WriteResult

REQUEST = RequestMeta.of("apply", {})

_MODULE = """\
format: graph_cli.ir/1
module: {{id: example_module, prefix: ex, version: 1.0.0, summary: Folders that nest.}}
classes:
  Folder:
    summary: A folder.
    attributes:
      name: {{summary: Name., type: string, required: true}}
relations:
  inside:
    summary: The folder lies in the other folder.
    domain: Folder
    range: Folder
    cardinality: {{out: {{max: 1}}}}
{policy}"""


def _model(policy: str | None) -> CompiledModel:
    line = "" if policy is None else f"    on_target_inactive: {policy}\n"
    text = _MODULE.format(policy=line)
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


class _Graph:
    """A memory graph with a root folder, two folders inside it and a spare folder."""

    def __init__(self, tmp_path: Path, policy: str | None) -> None:
        self.env = ServiceEnv("memory", "inventory", tmp_path, model_override=_model(policy))
        self.ids: dict[str, EntityId] = {}
        folder = {"op": "create", "type": "ex:Folder"}
        inside = [{"rel": "ex:inside", "target": "@root"}]
        self.apply(
            {
                "ops": [
                    {**folder, "local": "root", "attrs": {"name": "root"}},
                    {**folder, "local": "spare", "attrs": {"name": "spare"}},
                    {**folder, "local": "a", "attrs": {"name": "a"}, "edges": inside},
                    {**folder, "local": "b", "attrs": {"name": "b"}, "edges": inside},
                ]
            }
        )

    def apply(self, document: Mapping[str, JsonValue]) -> WriteResult:
        changeset = self.env.service.parse(dict(document))
        request = WriteRequest(
            self.env.actor, self.env.name, changeset, False, REQUEST, bindings=dict(self.ids)
        )
        result = self.env.service.apply(request)
        self.ids.update(result.bindings)
        return result

    def refused(self, document: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
        with pytest.raises(GraphCliError) as info:
            self.apply(document)
        assert info.value.code == "write.invalid"
        return list(info.value.details["findings"])  # type: ignore[arg-type]


_RETRACT_ROOT = {"ops": [{"op": "retract", "id": "@root"}], "reason": "withdrawn"}
_SUPERSEDE_ROOT = {
    "ops": [{"op": "supersede", "by": "@spare", "targets": ["@root"]}],
    "reason": "moved",
}


@pytest.mark.parametrize("document", [_RETRACT_ROOT, _SUPERSEDE_ROOT], ids=["retract", "supersede"])
def test_refuse_names_every_active_source(tmp_path: Path, document: dict[str, JsonValue]) -> None:
    graph = _Graph(tmp_path, "refuse")
    findings = graph.refused(document)
    assert [(f["code"], f["severity"], f["target"]["path"]) for f in findings] == [  # type: ignore[index,call-overload]
        ("data.referential.target_referenced", "error", "incoming.ex:inside")
    ]
    assert findings[0]["actual"] == {
        "count": 2,
        "sources": sorted([str(graph.ids["a"]), str(graph.ids["b"])]),
    }
    assert "2 active entities point here over ex:inside" in str(findings[0]["message"])


def test_warn_rides_along(tmp_path: Path) -> None:
    graph = _Graph(tmp_path, "warn")
    result = graph.apply(_SUPERSEDE_ROOT)
    assert [(f.code, f.severity.value) for f in result.findings] == [
        ("data.referential.target_referenced", "warning")
    ]


@pytest.mark.parametrize("policy", [None, "allow"])
def test_allow_is_todays_behaviour(tmp_path: Path, policy: str | None) -> None:
    graph = _Graph(tmp_path, policy)
    assert graph.apply(_RETRACT_ROOT).findings == ()


def test_relinking_in_the_same_change_set_passes(tmp_path: Path) -> None:
    graph = _Graph(tmp_path, "refuse")
    relink = [
        op
        for source in ("@a", "@b")
        for op in (
            {"op": "unlink", "id": source, "rel": "ex:inside", "target": "@root"},
            {"op": "link", "id": source, "rel": "ex:inside", "target": "@spare"},
        )
    ]
    result = graph.apply({**_SUPERSEDE_ROOT, "ops": [*relink, *_SUPERSEDE_ROOT["ops"]]})  # type: ignore[list-item]
    assert result.findings == ()


def test_retracting_the_sources_in_the_same_change_set_passes(tmp_path: Path) -> None:
    graph = _Graph(tmp_path, "refuse")
    ops = [{"op": "retract", "id": entity} for entity in ("@a", "@b", "@root")]
    assert graph.apply({"ops": ops, "reason": "withdrawn"}).findings == ()  # type: ignore[dict-item]


def test_one_source_left_is_still_refused(tmp_path: Path) -> None:
    graph = _Graph(tmp_path, "refuse")
    ops = [{"op": "retract", "id": "@a"}, {"op": "retract", "id": "@root"}]
    findings = graph.refused({"ops": ops, "reason": "withdrawn"})  # type: ignore[dict-item]
    assert [f["actual"] for f in findings] == [{"count": 1, "sources": [str(graph.ids["b"])]}]
    assert "1 active entity points here" in str(findings[0]["message"])


def test_a_new_edge_to_an_inactive_target_is_an_error_under_refuse(tmp_path: Path) -> None:
    graph = _Graph(tmp_path, "refuse")
    graph.apply({"ops": [{"op": "retract", "id": "@spare"}], "reason": "withdrawn"})
    findings = graph.refused(
        {"ops": [{"op": "link", "id": "@a", "rel": "ex:inside", "target": "@spare"}]}
    )
    codes = [(f["code"], f["severity"]) for f in findings]
    assert ("data.referential.target_not_active", "error") in codes


@pytest.mark.parametrize(("policy", "severity"), [("refuse", "error"), ("warn", "warning")])
def test_the_batch_check_follows_the_policy(tmp_path: Path, policy: str, severity: str) -> None:
    # Edges left dangling while the relation allowed it, judged after a release that tightens it.
    graph = _Graph(tmp_path, None)
    graph.apply(_RETRACT_ROOT)
    findings = check_graph(_model(policy), graph.env.snapshot())
    assert sorted((f.code, f.severity.value, f.target["id"]) for f in findings) == sorted(
        ("data.referential.target_not_active", severity, str(graph.ids[name]))
        for name in ("a", "b")
    )
