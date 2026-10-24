"""The ownership marker ``.graph_cli_skill.json`` of an installed skill (D15 Q5).

Canonical JSON (C.1) without a timestamp, so installing the same content
twice gives the same bytes. It says what the directory is (``skill``,
``kind``), which program installed it, where the content came from
(``source``) and the hash of every file, so ``check`` can tell a stale copy
from a hand-edited one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from graph_cli.kernel.canonical import (
    HASH_RE,
    CanonicalError,
    JsonValue,
    canonical_bytes,
    hash_bytes,
    parse_json,
)
from graph_cli.kernel.errors import EMPTY
from graph_cli.kernel.json_access import JsonShapeError, as_object, req_obj, req_str
from graph_cli.kernel.skill_marker import MARKER_SCHEMA
from graph_cli.skills.tree import SkillFiles

SkillKind = Literal["app", "graph"]
KINDS: Final = ("app", "graph")


@dataclass(frozen=True, slots=True)
class Marker:
    skill: str
    kind: SkillKind
    program: Mapping[str, JsonValue]  # {name, version}
    source: Mapping[str, JsonValue] = field(default=EMPTY)
    files: Mapping[str, str] = field(default_factory=dict)  # path -> sha256 of the bytes

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": MARKER_SCHEMA,
            "skill": self.skill,
            "kind": self.kind,
            "program": dict(self.program),
            "source": dict(self.source),
            "files": dict(sorted(self.files.items())),
        }


def file_hashes(files: SkillFiles) -> dict[str, str]:
    return {name: hash_bytes(files[name]) for name in sorted(files)}


def encode_marker(marker: Marker) -> bytes:
    return canonical_bytes(marker.to_json())


def decode_marker(data: bytes) -> Marker | None:
    """The marker, or ``None`` when it is unreadable or of another schema."""
    try:
        doc = as_object(parse_json(data), "marker")
        if doc.get("schema") != MARKER_SCHEMA:
            return None
        kind = req_str(doc, "kind")
        files = req_obj(doc, "files")
        if kind not in KINDS or not all(
            isinstance(v, str) and HASH_RE.match(v) for v in files.values()
        ):
            return None
        return Marker(
            skill=req_str(doc, "skill"),
            kind="app" if kind == "app" else "graph",
            program=req_obj(doc, "program"),
            source=req_obj(doc, "source"),
            files={name: str(value) for name, value in files.items()},
        )
    except (CanonicalError, JsonShapeError):
        return None
