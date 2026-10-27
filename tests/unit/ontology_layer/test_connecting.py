"""``connecting``: the relations that connect two types (modules for agents, M6)."""

from dataclasses import replace
from typing import Any

from support.fixtures import compiled_model

from graph_cli.ontology_layer.connecting import MAX_PATHS, connecting
from graph_cli.ontology_layer.model import CompiledModel

MODEL = compiled_model("company", "1.1.0")


def hop(relation: str, source: str, target: str) -> dict[str, str]:
    return {"relation": relation, "from": source, "to": target}


def with_relations(**added: tuple[tuple[str, ...], tuple[str, ...]]) -> CompiledModel:
    """The company model plus relations, each a copy of ``org:part_of`` with new ends."""
    template = MODEL.relations["org:part_of"]
    relations: dict[str, Any] = dict(MODEL.relations)
    for name, (domain, range_) in added.items():
        relations[f"org:{name}"] = replace(template, domain=domain, range=range_)
    return replace(MODEL, relations=relations)


def test_a_person_reaches_a_position_directly() -> None:
    assert connecting(MODEL, "ppl:Person", "org:Position") == {
        "direct": [hop("org:holds", "ppl:Person", "org:Position")],
        "paths": [],
    }


def test_the_reverse_direction_is_listed_as_declared() -> None:
    assert connecting(MODEL, "org:Position", "ppl:Person")["direct"] == [
        hop("org:holds", "ppl:Person", "org:Position")
    ]


def test_a_person_reaches_a_unit_over_a_position() -> None:
    for target in ("org:OrgUnit", "org:Department", "org:Team"):
        assert connecting(MODEL, "ppl:Person", target) == {
            "direct": [],
            "paths": [
                [
                    hop("org:holds", "ppl:Person", "org:Position"),
                    hop("org:in_unit", "org:Position", target),
                ]
            ],
        }


def test_subclasses_follow_their_ancestors() -> None:
    assert connecting(MODEL, "org:Team", "org:Department")["direct"] == [
        hop("org:part_of", "org:Team", "org:Department")
    ]


def test_a_pair_with_nothing_gives_empty_lists() -> None:
    assert connecting(MODEL, "org:CostCenter", "ppl:Person") == {"direct": [], "paths": []}


def test_relations_with_an_open_end_are_never_listed() -> None:
    # core:source and core:derived_from accept any class on both ends.
    listed = {h["relation"] for h in connecting(MODEL, "ppl:Person", "ppl:Person")["direct"]}
    assert not any(name.startswith("core:") for name in listed)
    assert connecting(MODEL, "ppl:Person", "ppl:Person") == {"direct": [], "paths": []}


def test_several_direct_relations_are_ordered_by_length_then_name() -> None:
    team, department = ("org:Team",), ("org:Department",)
    model = with_relations(
        belongs_to_department=(team, department),
        in_dept=(team, department),
        a_link=(team, department),
    )
    names = [h["relation"] for h in connecting(model, "org:Team", "org:Department")["direct"]]
    assert names == [
        "org:a_link",
        "org:in_dept",
        "org:part_of",
        "org:belongs_to_department",
    ]


def test_paths_are_capped_ordered_and_only_when_nothing_is_direct() -> None:
    position, cost_center = ("org:Position",), ("org:CostCenter",)
    model = with_relations(budget=(position, cost_center), cost_line=(position, cost_center))
    model = replace(
        model,
        relations={
            **model.relations,
            **{
                f"org:r{i}": model.relations["org:budget"]
                for i in range(MAX_PATHS)  # more paths than the cap
            },
        },
    )
    paths = connecting(model, "ppl:Person", "org:CostCenter")["paths"]
    assert isinstance(paths, list) and len(paths) == MAX_PATHS
    assert paths[0][1]["relation"] == "org:r0"  # shortest CURIE first, then alphabetical
    assert all(path[0] == hop("org:holds", "ppl:Person", "org:Position") for path in paths)
    assert connecting(model, "org:Position", "org:CostCenter")["paths"] == []


def test_the_answer_is_deterministic() -> None:
    first = connecting(MODEL, "ppl:Person", "org:Department")
    reordered = replace(MODEL, relations=dict(reversed(MODEL.relations.items())))
    assert connecting(reordered, "ppl:Person", "org:Department") == first
