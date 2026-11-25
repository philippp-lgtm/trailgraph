"""JSON request parameters to argv for the click parser (contracts B14.4, B14.22 G5/G6)."""

from typing import Any

import pytest

from graph_cli.app_cli.commands import all_specs
from graph_cli.cli_kit.argv import api_args, api_name, inventory_args, request_args, to_argv
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import FloatLiteral
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs

SPECS = {" ".join(spec.path): spec for spec in all_specs()}


def no_files(param: ParamSpec, value: JsonValue) -> str:
    raise AssertionError("no file parameter expected")


def argv(command: str, args: dict[str, Any]) -> list[str]:
    spec: CommandSpec = SPECS[command]
    return to_argv(spec, args, no_files)


def test_options_first_then_positionals_after_the_end_marker() -> None:
    assert argv(
        "create",
        {
            "type": "inv:Item",
            "--set": ["name=Item one", "code=-1"],
            "--dry-run": True,
            "--reason": None,
            "--graph": "example_graph",
        },
    ) == [
        "create",
        "--set",
        "name=Item one",
        "--set",
        "code=-1",
        "--graph",
        "example_graph",
        "--dry-run",
        "--",
        "inv:Item",
    ]


def test_integers_flags_false_and_repeatable_positionals() -> None:
    assert argv("find", {"--limit": 5, "--include-inactive": False}) == ["find", "--limit", "5"]
    assert argv("supersede", {"by": "-abc", "targets": ["a", "b"]}) == [
        "supersede",
        "--",
        "-abc",
        "a",
        "b",
    ]
    assert argv("describe types", {}) == ["describe", "types"]


@pytest.mark.parametrize(
    ("command", "args", "argument"),
    [
        ("find", {"--nope": 1}, "--nope"),
        ("find", {"--limit": "5"}, "--limit"),
        ("find", {"--limit": True}, "--limit"),
        ("find", {"--include-inactive": "yes"}, "--include-inactive"),
        ("create", {"type": "inv:Item", "--set": "name=x"}, "--set"),
        ("create", {"type": 7}, "type"),
    ],
)
def test_refusals(command: str, args: dict[str, Any], argument: str) -> None:
    with pytest.raises(GraphCliError) as info:
        argv(command, args)
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["argument"] == argument


def test_file_parameters_are_resolved_by_the_caller() -> None:
    seen: list[JsonValue] = []

    def stage(param: ParamSpec, value: JsonValue) -> str:
        seen.append(value)
        return "/staged/input"

    upload = {"upload": "sha256:" + "0" * 64, "name": "changes.json"}
    assert to_argv(SPECS["apply"], {"file": upload}, stage) == ["apply", "--", "/staged/input"]
    assert seen == [upload]


def test_request_args_leave_out_what_was_not_given() -> None:
    values = {"type": "inv:Item", "set": ("a=1",), "set_json": (), "dry_run": False, "graph": None}
    assert request_args(SPECS["create"], values) == {"type": "inv:Item", "--set": ["a=1"]}
    round_trip = to_argv(SPECS["create"], request_args(SPECS["create"], values), no_files)
    assert round_trip == ["create", "--set", "a=1", "--", "inv:Item"]


def test_canonical_names_are_unique_per_command() -> None:
    assert api_name("--dry-run") == "dry_run"
    assert api_name("--set-json") == "set_json"
    assert api_name("type") == "type"
    for spec in (*all_specs(), *admin_specs()):
        names = [api_name(param.name) for param in spec.params]
        assert len(names) == len(set(names)), spec.path


def test_pair_parameters_declare_their_shape() -> None:
    shapes = {
        (param.name, param.pairs, param.pair_encoding)
        for spec in (*all_specs(), *admin_specs())
        for param in spec.params
        if param.pairs is not None
    }
    assert shapes == {
        ("--set", "map", "text"),
        ("--set-json", "map", "json"),
        ("--input", "map", "json"),
        ("--qualifier", "map", "text"),
        ("--qualifier-json", "map", "json"),
        ("--param", "map", "json"),
        ("--param", "multimap", "text"),
        ("--add", "multimap", "text"),
        ("--add-json", "multimap", "json"),
        ("--remove", "multimap", "text"),
        ("--remove-json", "multimap", "json"),
        ("--edge", "multimap", "text"),
    }
    for spec in (*all_specs(), *admin_specs()):
        for param in spec.params:
            if param.type in ("assignment", "json_assignment"):
                assert param.pairs is not None and param.repeatable, (spec.path, param.name)
            if param.pairs is not None:
                assert param.to_json()["pairs"] == param.pairs
                assert param.to_json()["pair_values"] == param.pair_encoding


def test_canonical_and_inventory_names_mean_the_same() -> None:
    by_inventory = {"type": "inv:Item", "--dry-run": True, "--graph": "example_graph"}
    by_canonical = {"type": "inv:Item", "dry_run": True, "graph": "example_graph"}
    mixed = {"type": "inv:Item", "dry_run": True, "--graph": "example_graph"}
    expected = argv("create", by_inventory)
    assert argv("create", by_canonical) == argv("create", mixed) == expected
    assert api_args(by_inventory) == by_canonical
    assert inventory_args(SPECS["create"], mixed) == by_inventory


def literal(text: str) -> FloatLiteral:
    """A number with a fraction as the request body parser keeps it."""
    return FloatLiteral(text)


@pytest.mark.parametrize(
    ("command", "structured", "cli"),
    [
        (
            "create",
            {
                "type": "inv:Item",
                "set": {"label": "Drill", "quantity": 2, "weight": literal("1.10")},
            },
            {"type": "inv:Item", "--set": ["label=Drill", "quantity=2", "weight=1.10"]},
        ),
        (
            "create",
            {"type": "inv:Item", "set": {"active": True, "note": "a=b"}},
            {"type": "inv:Item", "--set": ["active=true", "note=a=b"]},
        ),
        (
            "create",
            {"type": "inv:Item", "set_json": {"code": "12", "tags": ["a", 1], "n": None}},
            {"type": "inv:Item", "--set-json": ['code="12"', 'tags=["a",1]', "n=null"]},
        ),
        (
            "create",
            {"type": "inv:Item", "edge": {"located_in": ["L1"], "in_category": ["C1", "C2"]}},
            {
                "type": "inv:Item",
                "--edge": ["located_in=L1", "in_category=C1", "in_category=C2"],
            },
        ),
        (
            "update",
            {
                "ref": "ITEM-1",
                "add": {"tags": ["a", "b"]},
                "add_json": {"sizes": [{"b": 1, "a": literal("2.5")}]},
                "remove": {"tags": ["c"]},
                "remove_json": {"sizes": [3]},
            },
            {
                "ref": "ITEM-1",
                "--add": ["tags=a", "tags=b"],
                "--add-json": ['sizes={"a":2.5,"b":1}'],
                "--remove": ["tags=c"],
                "--remove-json": ["sizes=3"],
            },
        ),
        (
            "link",
            {
                "ref": "a",
                "rel": "inv:located_in",
                "target": "b",
                "qualifier": {"role": "main"},
                "qualifier_json": {"rank": 3},
            },
            {
                "ref": "a",
                "rel": "inv:located_in",
                "target": "b",
                "--qualifier": ["role=main"],
                "--qualifier-json": ["rank=3"],
            },
        ),
        (
            "find",
            {
                "where": [
                    {"field": "quantity", "op": "ge", "value": 2},
                    {"field": "label", "value": "Drill"},
                    {"field": "weight", "op": None, "value": literal("1.10")},
                    {"field": "condition", "op": "in", "value": ["new", "good"]},
                    {"field": "note", "op": "exists", "value": False},
                    "status=in_stock",
                ]
            },
            {
                "--where": [
                    "quantity:ge=2",
                    "label=Drill",
                    "weight=1.10",
                    "condition:in=new,good",
                    "note:exists=false",
                    "status=in_stock",
                ]
            },
        ),
        (
            "query",
            {"text": "RETURN $n", "param": {"n": 12, "s": "12", "t": "text"}},
            {"text": "RETURN $n", "--param": ["n=12", 's="12"', 't="text"']},
        ),
    ],
)
def test_structured_forms_give_the_argv_of_their_cli_form(
    command: str, structured: dict[str, Any], cli: dict[str, Any]
) -> None:
    assert argv(command, structured) == argv(command, cli)


@pytest.mark.parametrize(
    ("command", "args", "argument", "message"),
    [
        ("create", {"type": "t", "set": {"a": "x"}, "--set": ["a=x"]}, "--set", "name it once"),
        ("create", {"type": "t", "set": {"a": {"b": 1}}}, "set", "set_json"),
        ("create", {"type": "t", "set": {"a": None}}, "set", "set_json"),
        ("create", {"type": "t", "set": {"a=b": "x"}}, "set", "without ="),
        ("create", {"type": "t", "set": {"": "x"}}, "set", "without ="),
        ("create", {"type": "t", "edge": {"located_in": "L1"}}, "edge", "list of values"),
        ("create", {"type": "t", "edge": {"located_in": [["L1"]]}}, "edge", "number or boolean"),
        ("find", {"where": [{"field": "a:b", "value": "x"}]}, "where", "field"),
        ("find", {"where": [{"field": "a=b", "value": "x"}]}, "where", "field"),
        ("find", {"where": [{"field": "a", "op": "e=q", "value": "x"}]}, "where", "op"),
        (
            "find",
            {"where": [{"field": "a", "op": "in", "value": ["x,y"]}]},
            "where",
            "none containing",
        ),
        ("find", {"where": [{"field": "a", "op": "in", "value": []}]}, "where", "non-empty"),
        ("find", {"where": [{"field": "a", "op": "exists", "value": "true"}]}, "where", "exists"),
        ("find", {"where": [{"field": "a", "value": "x", "extra": 1}]}, "where", "unknown"),
        ("find", {"where": [{"field": "a"}]}, "where", "value"),
        ("find", {"where": {"field": "a", "value": "x"}}, "where", "list of values"),
        ("find", {"limit": literal("5.0")}, "limit", "integer"),
        ("create", {"type": "t", "reason": literal("1.5")}, "reason", "string"),
        ("find", {"nope": 1}, "nope", "takes"),
    ],
)
def test_structured_refusals(
    command: str, args: dict[str, Any], argument: str, message: str
) -> None:
    with pytest.raises(GraphCliError) as info:
        argv(command, args)
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["argument"] == argument
    assert message in info.value.message
