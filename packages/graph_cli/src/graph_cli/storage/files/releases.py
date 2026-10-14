"""Reading manifests and installed ontology releases of a graph directory (B7.4, B12.8).

Lock-free and read-only, shared by the catalog (runtime reads) and the
maintenance port (verify, release flows).
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Final

from graph_cli.kernel.canonical import CanonicalError, hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import ManifestData
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.manifest import parse_manifest

VERSION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMPILED_FILE: Final = "compiled.json"


def read_manifest(layout: GraphLayout) -> tuple[ManifestData, tuple[WarningRecord, ...]]:
    """The graph's manifest plus warnings about unknown keys."""
    source = f"graphs/{layout.root.name}/manifest.toml"
    try:
        data = layout.manifest.read_bytes()
    except FileNotFoundError:
        raise GraphCliError(
            "config.invalid", f"{source} is missing", details={"file": source, "key": ""}
        ) from None
    except OSError as exc:
        raise _unavailable(f"cannot read {source}", exc) from exc
    return parse_manifest(data, source=source)


def release_hashes(layout: GraphLayout) -> dict[str, str]:
    """Installed versions and the C.2 hash of their ``compiled.json`` (unreadable: left out)."""
    try:
        names = os.listdir(layout.ontology)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _unavailable("cannot list releases", exc) from exc
    found = {}
    for name in sorted(names):
        if not VERSION_RE.match(name):
            continue
        try:
            document = parse_json((layout.release_dir(name) / COMPILED_FILE).read_bytes())
        except (OSError, CanonicalError):
            continue
        found[name] = hash_value(document)
    return found


def read_release_file(layout: GraphLayout, version: str, name: str) -> bytes:
    if not VERSION_RE.match(version) or not is_relative_name(name):
        raise GraphCliError("internal.error", f"bad release file reference {version}/{name}")
    path = layout.release_dir(version).joinpath(*PurePosixPath(name).parts)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise GraphCliError(
            "ontology.artifact_missing",
            f"release {version} has no file {name}",
            details={"version": version, "file": name},
        ) from None
    except OSError as exc:
        raise _unavailable("cannot read a release file", exc) from exc


def is_relative_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (
        bool(parts)
        and not name.startswith("/")
        and "\\" not in name
        and all(part not in ("", ".", "..") for part in parts)
    )


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")
