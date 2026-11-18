"""The card reads a bounded number of revisions however big the neighbourhood is (OV9); get
--affordances is the control that shows what the card avoids."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from support.cards import CountingSnapshot
from support.services import ServiceEnv

from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read import GraphRead, ReadService

REQUEST = RequestMeta("view", "sha256:" + "0" * 64)
HUB = 150


def hub_env(tmp_path: Path) -> tuple[ServiceEnv, str]:
    env = ServiceEnv("memory", "notes_and_sources", tmp_path / "root")
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "nas:Topic", "local": "hub", "attrs": {"label": "Hub"}}
    ]
    ops += [
        {
            "op": "create",
            "type": "nas:Note",
            "local": f"n{i}",
            "attrs": {"title": f"Note {i:03d}", "body": "t", "kind": "observation"},
            "edges": [{"rel": "nas:about", "target": "@hub"}],
        }
        for i in range(HUB)
    ]
    result = env.apply(*ops)
    return env, str(result.bindings["hub"])


def counted(env: ServiceEnv, reader: ReadService) -> tuple[GraphRead, CountingSnapshot]:
    view = reader.open(env.actor, env.catalog.resolve(env.name), "read.get", REQUEST)
    counting = CountingSnapshot(view.snapshot)  # type: ignore[arg-type]
    return replace(view, snapshot=counting), counting  # type: ignore[type-var]


def test_the_card_of_a_hub_loads_at_most_one_hundred_neighbours(tmp_path: Path) -> None:
    env, hub = hub_env(tmp_path)
    reader = env.make_reader()
    view, counting = counted(env, reader)
    data, _ = reader.view(view, hub)
    group = next(s for s in data["sections"] if s["kind"] == "relations")["groups"][0]  # type: ignore[index, attr-defined]
    assert group["count"] == HUB and group["approx"] is True and group["sampled"] == 100
    assert len(counting.loaded) <= 105, len(counting.loaded)


def test_get_with_affordances_reads_every_source_which_the_card_does_not(tmp_path: Path) -> None:
    env, hub = hub_env(tmp_path)
    reader = env.make_reader()
    view, counting = counted(env, reader)
    reader.get(view, hub, affordances=True)
    assert len(counting.loaded) >= HUB  # the control: relation room counts every active source
