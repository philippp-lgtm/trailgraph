"""Reading the published function or action catalog of a graph directory (K6, section 5
``functions/``; actions plan A8 ``actions/``): one implementation for both kinds.

``<kind>/slot.json`` names the tree hash of the sources, the hash of the
compiled catalog and the ontology pin at publish time; the sources live in
``<kind>/files/<hash hex>/<name>.yaml`` and the compiled document
(``graph_cli.functions/1``, ``graph_cli.action_catalog/1``) in
``<kind>/compiled/<compiled hash hex>.json``.
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
from graph_cli.kernel.ports.catalog import KINDS, KindSpec, KineticSlot, SlotKind, sources_hash
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.layout import GraphLayout

# The compiled schemas are ``KindSpec.catalog_schema``: they mirror
# kinetic.format.CATALOG_SCHEMA and kinetic.action_format.CATALOG_SCHEMA (storage may not
# import kinetic; a test pins the equality).


def read_slot(layout: GraphLayout, kind: SlotKind, *, sources: bool) -> KineticSlot | None:
    """The published catalog of ``kind``, ``None`` when there is none; a damaged slot is
    ``functions.slot_invalid``, ``actions.slot_invalid`` or ``views.slot_invalid``."""
    spec = KINDS[kind]
    slot = f"graphs/{layout.root.name}/{kind}/slot.json"
    try:
        data = layout.kinetic_slot(kind).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable(f"cannot read {slot}", exc) from exc
    try:
        doc = as_object(parse_json(data), "slot.json")
        schema = doc.get("schema")
        if schema != spec.slot_schema:
            raise GraphCliError(
                "format.unsupported_version",
                f"{slot} has schema {schema!r}",
                details={"expected": spec.slot_schema, "actual": str(schema)},
            )
        digest, compiled_hash = req_str(doc, "hash"), req_str(doc, "compiled_hash")
        pin = OntologyPin.from_json(req_obj(doc, "published_against"), "published_against")
    except (CanonicalError, JsonShapeError) as exc:
        raise _invalid(spec, slot, f"unreadable: {exc}") from exc
    for value in (digest, compiled_hash):
        if not HASH_RE.match(value):
            raise _invalid(spec, slot, f"hash {value!r} is not sha256:<hex>")
    compiled = _read_compiled(
        layout.kinetic_compiled(kind, compiled_hash), compiled_hash, spec, slot
    )
    files: dict[str, bytes] = {}
    if sources:
        found = _read_files(layout.kinetic_files(kind, digest), kind)
        if found is None:
            raise _invalid(spec, slot, f"the sources of the published {kind} are missing")
        if sources_hash(found) != digest:
            raise _invalid(spec, slot, "the sources differ from the published hash")
        files = found
    return KineticSlot(digest, compiled_hash, pin, compiled, files, kind)


def _read_compiled(path: Path, digest: str, spec: KindSpec, slot: str) -> JsonObject:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise _invalid(spec, slot, "the compiled catalog is missing") from None
    except OSError as exc:
        raise _unavailable("cannot read the compiled catalog", exc) from exc
    try:
        doc = as_object(parse_json(data), "compiled catalog")
        if doc.get("schema") != spec.catalog_schema:
            raise GraphCliError(
                "format.unsupported_version",
                f"{slot}: the compiled catalog has schema {doc.get('schema')!r}",
                details={"expected": spec.catalog_schema, "actual": str(doc.get("schema"))},
            )
        req_list(doc, spec.list_key)
    except (CanonicalError, JsonShapeError) as exc:
        raise _invalid(spec, slot, f"the compiled catalog is unreadable: {exc}") from exc
    if hash_value(dict(doc)) != digest:
        raise _invalid(spec, slot, "the compiled catalog differs from the published hash")
    return dict(doc)


def _read_files(root: Path, kind: str) -> dict[str, bytes] | None:
    if not root.is_dir():
        return None
    files: dict[str, bytes] = {}
    try:
        for name in sorted(os.listdir(root)):
            path = root / name
            if stat.S_ISREG(os.lstat(path).st_mode):  # only graph-admin writes here
                files[name] = path.read_bytes()
    except OSError as exc:
        raise _unavailable(f"cannot read the published {kind[:-1]} sources", exc) from exc
    return files


def _invalid(spec: KindSpec, slot: str, message: str) -> GraphCliError:
    return GraphCliError(spec.invalid_code, f"{slot}: {message}", details={"file": slot})


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")
