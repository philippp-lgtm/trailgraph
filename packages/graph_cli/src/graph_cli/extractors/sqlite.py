"""The ``sqlite`` extractor (file metadata M14): schema counts and the tables and views.

Opens the file read only and immutable (``mode=ro&immutable=1``), never runs a statement
that reads rows, loads no extension and distrusts the schema (``trusted_schema=OFF``): the
facts come from ``sqlite_master`` (``sqlite_schema`` is only an alias since SQLite 3.33) and
``PRAGMA table_info``. The record holds counts; the first ``MAX_OBJECTS`` tables and views by
name become its parts. A file SQLite cannot open is ``failed`` with ``sqlite.unreadable``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final
from urllib.parse import quote

from graph_cli.extractors.model import (
    ExtractionResult,
    Extractor,
    ModuleRef,
    PartResult,
    RunInput,
    accepts_types,
)
from graph_cli.extractors.text import MAX_BYTES, MAX_CHARACTERS
from graph_cli.extractors.text import label as one_line
from graph_cli.kernel.canonical import JsonValue

NAME: Final = "sqlite"
VERSION: Final = 1
MEDIA_TYPE: Final = "application/vnd.sqlite3"
PROBLEM_UNREADABLE: Final = "sqlite.unreadable"
MAX_OBJECTS: Final = 500
MAX_COLUMN_NAMES: Final = 200
MAX_NAME_LENGTH: Final = MAX_CHARACTERS
MAX_NAME_BYTES: Final = MAX_BYTES


def run(source: RunInput) -> ExtractionResult:
    try:
        with _open(source.path) as connection:
            return _read(connection)
    except sqlite3.Error:
        return ExtractionResult.failed(PROBLEM_UNREADABLE)


def _open(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.text_factory = lambda raw: raw.decode("utf-8", "replace")
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def _read(connection: sqlite3.Connection) -> ExtractionResult:
    objects = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY name, type"
    ).fetchall()
    counts = {"table": 0, "view": 0, "index": 0, "trigger": 0}
    shown: list[tuple[str, str]] = []
    for kind, name in objects:
        if kind in counts:
            counts[kind] += 1
        if kind in ("table", "view") and len(shown) < MAX_OBJECTS:
            shown.append((kind, name))
    (user_version,) = connection.execute("PRAGMA user_version").fetchone()
    parts: list[PartResult] = []
    used: set[str] = set()
    for kind, name in shown:
        part_name = _unique(one_line(name), used)
        values: dict[str, JsonValue] = {"kind": kind}
        columns = _columns(connection, name)
        if columns is not None:
            values["column_count"] = len(columns)
            values["column_names"] = [one_line(c) for c in columns[:MAX_COLUMN_NAMES]]
        parts.append(PartResult(part_name, values))
    return ExtractionResult(
        status="ok",
        values={
            "table_count": counts["table"],
            "view_count": counts["view"],
            "index_count": counts["index"],
            "trigger_count": counts["trigger"],
            "user_version": max(0, user_version),
            "objects_truncated": counts["table"] + counts["view"] > MAX_OBJECTS,
        },
        parts=tuple(parts),
    )


def _columns(connection: sqlite3.Connection, name: str) -> list[str] | None:
    """The column names of a table or view, ``None`` when SQLite cannot say (a view over a
    table that is gone)."""
    quoted = '"' + name.replace('"', '""') + '"'
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
    except sqlite3.Error:
        return None


def _unique(label: str, used: set[str]) -> str:
    """``label``, or ``label (2)``, ``label (3)`` ... when an earlier part took it: names that
    collapse to the same label (``a\nb`` and ``a b``) stay distinct parts, in schema order."""
    candidate, number = label, 1
    while candidate in used:
        number += 1
        suffix = f" ({number})"
        base = label
        while (
            len(base + suffix) > MAX_NAME_LENGTH or len((base + suffix).encode()) > MAX_NAME_BYTES
        ):
            base = base[:-1]
        candidate = base.rstrip() + suffix
    used.add(candidate)
    return candidate


SQLITE: Final = Extractor(
    name=NAME,
    version=VERSION,
    module=ModuleRef("files_sqlite", "1.0.0"),
    record_class="files_sqlite:SqliteMetadata",
    accepts=accepts_types(MEDIA_TYPE),
    accepted_types=(MEDIA_TYPE,),
    run=run,
    summary="Counts of tables, views, indexes and triggers; the tables and views as parts.",
    part_class="files_sqlite:SqliteObject",
    part_relation="files_sqlite:has_object",
)
