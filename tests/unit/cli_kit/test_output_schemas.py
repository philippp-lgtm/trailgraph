"""Declared answer schemas (contracts B9.3, B11.10): complete, valid, closed in tests.

That every answer of the suite fits its schema is checked by ``support.output_schemas``
on every envelope; this file checks the declarations themselves.
"""

from collections.abc import Iterator, Mapping
from typing import get_args

import pytest
from jsonschema import Draft202012Validator
from support.output_schemas import check, closed

from graph_cli.app_cli.commands import all_specs, files
from graph_cli.app_cli.commands.skills import SKILL_STATUS
from graph_cli.app_cli.output_schemas import DEFS
from graph_cli.cli_kit import output_schemas as kit
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.spec import CommandSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.services.files import CHECK_STATUSES
from graph_cli.skills.install import STATUSES, Action, Integrity
from graph_cli.skills.sources import KINDS
from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs
from graph_cli_admin.maintenance_cli.output_schemas import DEFS as ADMIN_DEFS
from graph_cli_admin.maintenance_cli.output_schemas import SKILL_KIND

SPECS = sorted(all_specs(), key=lambda s: s.path)
ADMIN_SPECS = sorted(admin_specs(), key=lambda s: s.path)
PROGRAMS: dict[str, tuple[list[CommandSpec], dict[str, kit.Schema]]] = {
    "graph": (SPECS, DEFS),
    "graph-admin": (ADMIN_SPECS, ADMIN_DEFS),
}
COMMANDS = [(program, spec) for program, (specs, _) in PROGRAMS.items() for spec in specs]


def _walk(value: JsonValue) -> Iterator[Mapping[str, JsonValue]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _refs(value: JsonValue) -> set[str]:
    return {str(node["$ref"]).removeprefix("#/$defs/") for node in _walk(value) if "$ref" in node}


@pytest.mark.parametrize(
    ("program", "spec"), COMMANDS, ids=[f"{p} {' '.join(s.path)}" for p, s in COMMANDS]
)
def test_every_command_declares_a_valid_schema_of_its_data(program: str, spec: CommandSpec) -> None:
    defs = PROGRAMS[program][1]
    assert spec.output is not None, "declare output= on the CommandSpec (B11.10)"
    Draft202012Validator.check_schema({**spec.output, "$defs": defs})
    assert _refs(spec.output) <= set(defs)


@pytest.mark.parametrize("program", PROGRAMS)
def test_definitions_resolve_and_are_used(program: str) -> None:
    specs, defs = PROGRAMS[program]
    for name, schema in defs.items():
        Draft202012Validator.check_schema(schema)
        assert _refs(schema) <= set(defs), name
    used = {"envelope", "short_envelope"}
    todo = [ref for spec in specs for ref in _refs(spec.output)] + ["envelope", "short_envelope"]
    while todo:
        name = todo.pop()
        new = _refs(defs[name]) - used
        used |= new | {name}
        todo += sorted(new)
    assert set(defs) == used


@pytest.mark.parametrize("program", PROGRAMS)
def test_objects_are_never_extended_with_all_of(program: str) -> None:
    """A closed branch of an ``allOf`` would refuse its siblings' keys."""
    specs, defs = PROGRAMS[program]
    schemas: list[JsonValue] = [*defs.values(), *(s.output for s in specs)]
    assert not [node for schema in schemas for node in _walk(schema) if "allOf" in node]


def test_shared_definitions_are_the_same_in_both_programs() -> None:
    """The envelope, the inventory and the program's update have one home each."""
    shared = set(DEFS) & set(ADMIN_DEFS)
    assert set(kit.DEFS) | {"program_step", "ontology_ref"} == shared
    assert all(DEFS[name] == ADMIN_DEFS[name] for name in shared)


def test_closing_adds_unevaluated_properties_to_objects_that_list_their_keys() -> None:
    schema = kit.obj(
        {"a": kit.obj({"b": kit.STRING}), "map": kit.obj(extra=kit.INTEGER)},
        {"c": kit.nullable(kit.obj())},
    )
    assert closed(schema) == {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
                "unevaluatedProperties": False,
            },
            "map": {"type": "object", "additionalProperties": {"type": "integer"}},
            "c": {"anyOf": [{"type": "object", "unevaluatedProperties": False}, {"type": "null"}]},
        },
        "required": ["a", "map"],
        "unevaluatedProperties": False,
    }
    assert schema["properties"]["a"] == kit.obj({"b": kit.STRING})  # type: ignore[index]


def test_the_checker_names_undeclared_keys_and_wrong_types() -> None:
    document: dict[str, JsonValue] = {
        "schema": "graph_cli.envelope/1",
        "ok": True,
        "command": "graphs",
        "graph": None,
        "data": {"items": [], "hidden_count": "0", "extra": 1},
        "page": None,
        "hints": [],
        "warnings": [],
        "error": None,
    }
    problems = check("graph", document)
    assert any("'extra' was unexpected" in p for p in problems)
    assert any("$.hidden_count" in p and "integer" in p for p in problems)
    assert check("graph", {**document, "data": {"items": [], "hidden_count": 0}}) == []
    assert check("graph", {**document, "surprise": 1}) != []


def test_a_definition_has_one_home() -> None:
    assert kit.merge_defs({"a": kit.STRING}, {"a": kit.STRING}) == {"a": kit.STRING}
    with pytest.raises(ValueError, match="'a' is defined twice"):
        kit.merge_defs({"a": kit.STRING}, {"a": kit.INTEGER})


def test_the_inventory_carries_the_schemas_only_on_request() -> None:
    plain = build_inventory(SPECS, program="graph", version="0", surface="app")
    assert "$defs" not in plain
    assert all("output" not in c for c in plain["commands"])  # type: ignore[union-attr]
    typed = build_inventory(SPECS, program="graph", version="0", surface="app", output_defs=DEFS)
    assert typed["$defs"] == dict(sorted(DEFS.items()))
    outputs = [c["output"] for c in typed["commands"]]  # type: ignore[index,union-attr]
    assert outputs == [s.output for s in SPECS]
    assert {k: v for k, v in typed.items() if k != "$defs"} | {"commands": None} == plain | {
        "commands": None
    }


def test_enumerations_follow_the_code() -> None:
    step = DEFS["skill_step"]["properties"]  # type: ignore[index]
    assert step["action"]["enum"] == list(get_args(Action))  # type: ignore[index]
    assert step["installed"]["anyOf"][0]["enum"] == list(get_args(Integrity))  # type: ignore[index]
    assert SKILL_STATUS["enum"] == list(STATUSES)
    assert files.CHECK_STATUSES == CHECK_STATUSES
    assert SKILL_KIND["enum"] == list(KINDS)  # type: ignore[index]
