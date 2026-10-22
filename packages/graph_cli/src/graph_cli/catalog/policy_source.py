"""The installed policies as a ``PolicySource`` (contracts B6.8, B6.14).

``policy/installation.json`` is the installation layer, without it the
built-in policy applies (nothing changes before the first ``graph-admin
policy apply``); ``graphs/<id>/policy.json`` adds that graph's layer, an
absent one is neutral. Files are read lock-free, each once per invocation,
so every decision of one command sees the same policy. A file that is not a
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


class FilePolicySource:
    def __init__(self, data_root: Path, *, fallback: Fallback | None = None) -> None:
        self.data_root = data_root
        self._fallback = fallback
        self._installation: Mapping[str, JsonValue] | None = None
        self._graphs: dict[GraphId, Mapping[str, JsonValue] | None] = {}

    def installation(self) -> Mapping[str, JsonValue]:
        """The installation layer: the installed document or the built-in one."""
        if self._installation is None:
            found = self._load(
                installation_policy_path(self.data_root),
                f"{POLICY_DIR}/{INSTALLATION_POLICY}",
                INSTALLATION_LAYER,
                None,
            )
            self._installation = found if found is not None else BUILTIN_POLICY
        return self._installation

    def graph(self, graph: GraphId) -> Mapping[str, JsonValue] | None:
        """The graph's own layer; ``None`` when it has none."""
        if graph not in self._graphs:
            layout = GraphLayout(self.data_root / "graphs" / str(graph))
            shown = f"graphs/{graph}/policy.json"
            self._graphs[graph] = self._load(layout.policy, shown, GRAPH_LAYER, str(graph))
        return self._graphs[graph]

    def current(self, graph: GraphId | None) -> PolicySet:
        installation = self.installation()
        return policy_set(installation, self.graph(graph) if graph is not None else None)

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


if TYPE_CHECKING:

    def _conforms(source: FilePolicySource) -> PolicySource:
        return source
