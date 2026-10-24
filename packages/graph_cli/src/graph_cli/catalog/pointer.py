"""Project pointer file ``.graph_cli.toml`` and graph selection (contracts B12.5, B12.6, B12.9).

The pointer narrows, it never widens: it names the graphs a project may use
(``graphs``), optionally a ``default`` and the ``project`` facet of the
actor. The first file found walking up from the working directory wins;
files are never merged. It must not move the data root, name a server or
carry a token, so any root, path, ``endpoint`` or token key is
``config.forbidden_key``.

Selection: ``--graph`` > ``GRAPH_CLI_GRAPH`` > pointer ``default`` >
``graph.not_selected`` with the visible graphs as candidates. There is no
implicit single-graph default. The policy decides visibility first: a graph
the actor cannot see is ``graph.not_found``, whatever the pointer says
(B6.15). With a pointer, a visible selected graph must be allowlisted
(``graph.not_in_project``); the pointer narrows, the policy grants.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry

POINTER_NAME: Final = ".graph_cli.toml"
POINTER_FORMAT: Final = 1
GRAPH_ENV: Final = "GRAPH_CLI_GRAPH"
_KEYS: Final = frozenset({"format_version", "project", "graphs", "default"})
_FORBIDDEN: Final = frozenset({"data_root", "root", "endpoint"})


class Visibility(Protocol):
    """What the policy lets the actor see (B6.15)."""

    def visible(self, entry: GraphEntry) -> bool: ...

    def refuse(self, entry: GraphEntry, ref: str) -> GraphCliError:
        """Record the refused selection; the error to raise."""
        ...


@dataclass(frozen=True, slots=True)
class PointerFile:
    path: Path
    project: str | None
    graphs: tuple[str, ...]  # names or ids
    default: str | None

    def allows(self, entry: GraphEntry) -> bool:
        return entry.name in self.graphs or str(entry.id) in self.graphs


def _invalid(path: Path, key: str, message: str) -> GraphCliError:
    where = f"{path}: {key}" if key else str(path)
    return GraphCliError(
        "config.invalid", f"{where} {message}", details={"file": str(path), "key": key}
    )


def parse_pointer(data: bytes, *, path: Path) -> tuple[PointerFile, tuple[WarningRecord, ...]]:
    try:
        document = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid(path, "", f"is not valid TOML: {exc}") from None
    for key in sorted(document):
        if key in _FORBIDDEN or key.startswith(("path", "token")):
            raise GraphCliError(
                "config.forbidden_key",
                f"{path}: {key!r} is not allowed in a pointer file; it cannot move the data"
                " root, name a server or carry a token",
                details={"file": str(path), "key": key},
            )
    version = document.get("format_version")
    if version is None:
        raise _invalid(path, "format_version", "is missing")
    if version != POINTER_FORMAT:
        raise GraphCliError(
            "format.unsupported_version",
            f"{path}: unsupported format_version {version!r}",
            details={"file": str(path), "expected": POINTER_FORMAT, "actual": str(version)},
        )
    warnings = tuple(
        WarningRecord(
            "config.unknown_key", f"{path}: unknown key {key!r}", {"file": str(path), "key": key}
        )
        for key in sorted(set(document) - _KEYS)
    )
    graphs = document.get("graphs")
    if (
        not isinstance(graphs, list)
        or not graphs
        or not all(isinstance(g, str) and g for g in graphs)
    ):
        raise _invalid(path, "graphs", "must be a non-empty array of graph names or ids")
    project = document.get("project")
    if project is not None and not isinstance(project, str):
        raise _invalid(path, "project", "must be a string")
    default = document.get("default")
    if default is not None and (not isinstance(default, str) or default not in graphs):
        raise _invalid(path, "default", "must be one of 'graphs'")
    return PointerFile(path, project, tuple(graphs), default), warnings


REQUEST_POINTER: Final = "<request>"


def request_pointer(value: object) -> PointerFile | None:
    """The pointer a server request carries (B14.4): ``{graphs, default?, file?}`` with the
    rules of the pointer file; ``file`` only names the client's pointer in messages. The
    project is part of the request's actor, not of its pointer."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"graphs", "default", "file"}:
        raise GraphCliError(
            "usage.invalid_input",
            "pointer must be {graphs, default?, file?}",
            details={"argument": "pointer"},
        )
    shown = value.get("file")
    path = Path(shown) if isinstance(shown, str) and shown else Path(REQUEST_POINTER)
    graphs, default = value.get("graphs"), value.get("default")
    if (
        not isinstance(graphs, list)
        or not graphs
        or not all(isinstance(g, str) and g for g in graphs)
    ):
        raise _invalid(path, "graphs", "must be a non-empty array of graph names or ids")
    if default is not None and (not isinstance(default, str) or default not in graphs):
        raise _invalid(path, "default", "must be one of 'graphs'")
    return PointerFile(path, None, tuple(graphs), default)


def find_pointer(cwd: Path) -> tuple[PointerFile | None, tuple[WarningRecord, ...]]:
    """The first pointer walking up from ``cwd`` to the filesystem root."""
    for directory in (cwd, *cwd.parents):
        candidate = directory / POINTER_NAME
        try:
            data = candidate.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _invalid(candidate, "", f"cannot be read: {exc.strerror or exc}") from exc
        return parse_pointer(data, path=candidate)
    return None, ()


def visible_graphs(
    entries: Sequence[GraphEntry], pointer: PointerFile | None
) -> tuple[list[GraphEntry], int]:
    """The graphs a project may see, and how many the pointer hides (B6.12)."""
    if pointer is None:
        return list(entries), 0
    shown = [e for e in entries if pointer.allows(e)]
    return shown, len(entries) - len(shown)


def select_graph(
    catalog: Catalog,
    *,
    flag: str | None,
    env_value: str | None,
    pointer: PointerFile | None,
    visibility: Visibility | None = None,
) -> GraphEntry:
    choice = flag or env_value or (pointer.default if pointer is not None else None)
    if not choice:
        entries = catalog.list_graphs()
        if visibility is not None:
            entries = [e for e in entries if visibility.visible(e)]
        shown, hidden = visible_graphs(entries, pointer)
        raise GraphCliError(
            "graph.not_selected",
            "no graph selected; select one by name or id",
            details={"candidates": [e.name for e in shown], "hidden_count": hidden},
        )
    entry = catalog.resolve(choice)
    if visibility is not None and not visibility.visible(entry):
        raise visibility.refuse(entry, choice)
    if pointer is not None and not pointer.allows(entry):
        raise GraphCliError(
            "graph.not_in_project",
            f"graph {entry.name} is not allowed by this project's pointer file",
            details={
                "graph": entry.name,
                "allowed": list(pointer.graphs),
                "file": str(pointer.path),
            },
        )
    return entry
