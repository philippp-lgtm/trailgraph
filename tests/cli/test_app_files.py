"""S: ``graph file add|path|check`` in an isolated home (files slice 1, step 6).

Covers file names with spaces and non-ASCII characters, adding twice, a
subclass, dry-run and refusals, origins against ``[roots]`` and every
``check`` status, the B12.4 rule that reads create nothing, the write scope
(``blobs/`` added) and the leak rule: file names, origin paths and content
never reach audit events, the op log or the state dir.
"""

import json
import os
import stat
import subprocess
import unicodedata
from pathlib import Path

import pytest
from harness import BIN_DIR, CliRunner
from support.admin import init
from support.app import ENV, app, create_graph
from support.files import synthetic_pdf, write_pdf, write_text
from support.trees import tree_hash

pytestmark = pytest.mark.slow


@pytest.fixture
def home(isolated_env: dict[str, str]) -> Path:
    return Path(isolated_env["HOME"])


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], home: Path) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    set_config(isolated_env, '[roots]\nnotes = "~/notes"\n')
    (home / "notes").mkdir()
    return root


def config_file(env: dict[str, str]) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"


def set_config(env: dict[str, str], extra: str) -> None:
    path = config_file(env)
    text = path.read_text().split("\n[roots]")[0].split("\n[files]")[0].rstrip("\n")
    path.write_text(text + "\n" + extra)


def graph_dir(root: Path) -> Path:
    (found,) = list((root / "graphs").iterdir())
    return found


def add(run_cli: CliRunner, *args: str, cwd: Path | None = None) -> dict:  # type: ignore[type-arg]
    outcome = app(run_cli, "file", "add", *args, env=ENV, cwd=cwd)
    assert outcome.code == 0, outcome.envelope
    return outcome.envelope


def test_add_then_path_and_check(run_cli: CliRunner, root: Path, home: Path) -> None:
    write_pdf(home / "notes" / "scans" / "report 2024.pdf")
    envelope = add(run_cli, "scans/report 2024.pdf", cwd=home / "notes")
    data = envelope["data"]
    assert data["existing"] is False
    assert data["entity"]["alias"] == "FILE-0001"
    assert data["file"]["media_type"] == "application/pdf"
    assert data["file"]["file_name"] == "report 2024.pdf"
    assert data["file"]["origin"] == {"root": "notes", "path": "scans/report 2024.pdf"}
    assert "warnings" not in envelope

    shown = app(run_cli, "file", "path", "FILE-0001", env=ENV)
    assert shown.code == 0, shown.envelope
    stored = Path(shown.data["path"])
    assert stored.read_bytes() == synthetic_pdf()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o444
    assert stored.is_relative_to(graph_dir(root) / "blobs")
    assert shown.data["origin"] == {
        "root": "notes",
        "path": "scans/report 2024.pdf",
        "resolved": str(home / "notes" / "scans" / "report 2024.pdf"),
    }
    origin = app(run_cli, "file", "path", "FILE-0001", "--origin", env=ENV)
    assert origin.data["path"] == str(home / "notes" / "scans" / "report 2024.pdf")

    checked = app(run_cli, "file", "check", env=ENV)
    assert checked.code == 0
    assert [i["status"] for i in checked.data["items"]] == ["same"]
    assert checked.data["counts"]["same"] == 1

    got = app(run_cli, "get", "FILE-0001", env=ENV)
    assert got.data["entity"]["attrs"]["title"] == "report 2024"


def test_non_ascii_names_and_adding_twice(run_cli: CliRunner, root: Path, home: Path) -> None:
    name = unicodedata.normalize("NFC", "Übersicht März.txt")
    write_text(home / "notes" / name)
    envelope = add(run_cli, str(home / "notes" / name))
    first = envelope["data"]
    assert first["file"]["origin"] == {"root": "notes", "path": name}
    assert first["file"]["media_type"] == "text/plain"
    seq = envelope["graph"]["seq"]  # after the file and the metadata record it got
    copy = write_text(home / "elsewhere" / "copy.txt")
    second = add(run_cli, str(copy))
    assert second["data"]["existing"] is True
    assert second["data"]["entity"]["id"] == first["entity"]["id"]
    assert [w["code"] for w in second["warnings"]] == ["file.already_present"]
    assert second["graph"]["seq"] == seq  # no second tx


def test_subclass_with_attributes_and_a_link(run_cli: CliRunner, root: Path, home: Path) -> None:
    path = write_pdf(home / "notes" / "scan.pdf", marker="scan")
    data = add(
        run_cli,
        str(path),
        "--type",
        "nas:Scan",
        "--title",
        "Quarterly report",
        "--set",
        "scanned_on=2026-03-01",
    )["data"]
    assert (data["entity"]["type"], data["entity"]["alias"]) == ("nas:Scan", "SCAN-0001")
    topic = app(run_cli, "create", "nas:Topic", "--set", "label=Physics", env=ENV)
    note = app(
        run_cli,
        "create",
        "nas:Note",
        "--set",
        "title=Task 3",
        "--set",
        "body=Read from the scan.",
        "--set",
        "kind=observation",
        "--edge",
        f"nas:about={topic.data['id']}",
        env=ENV,
    )
    assert note.code == 0, note.envelope
    linked = app(
        run_cli,
        "link",
        note.data["id"],
        "files:from_file",
        "SCAN-0001",
        "--qualifier-json",
        "page=3",
        env=ENV,
    )
    assert linked.code == 0, linked.envelope
    refused = app(
        run_cli,
        "link",
        note.data["id"],
        "files:from_file",
        "SCAN-0001",
        "--qualifier-json",
        "page=0",
        env=ENV,
    )
    assert refused.code == 1
    findings = refused.error["details"]["findings"]
    assert [f["code"] for f in findings] == ["data.structural.facet_violation"]


def test_dry_run_leaves_the_tree_byte_identical(run_cli: CliRunner, root: Path, home: Path) -> None:
    path = write_pdf(home / "notes" / "a.pdf")
    before = tree_hash(root)
    outcome = app(run_cli, "file", "add", str(path), "--dry-run", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["dry_run"] is True and outcome.data["entity"]["id"] is None
    assert tree_hash(root) == before


def test_refusals(run_cli: CliRunner, root: Path, home: Path, isolated_env: dict[str, str]) -> None:
    path = write_pdf(home / "notes" / "a.pdf")
    directory = app(run_cli, "file", "add", str(home / "notes"), env=ENV)
    assert (directory.code, directory.error["code"]) == (2, "file.unreadable")
    set_config(isolated_env, '[roots]\nnotes = "~/notes"\n[files]\nmax_bytes = 10\n')
    large = app(run_cli, "file", "add", str(path), env=ENV)
    assert (large.code, large.error["code"]) == (1, "file.too_large")
    assert not (graph_dir(root) / "blobs").exists() or not any(
        (graph_dir(root) / "blobs").iterdir()
    )
    create_graph(run_cli, "plain", "notes_and_sources")
    plain = app(run_cli, "file", "add", str(path), "--graph", "plain")
    assert (plain.code, plain.error["code"]) == (1, "file.module_missing")
    topic = app(run_cli, "create", "nas:Topic", "--set", "label=Physics", env=ENV)
    not_a_file = app(run_cli, "file", "path", topic.data["id"], env=ENV)
    assert (not_a_file.code, not_a_file.error["code"]) == (2, "usage.invalid_argument")


def test_check_reports_every_status(
    run_cli: CliRunner, root: Path, home: Path, isolated_env: dict[str, str]
) -> None:
    names = ("same", "changed", "missing", "unknown")
    for name in names:
        path = write_pdf(home / "notes" / f"{name}.pdf", marker=name)
        add(run_cli, str(path))
    set_config(isolated_env, '[roots]\nnotes = "~/notes"\nother = "~/other"\n')
    unknown_root = write_pdf(home / "other" / "x.pdf", marker="other")
    add(run_cli, str(unknown_root))
    loose = write_pdf(home / "loose.pdf", marker="loose")
    add(run_cli, str(loose), "--no-origin")
    (home / "notes" / "changed.pdf").write_bytes(synthetic_pdf("edited"))
    (home / "notes" / "missing.pdf").rename(home / "moved.pdf")
    set_config(isolated_env, '[roots]\nnotes = "~/notes"\n')
    checked = app(run_cli, "file", "check", env=ENV)
    statuses = sorted(i["status"] for i in checked.data["items"])
    assert statuses == ["changed", "missing", "root_unknown", "same", "same"]
    assert checked.data["counts"] == {
        "same": 2,
        "changed": 1,
        "missing": 1,
        "root_unknown": 1,
        "no_origin": 0,
    }
    one = app(run_cli, "file", "check", "FILE-0006", env=ENV)
    assert [i["status"] for i in one.data["items"]] == ["no_origin"]
    unknown = app(run_cli, "file", "path", "FILE-0005", "--origin", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "file.root_unknown")
    moved = app(run_cli, "file", "path", "FILE-0003", "--origin", env=ENV)
    assert (moved.code, moved.error["code"]) == (1, "file.origin_missing")


def test_reads_create_nothing(run_cli: CliRunner, root: Path, home: Path) -> None:
    """B12.4: file path and file check write nothing anywhere in the home."""
    add(run_cli, str(write_pdf(home / "notes" / "a.pdf")))
    before = tree_hash(home)
    for args in (
        ("file", "path", "FILE-0001"),
        ("file", "path", "FILE-0001", "--origin"),
        ("file", "check"),
        ("file", "check", "FILE-0001"),
        ("file", "add", "--help"),
    ):
        result = run_cli("graph", *args, env=ENV)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    assert tree_hash(home) == before


def _files(base: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_add_writes_only_where_the_app_may(
    run_cli: CliRunner, root: Path, home: Path, isolated_env: dict[str, str]
) -> None:
    """B12 invariant: graphs/<id>/{data,audit,blobs}, the installation audit, the state dir."""
    path = write_pdf(home / "notes" / "a.pdf")
    before = _files(home)
    add(run_cli, str(path))
    after = _files(home)
    changed = {name for name in after if before.get(name) != after[name]} | (before.keys() - after)
    graph = graph_dir(root).relative_to(home)
    allowed = (
        *(str(graph / part) for part in ("data", "audit", "blobs")),
        str(root.relative_to(home) / "audit"),
        str(Path(isolated_env["XDG_STATE_HOME"]).relative_to(home)),
    )
    assert changed and all(name.startswith(allowed) for name in changed), sorted(changed)
    assert any(name.startswith(str(graph / "blobs")) for name in changed)


def test_file_names_origins_and_content_never_reach_audit_or_logs(
    run_cli: CliRunner, root: Path, home: Path, isolated_env: dict[str, str]
) -> None:
    name_marker, dir_marker, content_marker = "NAMEMARK7f3a", "DIRMARK9c2e", "BODYMARK4b1d"
    path = write_pdf(home / "notes" / dir_marker / f"{name_marker}.pdf", marker=content_marker)
    add(run_cli, str(path), "--title", "Neutral title")
    app(run_cli, "file", "path", "FILE-0001", env=ENV)
    app(run_cli, "file", "check", env=ENV)
    bad = app(run_cli, "file", "add", str(path.with_name(f"{name_marker}-absent.pdf")), env=ENV)
    assert bad.code == 2
    graph = graph_dir(root)
    hidden = {
        "graph audit": _files(graph / "audit"),
        "installation audit": _files(root / "audit"),
        "state dir": _files(Path(isolated_env["XDG_STATE_HOME"])),
    }
    for where, files in hidden.items():
        assert files or where == "state dir", f"positive control: {where} has files"
        for file, data in files.items():
            for marker in (name_marker, dir_marker, content_marker):
                assert marker.encode() not in data, (where, file, marker)
    # positive controls: the markers are where they belong
    assert any(content_marker.encode() in d for d in _files(graph / "blobs").values())
    txlog = b"".join(_files(graph / "data" / "txlog").values())
    assert name_marker.encode() in txlog and dir_marker.encode() in txlog


def test_two_processes_adding_the_same_file_store_it_once(
    root: Path, home: Path, isolated_env: dict[str, str]
) -> None:
    path = write_pdf(home / "notes" / "same.pdf")
    env = {**isolated_env, **ENV}
    procs = [
        subprocess.Popen(
            [str(BIN_DIR / "graph"), "file", "add", str(path), "--json"],
            env=env,
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outputs
    datas = [json.loads(out)["data"] for out, _ in outputs]
    assert sorted(d["existing"] for d in datas) == [False, True]
    assert datas[0]["entity"]["id"] == datas[1]["entity"]["id"]
    blobs = [p for p in (graph_dir(root) / "blobs" / "sha256").rglob("*") if p.is_file()]
    assert len(blobs) == 1
    assert os.listdir(graph_dir(root) / "blobs") == ["sha256"]
