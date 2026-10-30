"""The installed policies as a ``PolicySource`` (contracts B6.8, B6.14).

``policy/installation.json`` is the installation layer, without it the
built-in policy applies (nothing changes before the first ``graph-admin
policy apply``); ``graphs/<id>/policy.json`` adds that graph's layer, an
absent one is neutral. Files are read lock-free, each once per invocation,
so every decision of one command sees the same policy. A process that runs
for long (``graph-admin view``) passes ``watch`` instead: before each use a
file is checked and read again when it was replaced (``policy apply``
renames a new file into place), so a policy applied meanwhile decides its
next request. A file that is not a
valid compiled document fails closed: ``policy.invalid`` for every call.
graph-admin passes ``fallback`` instead, so it can still replace a broken
policy: the invalid layer is left out (the built-in policy decides for the
installation layer) and its ``policy.invalid`` error goes to ``fallback``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from graph_cli.access_control.documents import (
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    Problem,
    check_document,
)
from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet, PolicySource
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.policy_files import (
    INSTALLATION_POLICY,
    POLICY_DIR,
    installation_policy_path,
    read_policy_file,
)

Fallback = Callable[[GraphCliError], None]
Stamp = tuple[int, int, int] | object | None  # what tells a replaced file apart (``_stamp``)


class FilePolicySource:
    def __init__(
        self, data_root: Path, *, fallback: Fallback | None = None, watch: bool = False
    ) -> None:
        self.data_root = data_root
        self._fallback = fallback
        self._watch = watch
        # each layer with the stamp of the file it was read from (``None`` unless watched)
        self._installation: tuple[Stamp, Mapping[str, JsonValue]] | None = None
        self._graphs: dict[GraphId, tuple[Stamp, Mapping[str, JsonValue] | None]] = {}

    def installation(self) -> Mapping[str, JsonValue]:
        """The installation layer: the installed document or the built-in one."""
        path = installation_policy_path(self.data_root)
        stamp = _stamp(path) if self._watch else None
        cached = self._installation
        if cached is None or cached[0] != stamp:
            shown = f"{POLICY_DIR}/{INSTALLATION_POLICY}"
            found = self._load(path, shown, INSTALLATION_LAYER, None)
            cached = self._installation = (stamp, found if found is not None else BUILTIN_POLICY)
        return cached[1]

    def graph(self, graph: GraphId) -> Mapping[str, JsonValue] | None:
        """The graph's own layer; ``None`` when it has none."""
        path = GraphLayout(self.data_root / "graphs" / str(graph)).policy
        stamp = _stamp(path) if self._watch else None
        cached = self._graphs.get(graph)  # one lookup: a refresh in another thread may clear
        if cached is None or cached[0] != stamp:
            shown = f"graphs/{graph}/policy.json"
            cached = self._graphs[graph] = (stamp, self._load(path, shown, GRAPH_LAYER, str(graph)))
        return cached[1]

    def current(self, graph: GraphId | None) -> PolicySet:
        installation = self.installation()
        return policy_set(installation, self.graph(graph) if graph is not None else None)

    def refresh(self) -> None:
        self._installation = None
        self._graphs.clear()

    def _load(
        self, path: Path, shown: str, layer: str, graph: str | None
    ) -> Mapping[str, JsonValue] | None:
        try:
            data = read_policy_file(path)
        except GraphCliError as exc:  # unreadable is invalid, never "no policy" (B6.14)
            self._refuse(shown, [Problem("", exc.message)])
            return None
        if data is None:
            return None
        try:
            document = parse_json(data)
        except CanonicalError as exc:
            self._refuse(shown, [Problem("", f"not JSON: {exc}")])
            return None
        if not isinstance(document, Mapping):
            self._refuse(shown, [Problem("", "not a JSON object")])
            return None
        problems = check_document(document, layer=layer, graph=graph)
        if problems:
            self._refuse(shown, problems)
            return None
        return document

    def _refuse(self, shown: str, problems: list[Problem]) -> None:
        error = GraphCliError(
            "policy.invalid",
            f"the installed policy {shown} is invalid: "
            + "; ".join(f"{p.path or '.'}: {p.message}" for p in problems[:5]),
            details={"file": shown, "problems": [p.to_json() for p in problems]},
        )
        if self._fallback is None:
            raise error
        self._fallback(error)


def _stamp(path: Path) -> Stamp:
    """``None`` for a missing file, else inode, size and modification time: a renamed-in file
    has a new inode. A file that cannot be checked gets a stamp equal to no other, so it
    is read again and ``read_policy_file`` says why it cannot be."""
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return object()
    return (found.st_ino, found.st_size, found.st_mtime_ns)


if TYPE_CHECKING:

    def _conforms(source: FilePolicySource) -> PolicySource:
        return source
