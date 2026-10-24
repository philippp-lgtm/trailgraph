"""JSON request parameters to argv for the click parser (contracts B14.4)."""

from typing import Any

import pytest

from graph_cli.app_cli.commands import all_specs
from graph_cli.cli_kit.argv import request_args, to_argv
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

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
