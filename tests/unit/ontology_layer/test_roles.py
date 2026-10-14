"""Role mappings: every core role exists in every fixture and means what it says."""

from collections.abc import Callable

import pytest
from support.fixtures import TOY_ONTOLOGIES, compiled_model
from support.roles import CORE_ROLES, FEATURE_ROLES, load_roles, resolve_role

from graph_cli.ontology_layer.model import (
    AttributeDef,
    ClassDef,
    RelationDef,
    RuleDef,
    TransitionDef,
)

CHECKS: dict[str, Callable[[object], bool]] = {
    "lifecycle_type": lambda c: (
        isinstance(c, ClassDef) and any(a.kind == "lifecycle" for a in c.attributes.values())
    ),
    "lifecycle_attribute": lambda a: isinstance(a, AttributeDef) and a.kind == "lifecycle",
    "guarded_transition": lambda t: isinstance(t, TransitionDef) and bool(t.guards),
    "guard_breaking_update": lambda a: isinstance(a, dict) and bool(a),
    "required_relation": lambda r: isinstance(r, RelationDef) and r.out.min >= 1,
    "acyclic_relation": lambda r: isinstance(r, RelationDef) and "acyclic" in r.characteristics,
    "unique_attribute": lambda a: isinstance(a, AttributeDef) and a.unique,
    "immutable_attribute": lambda a: isinstance(a, AttributeDef) and a.mutability == "immutable",
    "enum_attribute": lambda a: isinstance(a, AttributeDef) and a.kind == "enum",
    "supersede_target_type": lambda c: isinstance(c, ClassDef) and not c.abstract,
    "retractable_type": lambda c: isinstance(c, ClassDef) and not c.abstract,
    "write_rule": lambda r: isinstance(r, RuleDef) and r.when == "write",
    "batch_rule": lambda r: isinstance(r, RuleDef) and r.when == "batch",
    "abstract_type": lambda c: isinstance(c, ClassDef) and c.abstract,
    "subtype": lambda c: isinstance(c, ClassDef) and c.extends is not None,
    "qualified_relation": lambda r: isinstance(r, RelationDef) and bool(r.qualifiers),
    "transitive_relation": lambda r: (
        isinstance(r, RelationDef) and "transitive" in r.characteristics
    ),
    "claim_type": lambda c: isinstance(c, ClassDef),
    "named_datatype_attribute": lambda a: isinstance(a, AttributeDef) and a.kind == "datatype",
    "default_attribute": lambda a: isinstance(a, AttributeDef) and a.default is not None,
    "tree_relation": lambda r: (
        isinstance(r, RelationDef) and "acyclic" in r.characteristics and r.out.max == 1
    ),
}


def test_every_role_has_a_check() -> None:
    assert set(CHECKS) == set(CORE_ROLES) | set(FEATURE_ROLES)


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_core_roles_exist_in_every_fixture(ontology: str) -> None:
    roles = load_roles(ontology)
    assert set(CORE_ROLES) <= set(roles.values)
    assert set(roles.values) <= set(CORE_ROLES) | set(FEATURE_ROLES)


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_roles_resolve_and_mean_what_they_say(ontology: str) -> None:
    roles = load_roles(ontology)
    model = compiled_model(ontology, roles.version)
    for role, value in roles.values.items():
        definition = resolve_role(model, role, value)
        assert CHECKS[role](definition), f"{ontology}: role {role} -> {value}"


def test_feature_roles_skip_when_absent() -> None:
    roles = load_roles("inventory")
    with pytest.raises(pytest.skip.Exception):
        roles.get("claim_type")
