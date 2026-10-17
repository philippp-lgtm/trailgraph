"""Named file roots of this machine (``config.toml [roots]``; files slice 1).

A stored file records where it came from as ``{root, path}``: a root name
and a relative POSIX path. Each machine maps root names to directories, so
the same origin resolves wherever the root is configured, and graph data
never holds an absolute path (F21: machine facts stay in the device config).

- ``origin_of``: the longest configured root that contains the resolved
  path wins (roots may nest or share a target). A path outside every root
  has no origin.
- ``resolve``: the path of an origin on this machine; a root that is not
  configured here is ``file.root_unknown``. The result is never written.
- Paths are compared as given after ``Path.resolve()``: no case folding on
  case-insensitive file systems, no Unicode normalization (C.5), so a name
  stored in NFD on one machine may not resolve on another.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.values import local_path_problem


class FileRoots:
    def __init__(self, roots: Mapping[str, Path]) -> None:
        self._roots = dict(sorted(roots.items()))

    @classmethod
    def from_config(cls, roots: Mapping[str, str], env: Mapping[str, str]) -> FileRoots:
        """Expand ``~/`` with ``HOME`` like ``[paths] data_root``."""
        expanded: dict[str, Path] = {}
        for name, text in roots.items():
            if text.startswith("~/"):
                home = env.get("HOME", "")
                if not home.startswith("/"):
                    raise GraphCliError(
                        "config.invalid",
                        f"roots.{name} starts with ~/ but HOME is not an absolute path",
                        details={"key": f"roots.{name}"},
                    )
                expanded[name] = Path(home) / text[2:]
            else:
                expanded[name] = Path(text)
        return cls(expanded)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._roots)

    def origin_of(self, path: Path) -> dict[str, JsonValue] | None:
        """``{root, path}`` of an absolute, resolved file path; ``None`` outside every root."""
        best: tuple[int, str, str] | None = None
        for name, root in self._roots.items():
            try:
                relative = path.relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if not relative.parts:
                continue  # the root itself is no file inside it
            depth = len(root.resolve().parts)
            if best is None or depth > best[0]:
                best = (depth, name, relative.as_posix())
        if best is None:
            return None
        origin: dict[str, JsonValue] = {"root": best[1], "path": best[2]}
        return origin if local_path_problem(origin) is None else None

    def resolve(self, origin: Mapping[str, JsonValue]) -> Path:
        """The origin's path on this machine; ``file.root_unknown`` for a root not set here."""
        name, relative = origin.get("root"), origin.get("path")
        if not isinstance(name, str) or not isinstance(relative, str):
            raise GraphCliError("internal.error", "an origin is {root, path}")
        root = self._roots.get(name)
        if root is None:
            raise GraphCliError(
                "file.root_unknown",
                f"the file root {name!r} is not configured on this machine;"
                f" add it to [roots] in config.toml",
                details={"root": name},
            )
        return root.joinpath(*relative.split("/"))
