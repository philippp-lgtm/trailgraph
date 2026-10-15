"""Reading the published skill of a graph directory (D15 Q2, section 5 ``skill/``).

``skill/source.json`` names the kind, the tree hash and the ontology pin at
publish time; the files live in ``skill/files/<hash hex>/``. A tree
directory is created once and never changed, and ``source.json`` is
switched with one rename, so a reader sees the old or the new skill, never
a mix. Lock-free and read-only, shared by the catalog and the maintenance
port.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from graph_cli.kernel.canonical import HASH_RE, CanonicalError, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError, as_object, req_obj, req_str
from graph_cli.kernel.ports.catalog import SKILL_SOURCE_SCHEMA, SkillSlot, skill_hash
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.layout import GraphLayout


def read_skill(layout: GraphLayout) -> SkillSlot | None:
    """The published skill, ``None`` when there is none; a damaged slot is
    ``skills.source_invalid``."""
    source = f"graphs/{layout.root.name}/skill/source.json"
    try:
        data = layout.skill_source.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable(f"cannot read {source}", exc) from exc
    try:
        doc = as_object(parse_json(data), "source.json")
        schema = doc.get("schema")
        if schema != SKILL_SOURCE_SCHEMA:
            raise GraphCliError(
                "format.unsupported_version",
                f"{source} has schema {schema!r}",
                details={"expected": SKILL_SOURCE_SCHEMA, "actual": str(schema)},
            )
        kind, digest = req_str(doc, "kind"), req_str(doc, "hash")
        pin = OntologyPin.from_json(req_obj(doc, "published_against"), "published_against")
    except (CanonicalError, JsonShapeError) as exc:
        raise _invalid(source, f"unreadable: {exc}") from exc
    if not HASH_RE.match(digest):
        raise _invalid(source, f"hash {digest!r} is not sha256:<hex>")
    files = _read_files(layout.skill_files(digest))
    if files is None:
        raise _invalid(source, "the files of the published skill are missing")
    if skill_hash(files) != digest:
        raise _invalid(source, "the files differ from the published hash")
    return SkillSlot(kind, digest, pin, files)


def _read_files(root: Path) -> dict[str, bytes] | None:
    if not root.is_dir():
        return None
    files: dict[str, bytes] = {}
    try:
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(current) / name
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue  # only graph-admin writes here; it writes regular files
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    except OSError as exc:
        raise _unavailable("cannot read the published skill", exc) from exc
    return files


def _invalid(source: str, message: str) -> GraphCliError:
    return GraphCliError("skills.source_invalid", f"{source}: {message}", details={"file": source})


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")
