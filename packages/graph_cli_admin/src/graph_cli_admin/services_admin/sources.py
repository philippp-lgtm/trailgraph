"""``graph-admin graph pull|push``: a graph's authored sources in a maintainer workspace.

A workspace directory holds what a graph was released and published from,
in the layout of the maintainer workspace:

- ``ontology/<version>/<module_id>.yaml``: the modules of a release,
  byte-identical to ``source/`` of the installed release;
- ``skill/graph-<graph>/...``: the published skill tree, byte-identical to
  the slot;
- ``functions/<name>.yaml``: the published function sources
  (``graph_cli.function/1``), byte-identical to the slot;
- ``actions/<name>.yaml``: the published action sources
  (``graph_cli.action/1``), byte-identical to the slot;
- ``views/<name>.yaml``: the published view sources (``graph_cli.view/1``),
  byte-identical to the slot;
- ``.graph_cli_source.json`` (``graph_cli.graph_source/1``): the base, what
  the data root held when the directory was pulled or last pushed (graph,
  ontology pin, skill, functions, actions and views hash; ``views`` only for a graph
  that has published views, so the base of a graph without views is what it always was).

``pull`` writes the pinned release (or another installed one), the skill, the
functions, the actions and the views. It reads the data root without a lock (a release never
changes, the slots switch by one rename) and is authorized
``maint.graph_configure``: whoever may change the configuration may read it.
It owns six entries of the directory: the version directory, the skill
directory, the ``functions``, ``actions`` and ``views`` directories and the base file. Any of
them existing is ``admin.source_exists`` unless ``force`` replaces them; everything else (a
README, other version directories, ``.git``) stays. The base file is written last.

``push`` is ``sources_push``; it reads this layout and the base file through the
functions below.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from graph_cli.kernel.canonical import JsonValue, hash_bytes, parse_json, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SkillSlot
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.kinetic.format import NAME_RE as KINETIC_NAME_RE
from graph_cli.ontology_layer.semver import is_version
from graph_cli.ontology_layer.vocabulary import MODULE_ID_RE
from graph_cli.skills.tree import MAX_NAME, NAME_RE
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import RELEASE_FILE

SOURCE_FILE: Final = ".graph_cli_source.json"
SOURCE_SCHEMA: Final = "graph_cli.graph_source/1"
ONTOLOGY: Final = "ontology"
SKILL: Final = "skill"
FUNCTIONS: Final = "functions"
ACTIONS: Final = "actions"
VIEWS: Final = "views"
PARTS: Final = frozenset(
    {SOURCE_FILE, ONTOLOGY, SKILL, FUNCTIONS, ACTIONS, VIEWS}
)  # what push reads of a workspace
_RELEASE_SOURCES: Final = "source/"
_TEMP: Final = ".graph_cli_source.tmp"


@dataclass(frozen=True, slots=True)
class Base:
    """What ``.graph_cli_source.json`` says the workspace was made from."""

    graph_id: str
    graph_name: str
    pin: OntologyPin
    skill: str | None  # the skill tree's hash; None: the graph had no skill
    functions: str | None = None  # the function sources' hash; None: no catalog published
    actions: str | None = None  # the action sources' hash; None: no catalog published
    views: str | None = None  # the view sources' hash; None: no catalog published


@dataclass(frozen=True, slots=True)
class Pulled:
    entry: GraphEntry
    data: dict[str, JsonValue]


# ---- pull ------------------------------------------------------------------------------


def pull(
    ctx: AdminContext, *, graph_ref: str, target: Path, version: str | None, force: bool
) -> Pulled:
    entry = ctx.resolve(graph_ref, "graph pull")
    meta = RequestMeta.of("graph pull", {"graph": str(entry.id), "version": version})
    ctx.authorize([("maint.graph_configure", f"graph:{entry.id}")], request=meta, graph=entry.id)
    installed = release_hashes(GraphLayout(ctx.catalog.graphs_dir / str(entry.id)))
    version = version or entry.pin.version
    if version not in installed:
        raise GraphCliError(
            "usage.invalid_argument",
            f"graph {entry.name} has no release {version}; installed: {', '.join(installed)}",
            details={"argument": "--version", "installed": sorted(installed)},
        )
    pin, modules = release_sources(ctx, entry, version, installed[version])
    slot = ctx.catalog.read_skill(entry.id)
    published = ctx.catalog.read_functions(entry.id, sources=True)
    catalog = ctx.catalog.read_actions(entry.id, sources=True)
    views = ctx.catalog.read_views(entry.id, sources=True)
    name = graph_skill_name(entry.name)
    files = {f"{ONTOLOGY}/{version}/{module}.yaml": data for module, data in modules.items()}
    if slot is not None:
        files |= {f"{SKILL}/{name}/{path}": data for path, data in slot.files.items()}
    if published is not None:
        files |= {f"{FUNCTIONS}/{file}": data for file, data in published.files.items()}
    if catalog is not None:
        files |= {f"{ACTIONS}/{file}": data for file, data in catalog.files.items()}
    if views is not None:
        files |= {f"{VIEWS}/{file}": data for file, data in views.files.items()}
    files[SOURCE_FILE] = pretty_bytes(base_document(entry, pin, slot, published, catalog, views))
    write_workspace(target, files, graph_ref=graph_ref, force=force)
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "path": str(target),
        "ontology": {**pin.to_json(), "modules": sorted(modules), "pinned": pin == entry.pin},
        "skill": _skill_json(name, slot),
        "functions": _catalog_json(published),
        "actions": _catalog_json(catalog),
        "files": sorted(files),
    }
    if views is not None:  # a graph without views answers as it did
        data["views"] = _catalog_json(views)
    return Pulled(entry, data)


def release_sources(
    ctx: AdminContext, entry: GraphEntry, version: str, artifact_hash: str
) -> tuple[OntologyPin, dict[str, bytes]]:
    """The pin of an installed release and its authored modules, checked against the
    hashes ``release.json`` lists."""
    metadata = _object(ctx.catalog.read_release_file(entry.id, version, RELEASE_FILE))
    listed = metadata.get("files") if metadata is not None else None
    bundle = metadata.get("bundle") if metadata is not None else None
    if not isinstance(listed, Mapping) or not isinstance(bundle, str):
        raise _corrupt(entry, version, f"{RELEASE_FILE} is unreadable")
    modules: dict[str, bytes] = {}
    for name, digest in sorted(listed.items()):
        if not name.startswith(_RELEASE_SOURCES) or not name.endswith(".yaml"):
            continue
        data = ctx.catalog.read_release_file(entry.id, version, name)
        if hash_bytes(data) != digest:
            raise _corrupt(entry, version, f"{name} differs from its hash in {RELEASE_FILE}")
        modules[name[len(_RELEASE_SOURCES) : -len(".yaml")]] = data
    if bundle not in modules:
        raise _corrupt(entry, version, f"the main module {bundle} is not kept")
    return OntologyPin(bundle, version, artifact_hash), modules


def _corrupt(entry: GraphEntry, version: str, why: str) -> GraphCliError:
    return GraphCliError(
        "backend.corrupt",
        f"release {version} of graph {entry.name}: {why}",
        details={"graph_id": str(entry.id), "version": version},
    )


def base_document(
    entry: GraphEntry,
    pin: OntologyPin,
    slot: SkillSlot | None,
    published: KineticSlot | None = None,
    actions: KineticSlot | None = None,
    views: KineticSlot | None = None,
) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {
        "schema": SOURCE_SCHEMA,
        "graph": {"id": str(entry.id), "name": entry.name},
        "ontology": pin.to_json(),
        "skill": _skill_json(graph_skill_name(entry.name), slot),
        "functions": _catalog_json(published),
        "actions": _catalog_json(actions),
    }
    if views is not None:  # the base of a graph without views is what it always was
        document["views"] = _catalog_json(views)
    return document


def _skill_json(name: str, slot: SkillSlot | None) -> JsonValue:
    return None if slot is None else {"name": name, "kind": slot.kind, "hash": slot.hash}


def _catalog_json(slot: KineticSlot | None) -> JsonValue:
    return None if slot is None else {"count": slot.count, "hash": slot.hash}


def workspace_entry(name: str) -> str | None:
    """The entry of a workspace that pull owns and ``name`` lies in: the base file, a
    version directory, the skill directory, the functions, actions or views directory; ``None``
    for any other path (an answer over the API never writes elsewhere)."""
    if name == SOURCE_FILE:
        return name
    parts = name.split("/")
    if "\\" in name or "\x00" in name or any(p in ("", ".", "..") for p in parts):
        return None
    if len(parts) == 3 and parts[0] == ONTOLOGY and is_version(parts[1]):
        module = parts[2].removesuffix(".yaml")
        return "/".join(parts[:2]) if parts[2].endswith(".yaml") and _module(module) else None
    if len(parts) == 2 and parts[0] in (FUNCTIONS, ACTIONS, VIEWS):
        stem = parts[1].removesuffix(FILE_SUFFIX)
        ok = parts[1].endswith(FILE_SUFFIX) and KINETIC_NAME_RE.match(stem) is not None
        return parts[0] if ok else None
    if len(parts) > 2 and parts[0] == SKILL and len(parts[1]) <= MAX_NAME:
        return "/".join(parts[:2]) if NAME_RE.match(parts[1]) else None
    return None


def _module(text: str) -> bool:
    return MODULE_ID_RE.match(text) is not None


def write_workspace(
    target: Path, files: Mapping[str, bytes], *, graph_ref: str, force: bool
) -> None:
    """Write pulled ``files`` below ``target``: refuses when an entry they belong to exists
    (``admin.source_exists``) unless ``force`` replaces it; the base file goes last."""
    entries = {workspace_entry(name) for name in files}
    if None in entries or SOURCE_FILE not in files:
        raise GraphCliError("internal.error", "not the files of a workspace")
    owned = sorted(e for e in entries if e is not None)
    existing = [e for e in owned if _exists(target / e)]
    if existing and not force:
        raise GraphCliError(
            "admin.source_exists",
            f"{target} holds {', '.join(existing)} already; pass --force to replace"
            f" {'it' if len(existing) == 1 else 'them'}",
            details={"graph": graph_ref, "dir": str(target), "paths": existing},
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        for entry in existing:
            path = target / entry
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        for name in sorted(files):
            if name == SOURCE_FILE:
                continue
            path = target.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "xb") as handle:
                handle.write(files[name])
        _write_base(target, files[SOURCE_FILE])
    except OSError as exc:
        raise _unwritable(target, exc) from exc


def write_base(target: Path, document: Mapping[str, JsonValue]) -> None:
    """Record a new base in ``target`` (after a push); unchanged bytes are left alone."""
    data = pretty_bytes(dict(document))
    try:
        if (target / SOURCE_FILE).read_bytes() == data:
            return
    except OSError:
        pass
    try:
        _write_base(target, data)
    except OSError as exc:
        raise _unwritable(target, exc) from exc


def _write_base(target: Path, data: bytes) -> None:
    """Temp file and rename: never writes into an existing file (on the server it may be
    a hard link to an upload)."""
    temp = target / _TEMP
    temp.unlink(missing_ok=True)
    with open(temp, "xb") as handle:
        handle.write(data)
    os.replace(temp, target / SOURCE_FILE)


def _exists(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def _unwritable(target: Path, exc: OSError) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"cannot write {target}: {exc.strerror or exc}",
        details={"argument": "dir"},
    )


# ---- base file --------------------------------------------------------------------------


def read_base(source: Path) -> Base:
    """The base of a workspace; a usage error names what is missing or broken."""
    file = source / SOURCE_FILE
    try:
        document = _object(file.read_bytes())
    except FileNotFoundError:
        raise usage_error(f"{source} has no {SOURCE_FILE}: pull the graph into it first") from None
    except OSError as exc:
        raise usage_error(f"cannot read {file}: {exc.strerror or exc}") from exc
    try:
        if document is None or document.get("schema") != SOURCE_SCHEMA:
            raise ValueError(f"schema is not {SOURCE_SCHEMA}")
        graph = document["graph"]
        ontology = document["ontology"]
        found = document["skill"]
        if not isinstance(graph, Mapping) or not isinstance(ontology, Mapping):
            raise ValueError("graph and ontology must be objects")
        graph_id, name = graph["id"], graph["name"]
        if not isinstance(graph_id, str) or not isinstance(name, str):
            raise ValueError("graph.id and graph.name must be strings")
        pin = OntologyPin.from_json(dict(ontology), "ontology")
        digest = _hash_of(found, "skill")
        catalog = _hash_of(document.get("functions"), "functions")  # absent in older bases
        published = _hash_of(document.get("actions"), "actions")  # absent in older bases
        authored = _hash_of(document.get("views"), "views")  # absent without published views
    except (KeyError, ValueError, GraphCliError) as exc:
        message = exc.message if isinstance(exc, GraphCliError) else str(exc)
        raise usage_error(
            f"{file} is not a base file of graph-admin graph pull: {message}"
        ) from exc
    return Base(graph_id, name, pin, digest, catalog, published, authored)


def _hash_of(found: JsonValue | None, key: str) -> str | None:
    if found is None:
        return None
    value = found.get("hash") if isinstance(found, Mapping) else None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be null or name a hash")
    return value


def _object(data: bytes) -> Mapping[str, JsonValue] | None:
    try:
        document = parse_json(data)
    except ValueError:  # CanonicalError and malformed JSON
        return None
    return document if isinstance(document, Mapping) else None


def usage_error(message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": "dir"})
