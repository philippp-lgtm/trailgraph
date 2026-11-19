"""The page of ``graph-admin view``, assembled from its source files in ``static/``.

The page is one HTML document with its style sheet and scripts inlined, so the view serves
it on one route (``/``) and a reverse proxy in front needs no route for assets. The source
stays in separate files: ``static/view.html`` is the markup, ``static/view.css`` the style
sheet and ``static/js/*.js`` the scripts, in the order the markup lists them. They are
classic scripts that share one global scope, so the order is the order of their top-level
code; every file starts with ``"use strict"``.
"""

from __future__ import annotations

import re
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Final

STYLE: Final = re.compile(r'<link rel="stylesheet" href="([a-z_/]+\.css)">')
SCRIPT: Final = re.compile(r'<script src="([a-z_/]+\.js)"></script>')


def static_root() -> Traversable:
    return files("graph_cli_admin.maintenance_cli").joinpath("static")


def read_asset(name: str) -> str:
    node = static_root()
    for part in name.split("/"):
        node = node.joinpath(part)
    return node.read_text(encoding="utf-8")


def build_page() -> bytes:
    """``view.html`` with every ``<link rel="stylesheet">`` and ``<script src>`` it names
    replaced by the file's text inside ``<style>`` and ``<script>``."""

    def style(match: re.Match[str]) -> str:
        return f"<style>\n{inline_safe(match.group(1), '</style')}</style>"

    def script(match: re.Match[str]) -> str:
        return f"<script>\n{inline_safe(match.group(1), '</script')}</script>"

    html = read_asset("view.html")
    html = STYLE.sub(style, html)
    html = SCRIPT.sub(script, html)
    return html.encode("utf-8")


def inline_safe(name: str, closing: str) -> str:
    """The text of a file that is to sit inside an element: it must not end the element."""
    text = read_asset(name)
    if closing in text.lower():
        raise ValueError(f"{name} contains {closing}>, which would end its element early")
    return text if text.endswith("\n") else text + "\n"
