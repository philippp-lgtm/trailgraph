"""Role-based contract tests: roles map generic test needs to fixture terms.

Core tests never name a type. They ask for a role (``lifecycle_type``,
``required_relation``, ...) and each fixture maps roles to its terms in
``tests/fixtures/roles/<ontology>.toml``. Core roles must exist in every
fixture; feature roles may be absent (tests then skip).
"""

import tomllib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from graph_cli.ontology_layer.model import CompiledModel
from support.fixtures import FIXTURES

ROLES_DIR = FIXTURES / "roles"

# role -> kind of term it names
CORE_ROLES: Mapping[str, str] = {
    "lifecycle_type": "class",
    "lifecycle_attribute": "attribute",
    "guarded_transition": "transition",
    "guard_breaking_update": "attrs",
    "required_relation": "relation",
    "acyclic_relation": "relation",
    "unique_attribute": "attribute",
    "immutable_attribute": "attribute",
    "enum_attribute": "attribute",
    "supersede_target_type": "class",
    "retractable_type": "class",
    "write_rule": "rule",
    "batch_rule": "rule",
}
FEATURE_ROLES: Mapping[str, str] = {
    "abstract_type": "class",
    "subtype": "class",
    "qualified_relation": "relation",
    "transitive_relation": "relation",
    "claim_type": "class",
    "named_datatype_attribute": "attribute",
    "default_attribute": "attribute",
    "tree_relation": "relation",
    "label_type": "class",
    "label_attribute": "attribute",
    "alt_label_attribute": "attribute",
    "hidden_label_attribute": "attribute",
}


@dataclass(frozen=True)
class Roles:
    ontology: str
    version: str
    values: Mapping[str, object]

    def get(self, role: str) -> object:
        if role in CORE_ROLES:
            return self.values[role]
        if role not in self.values:
            pytest.skip(f"{self.ontology} has no role {role!r}")
        return self.values[role]

    def term(self, role: str) -> str:
        value = self.get(role)
        assert isinstance(value, str)
        return value


def load_roles(ontology: str) -> Roles:
    data = tomllib.loads((ROLES_DIR / f"{ontology}.toml").read_text())
    assert data.pop("format_version") == 1
    assert data.pop("ontology") == ontology
    version = data.pop("version")
    roles = {**data.pop("core"), **data.pop("feature", {})}
    assert not data, f"unknown keys in roles file: {sorted(data)}"
    return Roles(ontology, version, roles)


def resolve_role(model: CompiledModel, role: str, value: object) -> object:
    """The compiled definition a role points at; ``AssertionError`` if it does not exist."""
    kind = {**CORE_ROLES, **FEATURE_ROLES}[role]
    if kind == "class":
        assert isinstance(value, str)
        return model.classes[value]
    if kind == "relation":
        assert isinstance(value, str)
        return model.relations[value]
    if kind == "attribute":
        assert isinstance(value, str)
        cls, _, attr = value.rpartition(".")
        return model.classes[cls].attributes[attr]
    if kind == "attrs":
        assert isinstance(value, dict)
        return dict(value)
    if kind == "rule":
        (rule,) = [r for r in model.rules if r.id == value]
        return rule
    assert isinstance(value, dict)
    lifecycle = model.lifecycles[value["lifecycle"]]
    (transition,) = [
        t
        for t in lifecycle.transitions
        if (t.from_state, t.to_state) == (value["from"], value["to"])
    ]
    return transition


def lifecycle_path(model: CompiledModel, lifecycle: str, start: str, goal: str) -> list[str]:
    """The states after ``start`` on a shortest transition path to ``goal``."""
    transitions = model.lifecycles[lifecycle].transitions
    queue: deque[list[str]] = deque([[start]])
    while queue:
        path = queue.popleft()
        if path[-1] == goal:
            return path[1:]
        queue.extend(
            [*path, t.to_state]
            for t in transitions
            if t.from_state == path[-1] and t.to_state not in path
        )
    raise AssertionError(f"{goal} is not reachable from {start}")
