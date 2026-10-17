"""A sum write rule through the write service: parents are rechecked when parts change (B7.9)."""

from pathlib import Path
from typing import Any

from support.fixtures import compiled_model
from support.services import ServiceEnv

RULE = "inv.parts_within_weight"


def item(local: str, weight: str, parent: str | None = None) -> dict[str, Any]:
    edges = [
        {"rel": "inv:located_in", "target": "@room"},
        {"rel": "inv:in_category", "target": "@cat"},
    ]
    if parent is not None:
        edges.append({"rel": "inv:part_of", "target": parent})
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"label": local, "weight": weight},
        "edges": edges,
    }


def rule_findings(result: Any) -> list[tuple[str | None, str, Any]]:
    return [(f.rule_id, f.severity.value, f.target.get("local")) for f in result.findings]


def test_parts_heavier_than_their_assembly_warn_until_fixed(tmp_path: Path) -> None:
    svc = ServiceEnv(
        "memory", "inventory", tmp_path, model_override=compiled_model("inventory", "2.1.0")
    )
    result = svc.apply(
        {
            "op": "create",
            "type": "inv:Location",
            "local": "room",
            "attrs": {"name": "R", "code": "R1"},
        },
        {"op": "create", "type": "inv:Category", "local": "cat", "attrs": {"name": "Tools"}},
        item("rack", "5.00"),
        item("left", "2.5", "@rack"),
        item("right", "2.75", "@rack"),
    )
    assert result.seq == 1  # a warning does not block the write
    assert rule_findings(result) == [(RULE, "warning", "rack")]
    (finding,) = result.findings
    assert isinstance(finding.actual, list)
    assert {"operand": {"attr": "weight"}, "value": "5.00"} in finding.actual

    # Only the part changes; the untouched assembly is rechecked as its neighbour.
    right = str(result.bindings["right"])
    fixed = svc.apply({"op": "update", "id": right, "set": {"weight": "2.50"}})
    assert (fixed.seq, fixed.findings) == (2, ())
    heavier = svc.apply({"op": "update", "id": right, "set": {"weight": "3"}})
    assert [(f.rule_id, f.target["id"]) for f in heavier.findings] == [
        (RULE, str(result.bindings["rack"]))
    ]
