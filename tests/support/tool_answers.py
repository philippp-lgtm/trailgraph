"""Every function and view answer the suite sees is checked against the output schema of its tool
(tool manifest plan, step 6).

A pytest plugin, registered in ``conftest.py`` next to ``support.output_schemas``. That one checks
an envelope against the schema of its command, which is the same for every function (a row is any
value) and every view (a section is one of the kinds). The manifest of ``graph describe tools``
gives each function and each view its own schema, with the types of the columns and the sections
the view declares. Here the answer is checked against that one, closed like the schemas of the
envelopes (``support.output_schemas.closed``) and with the formats checked.

Two routes lead to an answer, and both are covered:

- in the test process (the server in-process, the services): ``ReadService.run`` and
  ``ReadService.view`` are wrapped, so ``run``, ``view`` and ``call`` over the server and the
  service tests pass the check;
- in a process (``graph run``, ``graph view`` and ``graph call`` of the CLI tests): ``run_cli``
  hands the printed envelope to ``check_process``, which opens the installation the process used,
  from its environment and working directory, and works out the schema of the tool for the same
  caller. It only reads (no access log, no audit event, no signal), so a test that counts those is
  not disturbed. An answer of a process in remote mode is the server's and is checked there.

The schema of a tool is built once per distinct catalog and caller: the key is the pinned ontology,
what the caller reads and the entries it sees. A failure is collected like those of the envelopes
(``FAILURES``) and fails the test that saw it; a test with a stub server marks itself
``unchecked_output``. An answer of the generated card (no authored view), of an action or of
``lookup`` has no schema of its own beyond its command's, and is left to that.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from graph_cli.app_cli import tool_manifest
from graph_cli.catalog.wiring import AppServices
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.read_view import ReadView
from graph_cli.kinetic.card import AUTHORED
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services import describe_tools
from graph_cli.services.describe_tools import FUNCTION, VIEW
from graph_cli.services.read import ReadService
from graph_cli.services.read.graph_read import GraphRead
from support.output_schemas import FAILURES, closed, message, where

type Key = tuple[Any, ...]
VALIDATORS: dict[Key, Any] = {}


def _validator(
    service: ReadService,
    entry: GraphEntry,
    actor: ActorContext,
    seen: ReadView,
    model: CompiledModel,
    kind: str,
    name: str,
) -> Any | None:
    """The validator of the tool ``kind`` ``name`` for the caller (``actor`` reading ``entry``
    through ``seen`` over ``model``), or ``None`` when the caller sees no such entry or the tool
    cannot be built (a stale entry)."""
    functions = service._visible_functions(entry, seen) or ()
    views = service._visible_views(actor, entry, seen) or ()
    key = (
        kind,
        name,
        entry.pin.artifact_hash,
        seen,
        tuple((e.name, e.hash) for e in functions),
        tuple((e.name, e.hash) for e in views),
    )
    if key not in VALIDATORS:
        tool = describe_tools.tool_name(kind, name)
        try:
            (head,) = tool_manifest.heads(model, functions, (), views, frozenset(), only=tool)
            schema = tool_manifest.tool_json(head, model)["output_schema"]
        except (GraphCliError, ValueError):
            VALIDATORS[key] = None
        else:
            VALIDATORS[key] = Draft202012Validator(
                closed(schema), format_checker=Draft202012Validator.FORMAT_CHECKER
            )
    return VALIDATORS[key]


def record(
    service: ReadService,
    entry: GraphEntry,
    actor: ActorContext,
    seen: ReadView,
    model: CompiledModel,
    kind: str,
    name: str,
    answer: JsonValue,
) -> None:
    found = _validator(service, entry, actor, seen, model, kind, name)
    if found is None:
        return
    problems = [f"{where(e)}: {message(e)}" for e in found.iter_errors(answer)]
    FAILURES.extend(f"{kind} tool {name} data: {problem}" for problem in problems[:5])


def _wrap_run(run: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(run)
    def checked(self: ReadService, view: GraphRead, name: str, *args: Any, **kwargs: Any) -> Any:
        found = run(self, view, name, *args, **kwargs)
        data, page = found[0], found[1]
        answer = {**data, "page": page.to_json()}
        record(self, view.entry, view.actor, view.read_view, view.model, FUNCTION, name, answer)
        return found

    return checked


def _wrap_view(view_of: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view_of)
    def checked(self: ReadService, view: GraphRead, *args: Any, **kwargs: Any) -> Any:
        found = view_of(self, view, *args, **kwargs)
        data = found[0]
        shown = data.get("view") if isinstance(data, dict) else None
        if isinstance(shown, dict) and shown.get("origin") == AUTHORED:
            name = str(shown["name"])
            record(self, view.entry, view.actor, view.read_view, view.model, VIEW, name, data)
        return found

    return checked


def check_process(program: str, stdout: str, env: Mapping[str, str], cwd: Path) -> None:
    """The answer of ``graph run``, ``graph view`` or ``graph call`` that ran as a process
    (``run_cli``, ``--json``): its tool's schema is worked out here, from the installation the
    process used (the environment and the working directory it had), without recording anything."""
    if program != "graph" or not stdout.startswith("{"):
        return
    try:
        document = json.loads(stdout)
    except ValueError:
        return
    if not isinstance(document, dict) or document.get("command") not in ("run", "view", "call"):
        return
    data, shown = document.get("data"), document.get("graph")
    if document.get("ok") is not True or not isinstance(data, dict) or not isinstance(shown, dict):
        return
    kind, name = _tool_of(data)
    if kind is None:
        return
    answer = {**data, "page": document.get("page")} if document["command"] == "run" else data
    services = AppServices(env=env, cwd=cwd)
    if services.remote_mode():  # the answer is the server's, checked where it is made
        return
    try:
        ctx = services.context([], "describe tools", None)
        entry = ctx.select(str(shown["name"]))
        reader = ctx.reader
        record(
            reader,
            entry,
            ctx.actor,
            reader.read_view(ctx.actor, entry),
            reader.visible_model(ctx.actor, entry),
            kind,
            name,
            answer,
        )
    except GraphCliError:
        return  # the process was refused or could not open the graph; its answer says so


def _tool_of(data: Mapping[str, Any]) -> tuple[str | None, str]:
    """The kind and the entry name of the tool a ``run``, ``view`` or ``call`` answer is from;
    ``None`` for what has no output schema of its own (an action, ``lookup``, a generated card)."""
    function, shown = data.get("function"), data.get("view")
    if isinstance(function, dict) and "columns" in data:
        return FUNCTION, str(function["name"])
    if isinstance(shown, dict) and shown.get("origin") == AUTHORED:
        return VIEW, str(shown["name"])
    return None, ""


def pytest_configure(config: pytest.Config) -> None:
    from graph_cli.services.read.queries import ReadQueries
    from graph_cli.services.read.views import ReadViews

    ReadQueries.run = _wrap_run(ReadQueries.run)  # type: ignore[method-assign]
    ReadViews.view = _wrap_view(ReadViews.view)  # type: ignore[method-assign]
