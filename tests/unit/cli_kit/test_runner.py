"""Envelope, exit codes, usage errors and hints (contracts B9.1-B9.13; plan M8)."""

import io
import json

import click
import pytest
from support.fixtures import GOLDENS, assert_golden

from graph_cli.cli_kit.envelope import Result, render_text
from graph_cli.cli_kit.hints import known_hint_codes
from graph_cli.cli_kit.runner import CommandState, Invocation, json_option, leaf, run
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, Severity

ENVELOPE_KEYS = ["schema", "ok", "command", "graph", "data", "page", "hints", "warnings", "error"]


@click.group()
def prog() -> None:
    """A toy program."""


@prog.group("thing")
def thing() -> None:
    """Things."""


@thing.command("show")
@click.argument("name")
@json_option
@leaf
def show(state: CommandState, name: str) -> Result:
    return Result(
        data={"name": name, "tags": ["a", "b"], "nested": {"n": 1, "none": None}},
        graph={"id": "g", "name": "example_graph"},
        warnings=(WarningRecord("config.unknown_key", "unknown key 'x'", {"key": "x"}),),
    )


@thing.command("fail")
@json_option
@leaf
def fail(state: CommandState) -> Result:
    finding = Finding(Severity.ERROR, "integrity.audit.seq_gap", {"stream": "s", "seq": 3}, "gap")
    raise GraphCliError(
        "graph.not_found", "no graph 'x'", details={"ref": "x", "findings": [finding.to_json()]}
    )


@thing.command("unrecorded")
@json_option
@leaf
def unrecorded(state: CommandState) -> Result:
    warning = WarningRecord("audit.refusal_not_recorded", "not audited", {"cause": "x"})
    raise GraphCliError("write.invalid", "invalid", warnings=(warning,))


@thing.command("noroot")
@json_option
@leaf
def noroot(state: CommandState) -> Result:
    raise GraphCliError("backend.unavailable", "no data root", details={"data_root": "/r"})


@thing.command("boom")
@json_option
@leaf
def boom(state: CommandState) -> Result:
    raise RuntimeError("secret detail")


def invoke(*argv: str, debug: bool = False, json_default: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(prog, argv, Invocation("prog", json_default, debug), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_success_envelope_golden() -> None:
    code, out, err = invoke("thing", "show", "x", "--json")
    assert (code, err) == (0, "")
    document = json.loads(out)
    assert list(document) == ENVELOPE_KEYS
    assert out.endswith("}\n") and out.count("\n") == 1
    assert (document["command"], document["ok"], document["error"]) == ("thing show", True, None)
    assert_golden(GOLDENS / "cli_kit" / "envelope_ok.json", out.encode())


def test_error_envelope_golden() -> None:
    code, out, err = invoke("thing", "fail", "--json")
    assert (code, err) == (1, "")
    document = json.loads(out)
    assert list(document) == ENVELOPE_KEYS
    assert document["data"] is None and document["ok"] is False
    assert list(document["error"]) == [
        "code",
        "category",
        "retryable",
        "message",
        "hint",
        "details",
    ]
    assert_golden(GOLDENS / "cli_kit" / "envelope_error.json", out.encode())


def test_json_default_from_the_invocation() -> None:
    code, out, _ = invoke("thing", "show", "y", json_default=True)
    assert code == 0 and json.loads(out)["data"]["name"] == "y"


def test_text_mode() -> None:
    code, out, err = invoke("thing", "show", "x")
    assert code == 0
    assert out == "name: x\ntags:\n  - a\n  - b\nnested:\n  n: 1\n  none: -\n"
    assert err == "warning: config.unknown_key: unknown key 'x'\n"
    code, out, err = invoke("thing", "fail")
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "error: graph.not_found: no graph 'x'",
        "  error integrity.audit.seq_gap [stream=s, seq=3] gap",
    ]


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (("thing", "nope", "--json"), "usage.unknown_command"),
        (("thing", "show", "--json"), "usage.missing_argument"),
        (("thing", "show", "x", "--bad", "--json"), "usage.invalid_argument"),
        (("--json", "thing", "show", "x", "y"), "usage.invalid_argument"),
    ],
)
def test_usage_errors_are_envelopes(argv: tuple[str, ...], code: str) -> None:
    exit_code, out, err = invoke(*argv)
    assert (exit_code, err) == (2, "")
    document = json.loads(out)
    assert document["error"]["code"] == code
    assert document["error"]["hint"] == "prog --help"


def test_usage_error_in_text_mode_shows_click_usage() -> None:
    code, out, err = invoke("thing", "show")
    assert (code, out) == (2, "")
    assert "Usage: prog thing show" in err and "Missing argument" in err


def test_internal_error_hides_the_trace_unless_debug() -> None:
    code, out, err = invoke("thing", "boom", "--json")
    assert (code, err) == (6, "")
    error = json.loads(out)["error"]
    assert error["code"] == "internal.error" and "secret detail" not in out
    code, out, err = invoke("thing", "boom", "--json", debug=True)
    assert code == 6 and "Traceback" in err and "secret detail" in err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = invoke("thing", "--help")  # click writes help to the process stdout
    assert code == 0 and "Usage: prog thing" in capsys.readouterr().out


def test_missing_root_hint() -> None:
    _, out, _ = invoke("thing", "noroot", "--json")
    assert json.loads(out)["error"]["hint"] == "graph-admin init"
    assert json.loads(out)["error"]["category"] == "unavailable"


def test_every_error_code_has_a_hint_entry() -> None:
    """B9.9: a template or an explicit None for every non-warning code."""
    errors = {c for c, spec in ERROR_CODES.items() if spec.category is not ErrorCategory.WARNING}
    assert errors <= known_hint_codes()


def test_render_text_of_empty_data() -> None:
    document = {"ok": True, "data": None, "warnings": [], "error": None, "hints": []}
    assert render_text(document) == ("", "")


def test_error_warnings_ride_along_in_the_envelope() -> None:
    code, out, _ = invoke("thing", "unrecorded", "--json")
    document = json.loads(out)
    assert code == 1 and document["error"]["code"] == "write.invalid"
    assert [w["code"] for w in document["warnings"]] == ["audit.refusal_not_recorded"]
