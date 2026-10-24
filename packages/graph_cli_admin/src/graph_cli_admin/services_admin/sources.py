"""``graph-admin graph pull|push``: a graph's authored sources in a maintainer workspace.

A workspace directory holds what a graph was released and published from,
in the layout of the maintainer workspace:

- ``ontology/<version>/<module_id>.yaml``: the modules of a release,
  byte-identical to ``source/`` of the installed release;
- ``skill/graph-<graph>/...``: the published skill tree, byte-identical to
  the slot;
- ``.graph_cli_source.json`` (``graph_cli.graph_source/1``): the base, what
  the data root held when the directory was pulled or last pushed (graph,
  ontology pin, skill hash).

``pull`` writes the pinned release (or another installed one) and the
skill. It reads the data root without a lock (a release never changes, the
slot switches by one rename) and is authorized ``maint.graph_configure``:
whoever may change the configuration may read it. It owns three entries of
the directory: the version directory, the skill directory and the base
file. Any of them existing is ``admin.source_exists`` unless ``force``
replaces them; everything else (a README, other version directories,
``.git``) stays. The base file is written last.

``push`` is one maintenance operation under the graph lock, like a
fast-forward push: the data root must still hold the base, or already what
is pushed (a repeated push), else ``admin.source_conflict``. The highest
version directory is the ontology: when it differs from the base it is
released and pinned (``release.plan|write``); a skill tree that differs
from the base is published (``skill.write``). Whenever either changes, the
skill the graph will have is checked against the ontology it will have.
Every check runs before the first write; the new base is written into the
directory afterwards (``dry_run``: answered, not written).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Final

from graph_cli.kernel.canonical import JsonValue, hash_bytes, parse_json, pretty_bytes
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot, skill_hash
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.ontology_layer.semver import Version, is_version
from graph_cli.ontology_layer.vocabulary import MODULE_ID_RE
from graph_cli.skills.tree import MAX_NAME, NAME_RE, Problem, read_tree
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli_admin.services_admin import release, skill
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import RELEASE_FILE, Compiled, compile_source

SOURCE_FILE: Final = ".graph_cli_source.json"
SOURCE_SCHEMA: Final = "graph_cli.graph_source/1"
ONTOLOGY: Final = "ontology"
SKILL: Final = "skill"
PARTS: Final = frozenset({SOURCE_FILE, ONTOLOGY, SKILL})  # what push reads of a workspace
_RELEASE_SOURCES: Final = "source/"
_TEMP: Final = ".graph_cli_source.tmp"


@dataclass(frozen=True, slots=True)
class Base:
    """What ``.graph_cli_source.json`` says the workspace was made from."""

    graph_id: str
    graph_name: str
    pin: OntologyPin
    skill: str | None  # the skill tree's hash; None: the graph had no skill


@dataclass(frozen=True, slots=True)
class Pulled:
    entry: GraphEntry
    data: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Pushed:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


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
    pin, modules = _release_sources(ctx, entry, version, installed[version])
    slot = ctx.catalog.read_skill(entry.id)
    name = graph_skill_name(entry.name)
    files = {f"{ONTOLOGY}/{version}/{module}.yaml": data for module, data in modules.items()}
    if slot is not None:
        files |= {f"{SKILL}/{name}/{path}": data for path, data in slot.files.items()}
    files[SOURCE_FILE] = pretty_bytes(base_document(entry, pin, slot))
    write_workspace(target, files, graph_ref=graph_ref, force=force)
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "path": str(target),
        "ontology": {**pin.to_json(), "modules": sorted(modules), "pinned": pin == entry.pin},
        "skill": _skill_json(name, slot),
        "files": sorted(files),
    }
    return Pulled(entry, data)


def _release_sources(
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
    entry: GraphEntry, pin: OntologyPin, slot: SkillSlot | None
) -> dict[str, JsonValue]:
    return {
        "schema": SOURCE_SCHEMA,
        "graph": {"id": str(entry.id), "name": entry.name},
        "ontology": pin.to_json(),
        "skill": _skill_json(graph_skill_name(entry.name), slot),
    }


def _skill_json(name: str, slot: SkillSlot | None) -> JsonValue:
    return None if slot is None else {"name": name, "kind": slot.kind, "hash": slot.hash}


def workspace_entry(name: str) -> str | None:
    """The entry of a workspace that pull owns and ``name`` lies in: the base file, a
    version directory or the skill directory; ``None`` for any other path (an answer
    over the API never writes elsewhere)."""
    if name == SOURCE_FILE:
        return name
    parts = name.split("/")
    if "\\" in name or "\x00" in name or any(p in ("", ".", "..") for p in parts):
        return None
    if len(parts) == 3 and parts[0] == ONTOLOGY and is_version(parts[1]):
        module = parts[2].removesuffix(".yaml")
        return "/".join(parts[:2]) if parts[2].endswith(".yaml") and _module(module) else None
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


# ---- push ------------------------------------------------------------------------------


def read_base(source: Path) -> Base:
    """The base of a workspace; a usage error names what is missing or broken."""
    file = source / SOURCE_FILE
    try:
        document = _object(file.read_bytes())
    except FileNotFoundError:
        raise _usage(f"{source} has no {SOURCE_FILE}: pull the graph into it first") from None
    except OSError as exc:
        raise _usage(f"cannot read {file}: {exc.strerror or exc}") from exc
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
        digest: str | None = None
        if found is not None:
            value = found.get("hash") if isinstance(found, Mapping) else None
            if not isinstance(value, str):
                raise ValueError("skill must be null or name a hash")
            digest = value
    except (KeyError, ValueError, GraphCliError) as exc:
        message = exc.message if isinstance(exc, GraphCliError) else str(exc)
        raise _usage(f"{file} is not a base file of graph-admin graph pull: {message}") from exc
    return Base(graph_id, name, pin, digest)


def push(
    ctx: AdminContext,
    *,
    graph_ref: str,
    source: Path,
    reason: str | None,
    dry_run: bool,
    inventory: Mapping[str, JsonValue],
) -> Pushed:
    entry = ctx.resolve(graph_ref, "graph push")
    release.check_state(entry)
    base = read_base(source)
    if base.graph_id != str(entry.id):
        raise _usage(
            f"{source} holds graph {base.graph_name} ({base.graph_id}), not {entry.name};"
            " pull this graph into another directory"
        )
    if not dry_run and not os.access(source, os.W_OK):
        raise _usage(f"{source} is not writable: push records the new base there")
    compiled = _ontology(source, base)
    name = graph_skill_name(entry.name)
    skill_dir = _skill_dir(source, name)
    files: dict[str, bytes] | None = None
    problems: list[Problem] = []
    if skill_dir is not None:
        try:
            files, problems = read_tree(skill_dir)
        except OSError as exc:
            raise _usage(f"cannot read {skill_dir}: {exc.strerror or exc}") from exc
    pushed = skill_hash(files) if files is not None else None
    skill_changed = pushed is not None and pushed != base.skill
    ontology_changed = _ontology_changed(ctx, entry, base, compiled, source / ONTOLOGY)
    meta = RequestMeta.of(
        "graph push",
        {
            "graph": str(entry.id),
            "ontology": compiled.pin.to_json(),
            "skill": pushed,
            "dry_run": dry_run,
        },
    )
    resource = f"graph:{entry.id}"
    configure = ctx.authorize(
        [("maint.graph_configure", resource)], request=meta, graph=entry.id, reason=reason
    )
    decisions = release.authorize(ctx, entry, meta, reason=reason) if ontology_changed else None
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        release.check_state(entry)
        current = skill.current_slot(session)
        target_pin = compiled.pin if ontology_changed else base.pin
        target_skill = pushed if skill_changed else base.skill
        current_skill = current.hash if current is not None else None
        if entry.pin not in (base.pin, target_pin) or current_skill not in (
            base.skill,
            target_skill,
        ):
            raise _conflict(source, entry, base, current)
        planned = release.plan(ctx, session, compiled) if ontology_changed else None
        releasing = planned is not None and not planned.unchanged
        after = replace(entry, pin=target_pin) if releasing else entry
        publishing = skill_changed and current_skill != pushed
        checked: skill.Checked | None = None
        if releasing or publishing:
            # The skill the graph will have must fit the ontology it will have.
            model = compiled.model if releasing else None
            label: Path | str = skill_dir or f"the published skill of graph {entry.name}"
            check = files if files is not None else (current.files if current else None)
            if check is not None:
                checked = skill.check_files(
                    ctx,
                    after,
                    dict(check),
                    problems,
                    label=label,
                    directory=name if files is not None else None,
                    inventory=inventory,
                    model=model,
                )
                skill.refuse(label, after, name, checked.problems)
        slot = None
        if publishing:
            assert checked is not None and files is not None and pushed is not None
            slot = SkillSlot(checked.kind, pushed, after.pin, files)
        seq = session.head().seq
        if not dry_run:
            if releasing:
                assert planned is not None and decisions is not None
                after = release.write(ctx, session, planned, decisions, meta, reason=reason)
            if slot is not None:
                skill.write(ctx, session, after, configure, meta, current, slot, reason=reason)
        ontology: dict[str, JsonValue] = (
            planned.data
            if planned is not None
            else {
                "from": entry.pin.to_json(),
                "to": entry.pin.to_json(),
                "released": False,
                "pinned": False,
            }
        )
        final = slot if slot is not None else current
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "ontology": {k: v for k, v in ontology.items() if k != "graph_id"},
            "skill": None
            if files is None
            else {
                "skill": name,
                "from": current.summary() if current is not None else None,
                "to": final.summary() if final is not None else None,
                "files": sorted(files),
                "published": slot is not None and not dry_run,
            },
            "unchanged": not (releasing or publishing),
            "dry_run": dry_run,
            "source": base_document(after, after.pin, final),
        }
    if not dry_run:
        write_base(source, base_document(after, after.pin, final))
    warnings = planned.warnings if planned is not None else ()
    return Pushed(after, data, seq, warnings)


def _ontology(source: Path, base: Base) -> Compiled:
    """The highest version directory of ``ontology/``, compiled with the base's main
    module; its name must be the version its main module declares."""
    directory = source / ONTOLOGY
    try:
        versions = sorted(
            (Version.parse(p.name) for p in directory.iterdir() if is_version(p.name)),
            reverse=True,
        )
    except OSError as exc:
        raise _usage(f"cannot read {directory}: {exc.strerror or exc}") from exc
    if not versions:
        raise _usage(f"{directory} holds no version directory (<major>.<minor>.<patch>)")
    latest = directory / str(versions[0])
    main = base.pin.bundle
    compiled = compile_source(latest, main if (latest / f"{main}.yaml").is_file() else None)
    declared = compiled.model.bundle_version
    if declared != latest.name:
        raise _usage(
            f"{latest} declares version {declared}; name the directory after the version"
            " (bump the module version in a new directory)"
        )
    return compiled


def _skill_dir(source: Path, name: str) -> Path | None:
    """``skill/<name>/`` when it exists; any other entry of ``skill/`` is a usage error
    (a renamed graph, a typo), except hidden files such as ``.DS_Store``."""
    directory = source / SKILL
    try:
        found = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _usage(f"cannot read {directory}: {exc.strerror or exc}") from exc
    other = [n for n in found if n != name]
    if other:
        raise _usage(f"{directory} holds {', '.join(other)}; the skill of this graph is {name}")
    return directory / name if found else None


def _ontology_changed(
    ctx: AdminContext, entry: GraphEntry, base: Base, compiled: Compiled, directory: Path
) -> bool:
    """Whether the workspace's ontology is a new release. The base version is compared by
    its authored modules (the compiled hash may change with the compiler while they do
    not); edited modules under the base version are ``admin.release_exists``: a release
    never changes, so even an edited comment needs a new version."""
    if compiled.pin.version != base.pin.version:
        return True
    installed = release_hashes(GraphLayout(ctx.catalog.graphs_dir / str(entry.id)))
    if base.pin.version not in installed:
        return True  # cannot happen after a pull; the release decides
    _, modules = _release_sources(ctx, entry, base.pin.version, installed[base.pin.version])
    if dict(compiled.sources) == modules:
        return False
    raise GraphCliError(
        "admin.release_exists",
        f"{directory} differs from release {base.pin.version}; copy it to a new version"
        " directory and bump the module version there",
        details={
            "version": base.pin.version,
            "expected": installed[base.pin.version],
            "actual": compiled.pin.artifact_hash,
        },
    )


def _conflict(
    source: Path, entry: GraphEntry, base: Base, current: SkillSlot | None
) -> GraphCliError:
    return GraphCliError(
        "admin.source_conflict",
        f"graph {entry.name} changed since {source} was pulled or last pushed; pull it into"
        " another directory, carry your changes over and push from there",
        details={
            "graph": entry.name,
            "dir": str(source),
            "base": {"ontology": base.pin.to_json(), "skill": base.skill},
            "current": {
                "ontology": entry.pin.to_json(),
                "skill": current.hash if current is not None else None,
            },
        },
    )


def _object(data: bytes) -> Mapping[str, JsonValue] | None:
    try:
        document = parse_json(data)
    except ValueError:  # CanonicalError and malformed JSON
        return None
    return document if isinstance(document, Mapping) else None


def _usage(message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": "dir"})
