"""Helpers for the tests of the view schemas: a view answer to build and the problems of a
document against a schema closed like the test plugin closes it."""

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from graph_cli.kernel.canonical import JsonValue
from support.output_schemas import closed

type Doc = dict[str, Any]

ENTITY = {"id": "i", "type": "t", "rev": 1, "record_status": "active"}


def problems(schema: Mapping[str, JsonValue], document: Any) -> list[str]:
    """What is wrong with ``document`` against the closed ``schema``; for a section that fits none
    of the alternatives, the error of the alternative it comes closest to."""
    found = Draft202012Validator(closed(dict(schema)))
    messages = []
    for error in found.iter_errors(document):
        text = error.message[:100]
        inner = best_match(error.context) if error.context else None
        if inner is not None:
            path = "/".join(str(p) for p in inner.absolute_path)
            text += f" (closest: {path}: {inner.message[:200]})"
        messages.append(f"{'/'.join(str(p) for p in error.absolute_path)}: {text}")
    return messages


def head(sections: list[Doc], *, name: str = "v", **more: Any) -> Doc:
    """A view answer with these sections (of the view ``name``)."""
    found: Doc = {"view": {"name": name, "origin": "authored", "hash": "sha256:x"}, **more}
    return found | {"sections": sections, "bytes": 10}
