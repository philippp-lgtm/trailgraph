"""The output envelope ``graph_cli.envelope/1`` and its renderings (contracts B9.1-B9.11).

JSON mode writes exactly one document plus ``\\n`` to stdout. Text mode
renders the same result: data on stdout, warnings and errors on stderr.
Every envelope key is always present (B9.2); unused ones are ``null`` or
``[]``. A program that prints the short envelope (``graph``) leaves those out
on stdout, and of ``graph`` what never changes within a graph (``short_envelope``);
the document itself, which the server sends and the access log records, stays whole.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph_cli.cli_kit.hints import hint_for
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord

ENVELOPE_SCHEMA: Final = "graph_cli.envelope/1"
# What the short envelope always prints, and of its ``graph`` besides a state other than active.
SHORT_KEYS: Final = ("schema", "ok", "command")
SHORT_GRAPH_KEYS: Final = ("name", "seq")
ACTIVE: Final = "active"


@dataclass(frozen=True, slots=True)
class Forwarded:
    """An answer another process rendered (the server, B14.10): its envelope and, when it
    rendered text, the text; ``None`` text is rendered here from the envelope."""

    document: Mapping[str, JsonValue]
    stdout: str | None = None
    stderr: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    """What a command returns; ``text`` overrides the generic text rendering of ``data``.

    A partial result sets ``error``: the envelope then fails with it and still
    carries ``data`` (B3.9: ``graph apply`` reports the documents it processed).
    """

    data: JsonValue = None
    graph: Mapping[str, JsonValue] | None = None
    warnings: tuple[WarningRecord, ...] = ()
    hints: tuple[Mapping[str, JsonValue], ...] = ()
    text: str | None = None
    page: Mapping[str, JsonValue] | None = field(default=None)
    error: GraphCliError | None = None
    forwarded: Forwarded | None = None  # the whole answer came from the server


def error_json(error: GraphCliError, *, program: str) -> dict[str, JsonValue]:
    """B9.8: ``{code, category, retryable, message, hint, details}``."""
    return {
        "code": error.code,
        "category": error.category.value,
        "retryable": error.retryable,
        "message": error.message,
        "hint": hint_for(error.code, error.details, program=program),
        "details": dict(error.details),
    }


def envelope(
    command: str | None,
    *,
    program: str,
    result: Result | None = None,
    error: GraphCliError | None = None,
    warnings: Sequence[WarningRecord] = (),
) -> dict[str, JsonValue]:
    all_warnings = [*warnings, *(result.warnings if result else ())]
    return {
        "schema": ENVELOPE_SCHEMA,
        "ok": error is None,
        "command": command,
        "graph": dict(result.graph) if result and result.graph is not None else None,
        "data": result.data if result and (error is None or error is result.error) else None,
        "page": dict(result.page) if result and result.page is not None else None,
        "hints": [dict(h) for h in result.hints] if result and error is None else [],
        "warnings": [w.to_json() for w in all_warnings],
        "error": error_json(error, program=program) if error is not None else None,
    }


def short_envelope(document: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The envelope as ``graph --json`` prints it: ``schema``, ``ok`` and ``command``;
    ``graph`` with ``name`` and ``seq`` (and ``state`` when it is not active); the other
    keys only when they hold something (not null, not ``[]``)."""
    shown: dict[str, JsonValue] = {}
    for key, value in document.items():
        if key in SHORT_KEYS:
            shown[key] = value
        elif key == "graph" and isinstance(value, Mapping):
            graph = {k: value[k] for k in SHORT_GRAPH_KEYS if k in value}
            if value.get("state", ACTIVE) != ACTIVE:
                graph["state"] = value["state"]
            shown[key] = graph
        elif value is not None and value != []:
            shown[key] = value
    return shown


def render_json(document: Mapping[str, JsonValue]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=False) + "\n"


def render_text(document: Mapping[str, JsonValue], *, text: str | None = None) -> tuple[str, str]:
    """``(stdout, stderr)`` of the text rendering."""
    out = ""
    if document["ok"] or document["data"] is not None:
        out = text if text is not None else _block(document["data"])
    warnings = document["warnings"]
    assert isinstance(warnings, list)
    err_lines = warning_lines(warnings)
    error = document["error"]
    if isinstance(error, Mapping):
        err_lines.append(f"error: {error['code']}: {error['message']}")
        details = error["details"]
        findings = details.get("findings") if isinstance(details, Mapping) else None
        if isinstance(findings, list):
            err_lines += _findings(findings)
        if error["hint"]:
            err_lines.append(f"hint: {error['hint']}")
    hints = document["hints"]
    if isinstance(hints, list):
        for hint in hints:
            if isinstance(hint, Mapping):
                command = hint.get("command")
                err_lines.append(f"hint: {hint.get('text')}" + (f": {command}" if command else ""))
    return out, "".join(line + "\n" for line in err_lines)


def warning_lines(warnings: Sequence[JsonValue]) -> list[str]:
    """The text rendering of envelope warnings, one line each (for stderr)."""
    lines = []
    for warning in warnings:
        assert isinstance(warning, Mapping)
        lines.append(f"warning: {warning['code']}: {warning['message']}")
    return lines


def _findings(findings: Sequence[JsonValue]) -> list[str]:
    lines = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        target = finding.get("target")
        where = ""
        if isinstance(target, Mapping):
            where = ", ".join(f"{k}={v}" for k, v in target.items())
        code, message = finding.get("code"), finding.get("message")
        lines.append(f"  {finding.get('severity')} {code} [{where}] {message}")
        options = _connections(finding.get("connecting"))
        if options:
            lines.append(f"    connect with: {'; '.join(options)}")
    return lines


def _connections(connecting: JsonValue) -> list[str]:
    """``a -rel-> b`` chains of a finding's ``connecting`` (direct relations, else paths)."""
    if not isinstance(connecting, Mapping):
        return []
    direct, paths = connecting.get("direct"), connecting.get("paths")
    chains = [[hop] for hop in direct] if isinstance(direct, list) and direct else paths
    options = []
    for chain in chains if isinstance(chains, list) else []:
        hops = [h for h in chain if isinstance(h, Mapping)] if isinstance(chain, list) else []
        if hops:
            text = str(hops[0].get("from"))
            for hop in hops:
                text += f" -{hop.get('relation')}-> {hop.get('to')}"
            options.append(text)
    return options


def _block(value: JsonValue, indent: int = 0) -> str:
    """A generic, YAML-like rendering for people; machines use ``--json``."""
    pad = "  " * indent
    if isinstance(value, Mapping):
        lines = []
        for key, item in value.items():
            if isinstance(item, Mapping | list) and item:
                lines.append(f"{pad}{key}:\n{_block(item, indent + 1)}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}\n")
        return "".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, Mapping) and item:
                body = _block(item, indent + 1)
                lines.append(f"{pad}- {body.lstrip()}")
            else:
                lines.append(f"{pad}- {_scalar(item)}\n")
        return "".join(lines)
    return "" if value is None else f"{pad}{_scalar(value)}\n"


def _scalar(value: JsonValue) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping | list):
        return "[]" if isinstance(value, list) else "{}"
    return str(value)
