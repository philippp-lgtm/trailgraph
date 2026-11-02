"""The model documents of a generated graph skill carry the actions the reader may run
(actions plan A6, A11): a skill block is a listing, so a reader who may not run an action does
not see it, and a reader who may run none gets the documents of a graph without actions."""

from pathlib import Path

import pytest
from support.actions import publish_actions, read_dir
from support.fixtures import FIXTURES
from support.services import ServiceEnv

from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.describe import SKILL_ACTION_KEYS

REQUEST = RequestMeta("skills", "sha256:" + "0" * 64)
SOURCES = FIXTURES / "actions" / "inventory" / "valid"


class Source:
    """A policy source with one fixed policy."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def deny(svc: ServiceEnv, action: str, type_curie: str) -> Source:
    rule: dict[str, JsonValue] = {
        "id": "deny_one",
        "effect": "deny",
        "action": [action],
        "resource": f"graph:{svc.entry.id}/type:{type_curie}",
    }
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], rule], "obligations": []}  # type: ignore[misc]
    return Source(policy_set(document))


@pytest.fixture
def svc(backend: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, "inventory", tmp_path / "root")
    publish_actions(env, read_dir(SOURCES))
    return env


def names(model: dict[str, JsonValue]) -> list[str]:
    catalog = model["actions"]
    assert isinstance(catalog, dict)
    return [e["name"] for e in catalog["actions"]]  # type: ignore[index,union-attr]


def test_a_reader_with_the_rights_gets_the_reduced_catalog(svc: ServiceEnv) -> None:
    entry = svc.catalog.resolve(svc.name)
    model, _ = svc.make_reader().skill_model(svc.actor, entry, REQUEST)
    assert names(model) == ["move_item", "retire_item"]
    catalog = model["actions"]
    assert isinstance(catalog, dict) and catalog["schema"] == "graph_cli.action_catalog/1"
    for action in catalog["actions"]:  # type: ignore[union-attr]
        assert set(action) <= set(SKILL_ACTION_KEYS)  # type: ignore[arg-type]


def test_a_reader_who_may_not_run_an_action_does_not_see_it(svc: ServiceEnv) -> None:
    """``move_item`` unlinks items; with that denied only ``retire_item`` is listed."""
    entry = svc.catalog.resolve(svc.name)
    narrow = svc.make_reader(deny(svc, "write.unlink", "inv:Item"))
    model, _ = narrow.skill_model(svc.actor, entry, REQUEST)
    assert names(model) == ["retire_item"]
    assert "move_item" not in str(model["actions"])


def test_a_reader_who_may_run_none_gets_a_graph_without_actions(svc: ServiceEnv) -> None:
    entry = svc.catalog.resolve(svc.name)
    rules: list[JsonValue] = [
        {
            "id": "no_writes",
            "effect": "deny",
            "action": ["write.unlink", "write.update", "write.transition", "write.link"],
            "resource": "graph:*",
        }
    ]
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], *rules], "obligations": []}  # type: ignore[misc]
    model, _ = svc.make_reader(Source(policy_set(document))).skill_model(svc.actor, entry, REQUEST)
    assert "actions" not in model


def test_a_graph_without_actions_has_no_actions_key(backend: str, tmp_path: Path) -> None:
    env = ServiceEnv(backend, "inventory", tmp_path / "root")
    model, _ = env.make_reader().skill_model(env.actor, env.catalog.resolve(env.name), REQUEST)
    assert "actions" not in model
