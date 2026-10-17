"""Named file roots (files slice 1, step 5): origins against roots, resolution per machine."""

import os
import unicodedata
from pathlib import Path

import pytest

from graph_cli.catalog.file_roots import FileRoots
from graph_cli.kernel.errors import GraphCliError


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for directory in ("notes/scans/2024", "notes/inbox", "archive", "outside"):
        (tmp_path / directory).mkdir(parents=True)
    return tmp_path.resolve()


def test_the_longest_root_wins(tree: Path) -> None:
    roots = FileRoots({"notes": tree / "notes", "scans": tree / "notes" / "scans"})
    file = tree / "notes" / "scans" / "2024" / "report 2024.pdf"
    assert roots.origin_of(file) == {"root": "scans", "path": "2024/report 2024.pdf"}
    assert roots.origin_of(tree / "notes" / "inbox" / "a.txt") == {
        "root": "notes",
        "path": "inbox/a.txt",
    }


def test_two_names_for_one_directory_take_the_first_name(tree: Path) -> None:
    roots = FileRoots({"zeta": tree / "notes", "alpha": tree / "notes"})
    assert roots.origin_of(tree / "notes" / "a.txt") == {"root": "alpha", "path": "a.txt"}


def test_paths_outside_every_root_have_no_origin(tree: Path) -> None:
    roots = FileRoots({"notes": tree / "notes"})
    assert roots.origin_of(tree / "outside" / "a.txt") is None
    assert roots.origin_of(tree / "notes") is None  # the root itself is not a file in it
    assert roots.origin_of(tree / "notes-2" / "a.txt") is None  # a prefix is not a parent
    assert FileRoots({}).origin_of(tree / "notes" / "a.txt") is None


def test_a_symlinked_root_matches_resolved_paths(tree: Path) -> None:
    os.symlink(tree / "notes", tree / "linked")
    roots = FileRoots({"notes": tree / "linked"})
    assert roots.origin_of((tree / "linked" / "inbox" / "a.txt").resolve()) == {
        "root": "notes",
        "path": "inbox/a.txt",
    }
    assert roots.resolve({"root": "notes", "path": "inbox/a.txt"}) == (
        tree / "linked" / "inbox" / "a.txt"
    )


def test_names_are_kept_as_given(tree: Path) -> None:
    name = unicodedata.normalize("NFD", "Prüfung.pdf")
    roots = FileRoots({"notes": tree / "notes"})
    origin = roots.origin_of(tree / "notes" / name)
    assert origin == {"root": "notes", "path": name}
    assert origin is not None and origin["path"] != unicodedata.normalize("NFC", name)


def test_resolution_needs_the_root_on_this_machine(tree: Path) -> None:
    roots = FileRoots({"notes": tree / "notes"})
    assert roots.resolve({"root": "notes", "path": "scans/2024/a.pdf"}) == (
        tree / "notes" / "scans" / "2024" / "a.pdf"
    )
    with pytest.raises(GraphCliError) as info:
        roots.resolve({"root": "archive", "path": "a.pdf"})
    assert (info.value.code, info.value.details) == ("file.root_unknown", {"root": "archive"})


def test_home_expansion() -> None:
    roots = FileRoots.from_config({"notes": "~/synced/notes", "srv": "/srv"}, {"HOME": "/home/u"})
    assert roots.resolve({"root": "notes", "path": "a"}) == Path("/home/u/synced/notes/a")
    assert roots.resolve({"root": "srv", "path": "a"}) == Path("/srv/a")
    assert roots.names == ("notes", "srv")
    with pytest.raises(GraphCliError) as info:
        FileRoots.from_config({"notes": "~/n"}, {})
    assert info.value.code == "config.invalid"
