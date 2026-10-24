"""Comparing answers of two installations: local and through graph-server (B14.10, B14.15).

Ids, hashes and times are numbered by first appearance and the actor's
principal, client and facet sources are left out: the only differences the
server may make.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HASH = re.compile(r"sha256:[0-9a-f]{64}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


def home_env(base: dict[str, str], home: Path) -> dict[str, str]:
    env = {
        **base,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "GRAPH_CLI_GRAPH": "example_graph",
    }
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def normalized(value: Any) -> Any:
    """Ids, hashes and times numbered by first appearance; the actor without principal,
    client, pid and sources."""
    names: dict[str, str] = {}

    def number(kind: str) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            return names.setdefault(match.group(0), f"<{kind}{len(names)}>")

        return replace

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            if "principal" in item and "client" in item:
                return {
                    k: walk({f: v for f, v in facet.items() if f != "source"})
                    for k, facet in item.items()
                    if k not in ("principal", "client", "pid")
                }
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, str):
            item = UUID.sub(number("id"), item)
            item = HASH.sub(number("hash"), item)
            return TIME.sub("<time>", item)
        return item

    return walk(value)


def differences(mine: Any, theirs: Any, path: str = "") -> list[str]:
    """Where two outcomes differ, for the failure message."""
    if isinstance(mine, dict) and isinstance(theirs, dict):
        found = []
        for key in sorted(set(mine) | set(theirs)):
            found += differences(mine.get(key), theirs.get(key), f"{path}.{key}")
        return found
    if isinstance(mine, list) and isinstance(theirs, list) and len(mine) == len(theirs):
        return [
            d
            for i, (a, b) in enumerate(zip(mine, theirs, strict=True))
            for d in differences(a, b, f"{path}[{i}]")
        ]
    return [] if mine == theirs else [f"{path}: remote {mine!r} != local {theirs!r}"]
