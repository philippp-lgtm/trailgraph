"""Every envelope the suite sees is checked against the declared schemas (B9.3, B11.10).

A pytest plugin, registered in ``conftest.py``. In-process runs (the server in tests,
``cli_kit.runner.run``) render through ``envelope``, which is wrapped for the whole
session; subprocess runs are checked by ``run_cli`` on their stdout (``graph`` prints the
short envelope), in-process server
calls by ``support.server.call`` on the body as sent, after the server's own additions
(``check_text``).
The envelope is checked against ``cli_kit.output_schemas``' definition, ``data`` against
the schema its command declares.

The schemas are checked closed: every object that does not say ``additionalProperties``
itself gets ``unevaluatedProperties: false``, so a key the code adds without declaring it
fails. A failure is collected and fails the test that saw it (in its setup or call),
also when a server thread rendered the envelope. A test whose envelopes come from a stub
server marks itself ``unchecked_output``.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Generator, Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from graph_cli.kernel.canonical import JsonValue

FAILURES: list[str] = []
MARKER = "unchecked_output"


def closed(schema: JsonValue) -> JsonValue:
    """A copy of ``schema`` in which every object lists every key it may have."""
    if isinstance(schema, list):
        return [closed(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    copy = {key: closed(value) for key, value in schema.items()}
    if (
        copy.get("type") == "object"
        and "additionalProperties" not in copy
        and "unevaluatedProperties" not in copy
    ):
        copy["unevaluatedProperties"] = False
    return copy


def validator(schema: Mapping[str, JsonValue], defs: Mapping[str, JsonValue]) -> Any:
    document = closed({**schema, "$defs": dict(defs)})
    assert isinstance(document, Mapping)
    return Draft202012Validator(document)


@functools.cache
def _programs() -> dict[str, tuple[Any, Any, dict[str, Any]]]:
    """Per program: the validators of the envelope and of the short envelope, and one per
    command that declares ``data``."""
    from graph_cli.app_cli.commands import all_specs as app_specs
    from graph_cli.app_cli.output_schemas import DEFS as APP_DEFS
    from graph_cli.cli_kit.output_schemas import DEFS as KIT_DEFS
    from graph_cli.cli_kit.output_schemas import ref
    from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs
    from graph_cli_admin.maintenance_cli.output_schemas import DEFS as ADMIN_DEFS

    programs: dict[str, tuple[Any, Any, dict[str, Any]]] = {}
    for program, specs, defs in (
        ("graph", app_specs(), APP_DEFS),
        ("graph-admin", admin_specs(), ADMIN_DEFS),
    ):
        commands = {
            " ".join(spec.path): validator(spec.output, defs)
            for spec in specs
            if spec.output is not None
        }
        programs[program] = (
            validator(ref("envelope"), KIT_DEFS),
            validator(ref("short_envelope"), KIT_DEFS),
            commands,
        )
    return programs


def check(program: str, document: JsonValue, *, short: bool = False) -> list[str]:
    """What is wrong with ``document``, an envelope of ``program`` (``short``: as printed)."""
    if program not in _programs():
        return []
    whole, shortened, commands = _programs()[program]
    envelope = shortened if short else whole
    problems = [f"envelope: {_where(e)}: {_message(e)}" for e in envelope.iter_errors(document)]
    if not isinstance(document, Mapping) or document.get("data") is None:
        return problems
    command = document.get("command")
    data = commands.get(command) if isinstance(command, str) else None
    if data is not None:
        problems += [
            f"{command} data: {_where(e)}: {_message(e)}"
            for e in data.iter_errors(document["data"])
        ]
    return problems


def _message(error: Any) -> str:
    """The message of ``error``; for a value that fits none of the alternatives of an ``anyOf``
    (a section of a view) the error of the alternative it comes closest to as well."""
    inner = best_match(error.context) if error.context else None
    if inner is None:
        return str(error.message)
    return f"{str(error.message)[:100]} (closest: {_where(inner)}: {str(inner.message)[:300]})"


def _where(error: Any) -> str:
    return "$" + "".join(f"[{p!r}]" if isinstance(p, int) else f".{p}" for p in error.path)


def record(program: str, document: JsonValue, *, short: bool = False) -> None:
    problems = check(program, document, short=short)[:5]
    FAILURES.extend(f"{program}: {problem}" for problem in problems)


def check_text(program: str, text: str, *, short: bool = False) -> None:
    """A subprocess's stdout or an HTTP answer's body, when it is one JSON envelope;
    ``short`` for ``graph``'s stdout."""
    if not text.startswith("{"):
        return
    try:
        document = json.loads(text)
    except ValueError:
        return
    if isinstance(document, dict) and document.get("schema") == "graph_cli.envelope/1":
        record(program, document, short=short)


def _wrap(render: Callable[..., dict[str, JsonValue]]) -> Callable[..., dict[str, JsonValue]]:
    @functools.wraps(render)
    def checked(command: str | None, *, program: str, **kwargs: Any) -> dict[str, JsonValue]:
        document = render(command, program=program, **kwargs)
        record(program, document)
        return document

    return checked


# The modules that render envelopes in the test process, each with its own binding.
RENDERERS = (
    "graph_cli.cli_kit.runner",
    "graph_cli_server.app",
    "graph_cli_server.admin_api",
    "graph_cli_server.enroll",
)


def pytest_configure(config: pytest.Config) -> None:
    import importlib

    config.addinivalue_line(
        "markers", f"{MARKER}(reason): envelopes come from a stub; do not check their schemas"
    )
    for name in RENDERERS:
        module = importlib.import_module(name)
        module.envelope = _wrap(module.envelope)  # type: ignore[attr-defined]


def pytest_runtest_setup(item: pytest.Item) -> None:
    FAILURES.clear()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Fail the test itself (setup and call), not its teardown."""
    result = yield
    found = list(FAILURES)
    FAILURES.clear()
    if found and item.get_closest_marker(MARKER) is None:
        pytest.fail("answers do not match their declared schemas:\n" + "\n".join(found[:20]))
    return result
