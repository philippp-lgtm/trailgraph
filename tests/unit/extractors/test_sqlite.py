"""The ``sqlite`` extractor: schema only, read only (file metadata M11, M14)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from graph_cli.extractors import sqlite as extractor
from graph_cli.extractors.model import ExtractionResult, RunInput


def make(tmp_path: Path, script: str, name: str = "db.sqlite") -> Path:
    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.executescript(script)
    connection.commit()
    connection.close()
    return path


def run(path: Path) -> ExtractionResult:
    return extractor.run(RunInput(path, "application/octet-stream"))


def test_counts_exclude_internal_objects_and_parts_are_tables_and_views(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE b (x, y, z);
        CREATE INDEX b_x ON b (x);
        CREATE VIEW v AS SELECT x FROM b;
        CREATE TRIGGER t AFTER INSERT ON b BEGIN SELECT 1; END;
        CREATE TABLE sequenced (id INTEGER PRIMARY KEY AUTOINCREMENT);
        INSERT INTO sequenced DEFAULT VALUES;
        """,
    )
    result = run(path)
    assert result.status == "ok"
    assert result.values == {
        "table_count": 3,  # sqlite_sequence is internal
        "view_count": 1,
        "index_count": 1,  # the autoindex of UNIQUE is internal
        "trigger_count": 1,
        "user_version": 0,
        "objects_truncated": False,
    }
    assert [(p.name, p.values["kind"]) for p in result.parts] == [
        ("a", "table"),
        ("b", "table"),
        ("sequenced", "table"),
        ("v", "view"),
    ]


def test_no_row_is_read_and_nothing_is_written(tmp_path: Path) -> None:
    path = make(tmp_path, "CREATE TABLE secrets (value TEXT); INSERT INTO secrets VALUES ('x');")
    before = sorted(p.name for p in tmp_path.iterdir())
    run(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no journal, wal or shm


def test_a_view_over_a_missing_table_is_a_part_without_columns(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        "CREATE TABLE t (a); CREATE VIEW broken AS SELECT * FROM t; PRAGMA writable_schema=ON;",
    )
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = 'CREATE VIEW broken AS SELECT * FROM gone' "
        "WHERE name = 'broken'"
    )
    connection.commit()
    connection.close()
    result = run(path)
    assert result.status == "ok"
    broken = next(p for p in result.parts if p.name == "broken")
    assert broken.values == {"kind": "view"}


def test_names_become_unique_single_lines(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        'CREATE TABLE "two\nlines" (a); CREATE TABLE "two lines" (a); CREATE TABLE "  " (a);',
    )
    assert sorted(p.name for p in run(path).parts) == ["two lines", "two lines (2)", "unnamed"]


def test_only_the_first_objects_become_parts(tmp_path: Path) -> None:
    script = "".join(f"CREATE TABLE t{n:04d} (a);" for n in range(extractor.MAX_OBJECTS + 5))
    result = run(make(tmp_path, script))
    assert len(result.parts) == extractor.MAX_OBJECTS
    assert result.values["table_count"] == extractor.MAX_OBJECTS + 5
    assert result.values["objects_truncated"] is True


def test_only_the_first_columns_are_listed(tmp_path: Path) -> None:
    columns = ", ".join(f"c{n}" for n in range(extractor.MAX_COLUMN_NAMES + 3))
    result = run(make(tmp_path, f"CREATE TABLE wide ({columns});"))
    part = result.parts[0]
    assert part.values["column_count"] == extractor.MAX_COLUMN_NAMES + 3
    assert len(part.values["column_names"]) == extractor.MAX_COLUMN_NAMES  # type: ignore[arg-type]


def test_user_version_is_the_applications(tmp_path: Path) -> None:
    result = run(make(tmp_path, "CREATE TABLE t (a); PRAGMA user_version = 7;"))
    assert result.values["user_version"] == 7


def test_a_file_that_is_not_a_database_is_failed(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite"
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    assert run(path) == ExtractionResult.failed("sqlite.unreadable")


def test_a_schema_with_a_function_is_not_trusted(tmp_path: Path) -> None:
    """``trusted_schema=OFF``: a view that calls an application function is not evaluated to
    name its columns, and no extension is loaded."""
    path = make(tmp_path, "CREATE TABLE t (a); CREATE VIEW v AS SELECT load_extension('x') AS e;")
    result = run(path)
    assert result.status == "ok"
    view = next(p for p in result.parts if p.name == "v")
    assert view.values == {"kind": "view"}


def test_hostile_names_become_bounded_printable_labels(tmp_path: Path) -> None:
    """A name is untrusted bytes and becomes the key of the part: printable, one line, within
    the byte limit the reconcile planner accepts."""
    names = [
        "a\x00b",  # NUL
        "zero\u200bwidth",  # a format character
        "\u00a0nbsp\u00a0",  # a non-breaking blank at both ends
        "x" * 600,
        "\U0001f600" * 150,
    ]
    script = "".join(
        'CREATE TABLE "' + n.replace("\x00", "") + '" (a);' if "\x00" not in n else ""
        for n in names
    )
    result = run(make(tmp_path, script))
    labels = [p.name for p in result.parts]
    assert "zero width" in labels and "nbsp" in labels
    for label in labels:
        assert label.isprintable() and label == label.strip() and "  " not in label
        assert len(label) <= extractor.MAX_NAME_LENGTH
        assert len(label.encode()) <= extractor.MAX_NAME_BYTES
    assert "x" * 200 in labels  # 600 characters cut to 200
    assert "\U0001f600" * 100 in labels  # 150 emoji cut to 400 bytes


def test_labels_that_collide_stay_distinct_in_schema_order(tmp_path: Path) -> None:
    script = 'CREATE TABLE "a\nb" (x); CREATE TABLE "a b" (x); CREATE TABLE "a  b" (x);'
    labels = [p.name for p in run(make(tmp_path, script)).parts]
    assert sorted(labels) == ["a b", "a b (2)", "a b (3)"]
    assert len(set(labels)) == 3


def test_a_collision_suffix_keeps_the_label_within_the_limits(tmp_path: Path) -> None:
    long = "\U0001f600" * 100  # 400 bytes: the suffix must fit by cutting the label
    script = f'CREATE TABLE "{long}" (x); CREATE TABLE "{long}\n" (x);'
    labels = [p.name for p in run(make(tmp_path, script)).parts]
    assert len(labels) == 2 and len(set(labels)) == 2
    assert all(len(label.encode()) <= extractor.MAX_NAME_BYTES for label in labels)
    assert any(label.endswith(" (2)") for label in labels)
