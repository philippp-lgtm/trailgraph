"""P through the write path: real processes running the WriteService concurrently.

Contracts B1.8 (aliases contiguous and unique), B3.11 (one of two writers
with the same ``expect_rev`` wins), B4.2, B13.8; P1 exit criterion
"concurrent processes lose no writes" at the service level.
"""

import re
from pathlib import Path

import pytest
from support.actions import publish_actions, valid_sources
from support.procs import release, result, spawn
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.ontology_layer.data_validator import check_graph

pytestmark = pytest.mark.slow

WORKERS = 6
PER_WORKER = 6


def test_concurrent_creates_lose_nothing(tmp_path: Path) -> None:
    svc = ServiceEnv("files", "inventory", tmp_path / "root")
    cls = load_roles("inventory").term("lifecycle_type")
    barrier = tmp_path / "go"
    procs = [
        spawn(
            "write",
            {
                "root": str(svc.root),
                "graph": svc.name,
                "barrier": str(barrier),
                "worker": n,
                "documents": [{"ops": svc.factory.create(cls)} for _ in range(PER_WORKER)],
            },
        )
        for n in range(WORKERS)
    ]
    release(barrier)
    outcomes = [r for proc in procs for r in result(proc, timeout=120)["results"]]
    assert all("seq" in r for r in outcomes), outcomes
    total = WORKERS * PER_WORKER
    assert sorted(r["seq"] for r in outcomes) == list(range(1, total + 1))
    assert svc.head_seq() == total
    by_prefix: dict[str, list[int]] = {}
    for alias in (a for r in outcomes for a in r["aliases"]):
        match = re.fullmatch(r"([A-Z0-9]+)-(\d+)", alias)
        assert match is not None
        by_prefix.setdefault(match[1], []).append(int(match[2]))
    assert by_prefix
    for numbers in by_prefix.values():
        assert sorted(numbers) == list(range(1, len(numbers) + 1))
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]
    assert check_graph(svc.model, svc.snapshot()) == []


def test_same_expect_rev_one_writer_wins(tmp_path: Path) -> None:
    svc = ServiceEnv("files", "inventory", tmp_path / "root")
    cls = load_roles("inventory").term("lifecycle_type")
    entity = str(next(e.id for e in svc.create(cls).entities if e.local == "x"))
    attr = next(n for n, v in svc.factory.attrs(cls).items() if isinstance(v, str))
    barrier = tmp_path / "go"
    procs = [
        spawn(
            "write",
            {
                "root": str(svc.root),
                "graph": svc.name,
                "barrier": str(barrier),
                "worker": n,
                "documents": [
                    {
                        "ops": [
                            {"op": "update", "id": entity, "set": {attr: f"w{n}"}, "expect_rev": 1}
                        ]
                    }
                ],
            },
        )
        for n in range(2)
    ]
    release(barrier)
    outcomes = [result(proc)["results"][0] for proc in procs]
    assert sorted(str(r.get("error")) for r in outcomes) == ["None", "conflict.version_mismatch"]
    assert svc.head_seq() == 2
    statuses = [e["result"]["status"] for e in svc.graph_events()]  # type: ignore[index]
    assert statuses.count("conflict") == 1


def test_two_concurrent_actions_on_one_vacant_position_commit_once(tmp_path: Path) -> None:
    """The precondition is checked under the lock: of two ``assign_position`` for the same
    vacant position with different persons, one commits and the other finds the position
    held when it takes the lock."""
    svc = ServiceEnv("files", "company", tmp_path / "root", example_version="1.1.0")
    publish_actions(svc, valid_sources())
    ids = {}
    for cls, local in (("org:Position", "pos"), ("ppl:Employee", "a"), ("ppl:Employee", "b")):
        created = svc.create(cls, local)
        ids[local] = str(next(e.id for e in created.entities if e.local == local))
    head = svc.head_seq()
    barrier = tmp_path / "go"
    procs = [
        spawn(
            "do",
            {
                "root": str(svc.root),
                "graph": svc.name,
                "action": "assign_position",
                "params": [f"person={ids[who]}", f"position={ids['pos']}", "since=2026-03-01"],
                "barrier": str(barrier),
                "worker": n,
            },
        )
        for n, who in enumerate(("a", "b"))
    ]
    release(barrier)
    outcomes = [result(proc)["results"][0] for proc in procs]
    assert sorted(str(r.get("error")) for r in outcomes) == [
        "None",
        "action.precondition_failed",
    ], outcomes
    assert svc.head_seq() == head + 1
    held = svc.snapshot().incoming(EntityId(parse_id(ids["pos"])), "org:holds")  # type: ignore[arg-type]
    assert len(list(held)) == 1
    refusals = [
        e
        for e in svc.graph_events()
        if e["result"]["status"] == "invalid"  # type: ignore[index]
    ]
    assert len(refusals) == 1
    assert refusals[0]["request"]["action"]["name"] == "assign_position"  # type: ignore[index]
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]
