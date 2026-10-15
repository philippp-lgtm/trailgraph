"""Command declarations, the generated click tree and the inventory (contracts B11; plan A6)."""

import io
import json
from typing import Any

import click
import pytest

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.runner import CommandState, Invocation, run
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec, build_click


def echo(state: CommandState, **kwargs: Any) -> Result:
    return Result(data={k: list(v) if isinstance(v, tuple) else v for k, v in kwargs.items()})


SPECS = (
    CommandSpec(
        path=("thing", "make"),
        kind="write",
        surface="act",
        action="write.create",
        summary="Make a thing.",
        params=(
            ParamSpec("type", "term", "Type CURIE.", required=True, positional=True),
            ParamSpec("--set", "assignment", "attr=value.", repeatable=True),
            ParamSpec("--dry-run", "flag", "Validate only."),
            ParamSpec("--limit", "integer", "How many.", default=5),
            ParamSpec("--direction", "choice", "Which way.", choices=("in", "out"), default="out"),
        ),
        examples=(
            Example("prog thing make <type> --set <attr>=<value>", "Make one."),
            Example("prog thing make <type> --dry-run --json", "Preview."),
        ),
        errors=("write.invalid", "graph.not_selected"),
        handler=echo,
    ),
    CommandSpec(
        path=("whoami",),
        kind="meta",
        surface="meta",
        action=None,
        summary="Who am I.",
        params=(),
        examples=(Example("prog whoami", "Text."), Example("prog whoami --json", "JSON.")),
        errors=(),
        handler=echo,
    ),
)


def program() -> click.Group:
    return build_click(click.Group("prog"), SPECS)


def invoke(*argv: str) -> tuple[int, dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    code = run(program(), [*argv, "--json"], Invocation("prog"), out=out, err=err)
    return code, json.loads(out.getvalue())


def test_click_tree_equals_the_declarations() -> None:
    group = program()
    thing = group.commands["thing"]
    assert isinstance(thing, click.Group)
    make = thing.commands["make"]
    names = {p.name for p in make.params}
    assert names == {"type", "set", "dry_run", "limit", "direction", "json_mode"}
    assert set(group.commands) == {"thing", "whoami"}


def test_generated_commands_parse_and_call_the_handler() -> None:
    code, document = invoke("thing", "make", "ex:T", "--set", "a=1", "--set", "b=2", "--dry-run")
    assert code == 0 and document["command"] == "thing make"
    assert document["data"] == {
        "type": "ex:T",
        "set": ["a=1", "b=2"],
        "dry_run": True,
        "limit": 5,
        "direction": "out",
    }
    code, document = invoke("thing", "make")
    assert code == 2 and document["error"]["code"] == "usage.missing_argument"
    code, document = invoke("thing", "make", "ex:T", "--direction", "up")
    assert code == 2


def test_required_options_and_directories() -> None:
    """A required option is enforced (click treats an explicit default as given)."""
    spec = CommandSpec(
        path=("put",),
        kind="meta",
        surface="meta",
        action=None,
        summary="Put.",
        params=(ParamSpec("--out", "directory", "Where.", required=True),),
        examples=(Example("prog put --out <dir>", "Put."), Example("prog put --json", "x")),
        errors=(),
        handler=echo,
    )
    group = build_click(click.Group("prog"), (spec,))
    out = io.StringIO()
    code = run(group, ["put", "--json"], Invocation("prog"), out=out, err=io.StringIO())
    assert code == 2 and json.loads(out.getvalue())["error"]["code"] == "usage.missing_argument"
    out = io.StringIO()
    code = run(group, ["put", "--out", "x", "--json"], Invocation("prog"), out=out, err=out)
    assert code == 0 and json.loads(out.getvalue())["data"] == {"out": "x"}


def test_help_shows_examples() -> None:
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(SystemExit) as info:
        program().main(["thing", "make", "--help"], prog_name="prog")
    assert info.value.code == 0
    del out, err


def test_inventory_shape_and_determinism() -> None:
    inventory = build_inventory(
        tuple(reversed(SPECS)),
        program="prog",
        version="0.1.0",
        surface="app",
        global_options=(ParamSpec("--json", "flag", "Emit the JSON envelope."),),
    )
    assert inventory == build_inventory(
        SPECS,
        program="prog",
        version="0.1.0",
        surface="app",
        global_options=(ParamSpec("--json", "flag", "Emit the JSON envelope."),),
    )
    assert inventory["schema"] == "graph_cli.inventory/1"
    commands = inventory["commands"]
    assert isinstance(commands, list)
    assert [c["path"] for c in commands] == [["thing", "make"], ["whoami"]]  # type: ignore[index]
    make = commands[0]
    assert make["args"] == [  # type: ignore[index]
        {
            "name": "type",
            "type": "term",
            "required": True,
            "repeatable": False,
            "summary": "Type CURIE.",
        }
    ]
    assert [o["name"] for o in make["options"]] == ["--set", "--dry-run", "--limit", "--direction"]  # type: ignore[index]
    assert make["exit_codes"] == [0, 1, 2, 5, 6]  # type: ignore[index]
    assert "write.invalid" in make["errors"]  # type: ignore[index, operator]
    assert inventory["actions"] == ["write.create"]
    codes = {e["code"]: e for e in inventory["error_codes"]}  # type: ignore[index, union-attr]
    assert codes["graph.not_selected"] == {
        "code": "graph.not_selected",
        "category": "usage",
        "exit": 2,
    }


def test_unregistered_codes_are_refused() -> None:
    bad = CommandSpec(("x",), "meta", "meta", None, "x", (), (), ("no.such_code",), echo)
    with pytest.raises(ValueError):
        build_inventory([bad], program="p", version="0", surface="app")
