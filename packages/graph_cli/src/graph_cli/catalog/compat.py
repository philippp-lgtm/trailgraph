"""The compatibility id of a program (``Graph-Cli-Compat``, contracts B14.16).

What a client brings to a conversation with the server: the wire level, its
inventory with answer schemas and the agent guidance it installs (skill 1
for ``graph``, the bundled admin skills for ``graph-admin``). Two builds
with the same id parse the same commands, read the same answers and tell
the agent the same, so ``[server] client_builds = "compatible"`` lets a
client of another build through and the client only warns. The program
computes its own id (``app_cli`` and ``graph_cli_admin`` know their
inventory and skills); this module hashes and caches it per build.

``WIRE_LEVEL`` is the one input set by hand. Raise it when the request or
answer format changes so that an older client misreads a newer server or
the other way round (headers, envelope, uploads), or when a fix in code
that runs on the client (the remote client, hooked commands, text output,
skill install, self update) must reach every client now.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from graph_cli.catalog.build import BUILD_RE
from graph_cli.kernel.canonical import JsonValue, hash_bytes, hash_value

WIRE_LEVEL: Final = 1
COMPAT_HEADER: Final = "Graph-Cli-Compat"
COMPAT_DIR: Final = "compat"  # below the state root: <program>/<build hex>


def compat_id(inventory: JsonValue, guidance: Mapping[str, Mapping[str, bytes]]) -> str:
    """``sha256:<hex>`` over the wire level, the inventory and the guidance's file hashes
    (skill name to its files, relative path to bytes)."""
    files: dict[str, JsonValue] = {
        skill: {path: hash_bytes(data) for path, data in sorted(tree.items())}
        for skill, tree in sorted(guidance.items())
    }
    return hash_value({"wire": WIRE_LEVEL, "inventory": inventory, "guidance": files})


def cached_compat(
    state_root: Path | None, program: str, build: str | None, compute: Callable[[], str]
) -> str | None:
    """The compatibility id of this program's ``build``: ``None`` without a build id, else
    from ``<state_root>/compat/<program>/<build hex>`` (the build covers every input, so
    the file never goes stale) or computed and stored there."""
    if build is None:
        return None
    if state_root is None:
        return compute()
    path = state_root / COMPAT_DIR / program / build.removeprefix("sha256:")
    with contextlib.suppress(OSError, UnicodeDecodeError):
        found = path.read_text().strip()
        if BUILD_RE.match(found):
            return found
    value = compute()
    with contextlib.suppress(OSError):  # a cache that cannot be written costs one compute
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(value + "\n")
        os.replace(temp, path)
    return value
