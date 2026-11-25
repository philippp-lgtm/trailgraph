"""A client of the tool manifest over the server API, the way an agent harness works.

It fetches the manifest once (``POST /v1/commands/describe/tools``) and from then on knows its
tools from that document alone: it never calls another ``describe`` command. Every input it sends
is checked with ``jsonschema`` against the ``input_schema`` of the tool first (2020-12, formats
checked), every call goes through ``POST /v1/commands/call`` with the hash of the tool it built
(``expect_hash``), and every answer is checked against the ``output_schema`` of the tool, as
published and closed (what the test plugin does for every answer of the suite). An error must be
one the tool lists, and the ``errors`` map of the manifest must say what to do about it.

``Ref`` names an entity by its name where an input takes a reference: the client resolves it with
the ``lookup`` tool, the way a harness without ids does.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from graph_cli.services.tool_errors import ON_ERROR_VALUES
from support.output_schemas import closed, message, where
from support.server import Reply, call

FORMATS = Draft202012Validator.FORMAT_CHECKER
CALL = "call"
DESCRIBE = "describe/tools"


@dataclass(frozen=True)
class Ref:
    """The entity named ``text``, for the reference input ``slot`` (``<tool>.<input>``)."""

    text: str
    slot: str


@dataclass(frozen=True)
class Answer:
    """The answer to one call, with what the client found out about it."""

    reply: Reply
    problems: tuple[str, ...]  # what is wrong with the input against the input schema
    on_error: str | None  # what the manifest says to do about the error, None on success

    @property
    def ok(self) -> bool:
        return self.reply.status == 200

    @property
    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = self.reply.json
        return body

    @property
    def data(self) -> Any:
        assert self.ok, self.body
        return self.body["data"]

    @property
    def code(self) -> str:
        return str(self.reply.error["code"])

    @property
    def details(self) -> dict[str, Any]:
        details: dict[str, Any] = self.reply.error["details"]
        return details


class ToolsClient:
    def __init__(self, server: Any, token: str, graph: str, manifest: Mapping[str, Any]) -> None:
        self.server, self.token, self.graph = server, token, graph
        self.manifest = manifest
        self.tools: dict[str, Any] = {t["name"]: t for t in manifest["tools"]}
        self.requests: list[str] = []
        self._keys = itertools.count(1)

    @classmethod
    def fetch(cls, server: Any, token: str, graph: str) -> ToolsClient:
        """The client of ``token`` with the manifest of its own view of the graph."""
        reply = _post(server, token, DESCRIBE, {"graph": graph})
        assert reply.status == 200, reply.json
        client = cls(server, token, graph, reply.json["data"])
        client.requests.append(DESCRIBE)
        return client

    def as_caller(self, token: str) -> ToolsClient:
        """The same tools, called with another token: a harness that built its tools from the
        manifest of one caller and runs for another."""
        return ToolsClient(self.server, token, self.graph, self.manifest)

    # ---- inputs -----------------------------------------------------------------------------

    def input_problems(self, tool: str, given: Mapping[str, Any]) -> list[str]:
        """What is wrong with ``given`` against the input schema of ``tool``."""
        found = Draft202012Validator(self.tools[tool]["input_schema"], format_checker=FORMATS)
        return [f"{where(e)}: {message(e)}" for e in found.iter_errors(given)]

    def resolve(self, ref: Ref) -> str:
        """The alias of the one entity the ``lookup`` tool finds for ``ref.text``."""
        answer = self.call("lookup", {"text": ref.text, "for": ref.slot})
        (candidate,) = [i for i in answer.data["items"] if i["text"] == ref.text]
        return str(candidate["alias"])

    def _filled(self, given: Mapping[str, Any]) -> dict[str, Any]:
        """``given`` with every ``Ref``, alone or in a list, looked up."""

        def known(value: Any) -> Any:
            if isinstance(value, Ref):
                return self.resolve(value)
            return [known(v) for v in value] if isinstance(value, list) else value

        return {name: known(value) for name, value in given.items()}

    # ---- calls ------------------------------------------------------------------------------

    def call(
        self,
        tool: str,
        given: Mapping[str, Any] | None = None,
        *,
        valid: bool = True,
        expect_hash: str | bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> Answer:
        """Call ``tool`` with ``given`` (a ``Ref`` is looked up first). The input must fit the
        input schema, or, with ``valid=False``, must not: the test sends what the schema forbids
        to see what the server answers. ``expect_hash`` is the hash of the tool in the manifest
        (``False``: none)."""
        built = self._filled(given or {})
        problems = tuple(self.input_problems(tool, built))
        assert bool(problems) is not valid, (tool, built, problems)
        listed = self.tools[tool]
        args: dict[str, Any] = {"graph": self.graph, "tool": tool, "input": built}
        if expect_hash is True:
            args["expect_hash"] = listed["hash"]
        elif expect_hash:
            args["expect_hash"] = expect_hash
        if _writes(tool, built):  # only an action that is not a dry run takes a key
            args["idempotency_key"] = f"client-{next(self._keys)}"
        return self._send(tool, args, problems, headers)

    def _send(
        self,
        tool: str,
        args: dict[str, Any],
        problems: tuple[str, ...],
        headers: Mapping[str, str] | None,
    ) -> Answer:
        self.requests.append(CALL)
        reply = _post(self.server, self.token, CALL, args, headers)
        if reply.status == 200:
            self.check_output(tool, reply.json["data"])
            return Answer(reply, problems, None)
        return Answer(reply, problems, self.check_error(tool, reply))

    # ---- answers ----------------------------------------------------------------------------

    def check_output(self, tool: str, data: Any) -> None:
        """``data`` fits the output schema of ``tool`` as published and in the closed form, in
        which a key the schema does not list is an error as well."""
        schema = self.tools[tool]["output_schema"]
        for label, form in (("published", schema), ("closed", closed(schema))):
            found = Draft202012Validator(form, format_checker=FORMATS)
            errors = [f"{where(e)}: {message(e)}" for e in found.iter_errors(data)]
            assert not errors, f"{tool}: the answer does not fit the {label} schema: {errors[:5]}"

    def check_error(self, tool: str, reply: Reply) -> str:
        """The error is one the tool lists and the manifest says what to do about it; returns
        that (``on_error``)."""
        error = reply.error
        code = error["code"]
        listed = self.tools[tool]["errors"] if tool in self.tools else []
        assert code in listed, f"{tool} does not list {code}: {error['message']}"
        info = self.manifest["errors"][code]
        assert info["on_error"] in ON_ERROR_VALUES, (code, info)
        assert (info["category"], info["retryable"]) == (error["category"], error["retryable"])
        return str(info["on_error"])

    # ---- paging -----------------------------------------------------------------------------

    def rows(self, tool: str, given: Mapping[str, Any], *, limit: int) -> list[Any]:
        """Every row of a function, ``limit`` at a time: each next page starts at ``next_offset``
        of the one before."""
        items: list[Any] = []
        offset = 0
        while True:
            page = self.call(tool, {**given, "_limit": limit, "_offset": offset}).data
            items += page["items"]
            if not page["page"]["truncated"]:
                return items
            offset = page["page"]["next_offset"]

    def pages(self, tool: str, given: Mapping[str, Any], *, page_bytes: int) -> list[Any]:
        """The pages of a set view: the ``next`` of a page is the ``_cursor`` of the next call,
        while ``more`` is true."""
        found: list[Any] = []
        cursor: str | None = None
        while True:
            asked = {**given, "_page_bytes": page_bytes, "_cursor": cursor}
            data = self.call(tool, asked).data
            found.append(data)
            if not data.get("more"):
                return found
            cursor = data["next"]


def _post(
    server: Any,
    token: str,
    path: str,
    args: dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> Reply:
    return call(
        server,
        "POST",
        f"/v1/commands/{path}",
        token=token,
        body={"args": args},
        headers=dict(headers or {}),
    )


def _writes(tool: str, given: Mapping[str, Any]) -> bool:
    return tool.startswith("do_") and given.get("_dry_run") is not True


def sections(data: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The sections of a view answer."""
    found: Sequence[Mapping[str, Any]] = data["sections"]
    return found
