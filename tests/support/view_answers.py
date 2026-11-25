"""Helpers for the tests of the view schemas: a view answer to build and the problems of a
document against a schema closed like the test plugin closes it."""

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from graph_cli.kernel.canonical import JsonValue
from support.output_schemas import closed, message, where

type Doc = dict[str, Any]

ENTITY = {"id": "i", "type": "t", "rev": 1, "record_status": "active"}


def problems(schema: Mapping[str, JsonValue], document: Any) -> list[str]:
    """What is wrong with ``document`` against the closed ``schema``, formats checked; for a
    section that fits none of the alternatives, the error of the alternative it comes closest to
    (the message of the test plugin)."""
    found = Draft202012Validator(
        closed(dict(schema)), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [f"{where(error)}: {message(error)}" for error in found.iter_errors(document)]


def head(sections: list[Doc], *, name: str = "v", **more: Any) -> Doc:
    """A view answer with these sections (of the view ``name``)."""
    found: Doc = {"view": {"name": name, "origin": "authored", "hash": "sha256:x"}, **more}
    return found | {"sections": sections, "bytes": 10}
