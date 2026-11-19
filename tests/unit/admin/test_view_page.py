"""The page of ``graph-admin view`` is assembled from its source files."""

from __future__ import annotations

import re

from graph_cli_admin.maintenance_cli.view_page import SCRIPT, STYLE, build_page, read_asset


def test_the_page_is_one_document_without_asset_references() -> None:
    html = build_page().decode()
    assert "<link" not in html and "<script src" not in html
    assert html.count("<style>") == 1 and html.count("<script>") == len(
        SCRIPT.findall(read_asset("view.html"))
    )
    assert "function draw(" in html and ".viz-root" in html


def test_every_script_file_is_listed_once_and_strict() -> None:
    markup = read_asset("view.html")
    listed = SCRIPT.findall(markup)
    assert len(listed) == len(set(listed)) > 1
    assert STYLE.findall(markup) == ["view.css"]
    for name in listed:
        assert read_asset(name).startswith('"use strict";'), name


def test_the_scripts_load_in_one_scope_in_listed_order() -> None:
    """A top-level ``const`` or ``let`` is used by later scripts only: no file reads one
    that a file after it declares while it loads (the listed order is the order of the
    top-level code)."""
    markup = read_asset("view.html")
    listed = SCRIPT.findall(markup)
    declared: dict[str, int] = {}
    for index, name in enumerate(listed):
        for found in re.finditer(r"^(?:const|let) (\w+)", read_asset(name), re.MULTILINE):
            assert found.group(1) not in declared, f"{found.group(1)} declared twice"
            declared[found.group(1)] = index
    assert declared["token"] == 0  # state.js comes first
