"""Every function and view answer the suite sees is checked against the output schema of its tool
(tool manifest plan, step 6).

A pytest plugin, registered in ``conftest.py`` next to ``support.output_schemas``. That one checks
an envelope against the schema of its command, which is the same for every function (a row is any
value) and every view (a section is one of the kinds). The manifest of ``graph describe tools``
gives each function and each view its own schema, with the types of the columns and the sections
the view declares. Here the answer is checked against that one, closed like the schemas of the
envelopes (``support.output_schemas.closed``) and with the formats checked. The tool is built the
way ``call --expect-hash`` builds it (``tool_manifest.tool_head``), for the caller that got the
answer, so a caller with read limits or markings is judged by the schema it sees.

Two routes lead to an answer, and both are covered:

- in the test process (the server in-process, the services): ``ReadService.run`` and
  ``ReadService.view`` are wrapped, so ``run``, ``view`` and ``call`` over the server and the
  service tests pass the check;
- in a process (``graph run``, ``graph view`` and ``graph call`` of the CLI tests): ``run_cli``
  hands the printed envelope to ``check_process``, which opens the installation the process used,
  from its environment and working directory, as the caller the process was (its identity comes
  from that environment, not from the test process). It reads the catalog and the entries and
  records nothing itself: the graph is looked up, not selected, so even a graph the caller cannot
  see is no audited refusal.

What is not covered: an answer of a ``graph`` in remote mode (``graph`` with a real
``graph-server`` process, ``support.serve``, and ``Running.request``) is checked against the
schema of its command only; the server in-process (``ServerApp``) is covered by the first route.
The generated card (no authored view), an action and ``lookup`` have no schema of their own beyond
their command's.

The schema of a tool is built once per distinct catalog and caller: the key is the pinned ontology,
what the caller reads and the entries it sees with their rights. A tool that cannot be built for an
entry that has just answered is an error of the test, not a skip. A failure is collected like those
of the envelopes (``FAILURES``) and fails the test that saw it; a test with a stub server marks
itself ``unchecked_output``.
"""

from __future__ import annotations

import functools
import json
import os
import pwd
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from graph_cli import __version__
from graph_cli.app_cli.tool_manifest import classified, tool_head, tool_json
from graph_cli.catalog.config import Config
from graph_cli.catalog.identity import resolve_actor
from graph_cli.catalog.identity.facets import IdentityInputs
from graph_cli.catalog.pointer import PointerFile
from graph_cli.catalog.wiring import PROGRAM, AppServices
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kinetic.card import AUTHORED
from graph_cli.services.describe_tools import FUNCTION, VIEW
from graph_cli.services.read import ReadService
from graph_cli.services.read.graph_read import GraphRead
from support.output_schemas import FAILURES, closed, message, where

type Key = tuple[Any, ...]
VALIDATORS: dict[Key, Any] = {}


@dataclass
class _EnvServices(AppServices):
    """``AppServices`` whose actor comes from the environment the process had. ``AppServices``
    asks the identity of the process it runs in; here that is the test process, whose environment
    is not the one of the ``graph`` the test ran (an explore agent, a project)."""

    def _actor(
        self, config: Config, pointer: PointerFile | None
    ) -> tuple[ActorContext, tuple[WarningRecord, ...]]:
        uid = os.getuid()
        try:
            user: str | None = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = None
        return resolve_actor(
            IdentityInputs(env=dict(self.env), uid=uid, user_name=user, pid=os.getpid()),
            client=ClientInfo(PROGRAM, __version__, Entrypoint.APP),
            device_id=config.device_id,
            project=pointer.project if pointer is not None else None,
            providers=config.identity_providers,
        )


def _validator(
    reader: ReadService, actor: ActorContext, entry: GraphEntry, kind: str, name: str
) -> Any:
    """The validator of the tool ``kind`` ``name`` for ``actor`` on ``entry``, built the way
    ``call --expect-hash`` builds the tool (``tool_manifest.tool_head``): a tool that cannot be
    built for an entry that has just answered is a failure, not a skip."""
    functions = classified(reader, actor, entry, FUNCTION)
    views = classified(reader, actor, entry, VIEW)
    key = (
        kind,
        name,
        entry.pin.artifact_hash,
        reader.read_view(actor, entry),
        reader.catalog_scope(actor, entry) is not None,  # references are ids and aliases only
        tuple((c.entry.name, c.entry.hash, c.allowed) for c in functions),
        tuple((c.entry.name, c.entry.hash, c.allowed) for c in views),
    )
    if key not in VALIDATORS:
        head, model = tool_head(
            reader, actor, entry, kind, name, seen=functions if kind == FUNCTION else views
        )
        schema = tool_json(head, model)["output_schema"]
        VALIDATORS[key] = Draft202012Validator(
            closed(schema), format_checker=Draft202012Validator.FORMAT_CHECKER
        )
    return VALIDATORS[key]


def record(
    reader: ReadService,
    actor: ActorContext,
    entry: GraphEntry,
    kind: str,
    name: str,
    answer: JsonValue,
) -> None:
    """Check ``answer`` of the tool ``kind`` ``name`` and collect what is wrong with it."""
    found = _validator(reader, actor, entry, kind, name)
    problems = [f"{where(e)}: {message(e)}" for e in found.iter_errors(answer)]
    FAILURES.extend(f"{kind} tool {name} data: {problem}" for problem in problems[:5])


def _wrap_run(run: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(run)
    def checked(self: ReadService, view: GraphRead, name: str, *args: Any, **kwargs: Any) -> Any:
        found = run(self, view, name, *args, **kwargs)
        data, page = found[0], found[1]
        record(self, view.actor, view.entry, FUNCTION, name, {**data, "page": page.to_json()})
        return found

    return checked


def _wrap_view(view_of: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view_of)
    def checked(self: ReadService, view: GraphRead, *args: Any, **kwargs: Any) -> Any:
        found = view_of(self, view, *args, **kwargs)
        data = found[0]
        kind, name = _tool_of(data)
        if kind is not None:
            record(self, view.actor, view.entry, kind, name, data)
        return found

    return checked


def check_process(program: str, stdout: str, env: Mapping[str, str], cwd: Path) -> None:
    """The answer of ``graph run``, ``graph view`` or ``graph call`` that ran as a process
    (``run_cli``, ``--json``): its tool's schema is worked out here for the caller the process
    was (the environment and the working directory it had), from the installation it used."""
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
    services = _EnvServices(env=env, cwd=cwd)
    if services.remote_mode():  # the answer is the server's: see the module docstring
        return
    ctx = services.context([], "describe tools", None)
    # the entry is looked up in the catalog, not selected: a refusal would be recorded
    entry = ctx.catalog.resolve(str(shown["name"]))
    record(ctx.reader, ctx.actor, entry, kind, name, answer)


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
