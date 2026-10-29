"""Reading the published function catalog of a graph directory (K6, section 5 ``functions/``).

``functions/slot.json`` names the tree hash of the sources, the hash of the
compiled catalog and the ontology pin at publish time; the sources live in
``functions/files/<hash hex>/<name>.yaml`` and the compiled document
(``graph_cli.functions/1``) in ``functions/compiled/<compiled hash hex>.json``.
Both are created once and never changed, and ``slot.json`` is switched with
one rename, so a reader sees the old or the new catalog, never a mix.
Lock-free and read-only, shared by the catalog and the maintenance port. The
app reads only the compiled document; ``sources=True`` also reads the
sources, for graph-admin.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from graph_cli.kernel.canonical import HASH_RE, CanonicalError, JsonObject, hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError, as_object, req_list, req_obj, req_str
from graph_cli.kernel.ports.catalog import FUNCTION_SLOT_SCHEMA, FunctionsSlot, functions_hash
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.layout import GraphLayout

CATALOG_SCHEMA = "graph_cli.functions/1"  # kinetic.format.CATALOG_SCHEMA; storage may not import it


def read_functions(layout: GraphLayout, *, sources: bool) -> FunctionsSlot | None:
    """The published catalog, ``None`` when there is none; a damaged slot is
    ``functions.slot_invalid``."""
    slot = f"graphs/{layout.root.name}/functions/slot.json"
    try:
        data = layout.functions_slot.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable(f"cannot read {slot}", exc) from exc
    try:
        doc = as_object(parse_json(data), "slot.json")
        schema = doc.get("schema")
        if schema != FUNCTION_SLOT_SCHEMA:
            raise GraphCliError(
                "format.unsupported_version",
                f"{slot} has schema {schema!r}",
                details={"expected": FUNCTION_SLOT_SCHEMA, "actual": str(schema)},
            )
        digest, compiled_hash = req_str(doc, "hash"), req_str(doc, "compiled_hash")
        pin = OntologyPin.from_json(req_obj(doc, "published_against"), "published_against")
    except (CanonicalError, JsonShapeError) as exc:
        raise _invalid(slot, f"unreadable: {exc}") from exc
    for value in (digest, compiled_hash):
        if not HASH_RE.match(value):
            raise _invalid(slot, f"hash {value!r} is not sha256:<hex>")
    compiled = _read_compiled(layout.functions_compiled(compiled_hash), compiled_hash, slot)
    files: dict[str, bytes] = {}
    if sources:
        found = _read_files(layout.functions_files(digest))
        if found is None:
            raise _invalid(slot, "the sources of the published functions are missing")
        if functions_hash(found) != digest:
            raise _invalid(slot, "the sources differ from the published hash")
        files = found
    return FunctionsSlot(digest, compiled_hash, pin, compiled, files)


def _read_compiled(path: Path, digest: str, slot: str) -> JsonObject:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise _invalid(slot, "the compiled catalog is missing") from None
    except OSError as exc:
        raise _unavailable("cannot read the compiled catalog", exc) from exc
    try:
        doc = as_object(parse_json(data), "compiled catalog")
        if doc.get("schema") != CATALOG_SCHEMA:
            raise GraphCliError(
                "format.unsupported_version",
                f"{slot}: the compiled catalog has schema {doc.get('schema')!r}",
                details={"expected": CATALOG_SCHEMA, "actual": str(doc.get("schema"))},
            )
        req_list(doc, "functions")
    except (CanonicalError, JsonShapeError) as exc:
        raise _invalid(slot, f"the compiled catalog is unreadable: {exc}") from exc
    if hash_value(dict(doc)) != digest:
        raise _invalid(slot, "the compiled catalog differs from the published hash")
    return dict(doc)


def _read_files(root: Path) -> dict[str, bytes] | None:
    if not root.is_dir():
        return None
    files: dict[str, bytes] = {}
    try:
        for name in sorted(os.listdir(root)):
            path = root / name
            if stat.S_ISREG(os.lstat(path).st_mode):  # only graph-admin writes here
                files[name] = path.read_bytes()
    except OSError as exc:
        raise _unavailable("cannot read the published function sources", exc) from exc
    return files


def _invalid(slot: str, message: str) -> GraphCliError:
    return GraphCliError("functions.slot_invalid", f"{slot}: {message}", details={"file": slot})


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")
