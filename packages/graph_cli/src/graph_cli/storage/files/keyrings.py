"""Store formats of a graph directory and the key rings of sealed graphs (plan T16).

A graph's ``manifest.toml [store] format_version`` says how its records are
stored: ``1`` in the clear, ``2`` sealed under the keys of its key store
(``storage.files.keystore``). ``KeyRings`` opens one key store per graph and
process and keeps it open; the key store module, and with it the cipher
library, is imported only when a sealed graph is opened or created.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.clock import Clock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, IdProvider
from graph_cli.kernel.record_codec import RecordCodec
from graph_cli.storage.common.faults import FaultHook
from graph_cli.storage.files.durable import Syncer

if TYPE_CHECKING:
    from graph_cli.storage.files.keystore import Kek, KeyStore

FILES_BACKEND: Final = "files"
PLAIN_FORMAT: Final = 1  # [store] format_version: records in the clear
SEALED_FORMAT: Final = 2  # [store] format_version: records sealed (plan T16)
STORE_FORMATS: Final = frozenset({PLAIN_FORMAT, SEALED_FORMAT})


@dataclass(frozen=True, slots=True)
class KeySettings:
    """Where the key stores and the key-encryption keys are (``config.toml [keys]``)."""

    dir: Path
    kek_file: Path
    previous_kek_file: Path | None = None

    def store_path(self, graph: GraphId) -> Path:
        return self.dir / f"{graph}.sqlite"


class KeyRings:
    """The open key stores of this process, one per sealed graph. Thread-safe."""

    def __init__(
        self,
        settings: KeySettings,
        *,
        clock: Clock,
        ids: IdProvider,
        faults: FaultHook | None = None,
        syncer: Syncer | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._ids = ids
        self._faults = faults
        self._syncer = syncer
        self._lock = threading.Lock()
        self._stores: dict[GraphId, KeyStore] = {}
        self._keks: tuple[Kek, ...] | None = None

    def ring(self, graph: GraphId) -> KeyStore:
        """The key store of ``graph``, opened on first use."""
        from graph_cli.storage.files.keystore import KeyStore

        with self._lock:
            store = self._stores.get(graph)
            if store is None:
                store = KeyStore.load(
                    self.settings.store_path(graph),
                    graph,
                    self._load_keks(),
                    clock=self._clock,
                    ids=self._ids,
                    faults=self._faults,
                )
                self._stores[graph] = store
            return store

    def create(self, graph: GraphId) -> KeyStore:
        """A new, empty key store for ``graph`` (before the graph is published)."""
        from graph_cli.storage.files.keystore import KeyStore

        with self._lock:
            store = KeyStore.create(
                self.settings.store_path(graph),
                graph,
                self._load_keks()[0],
                clock=self._clock,
                ids=self._ids,
                faults=self._faults,
                syncer=self._syncer,
            )
            self._stores[graph] = store
            return store

    def check(self) -> None:
        """``config.invalid`` unless the key-encryption keys can be read."""
        with self._lock:
            self._load_keks()

    def close(self) -> None:
        with self._lock:
            for store in self._stores.values():
                store.close()
            self._stores.clear()

    def _load_keks(self) -> tuple[Kek, ...]:
        from graph_cli.storage.files.keystore import read_kek_file

        if self._keks is None:
            kek_file = self.settings.kek_file
            if not os.path.lexists(kek_file):
                raise GraphCliError(
                    "config.invalid",
                    f"the key-encryption key file {kek_file} does not exist; graph-admin init "
                    "creates one, or [keys] kek_file names where it is",
                    details={"file": str(kek_file)},
                )
            keks = [read_kek_file(kek_file)]
            if self.settings.previous_kek_file is not None:
                keks.append(read_kek_file(self.settings.previous_kek_file))
            self._keks = tuple(keks)
        return self._keks


def check_store_format(graph: GraphId, backend: str, format_version: int) -> None:
    """``format.unsupported_version`` for a store this program cannot read."""
    if backend != FILES_BACKEND or format_version not in STORE_FORMATS:
        raise GraphCliError(
            "format.unsupported_version",
            f"graph {graph} uses store {backend}/{format_version}",
            details={
                "expected": f"{FILES_BACKEND}/{PLAIN_FORMAT} or {FILES_BACKEND}/{SEALED_FORMAT}",
                "actual": f"{backend}/{format_version}",
            },
        )


def codec_for(graph: GraphId, format_version: int, rings: KeyRings | None) -> RecordCodec:
    """The record codec of a graph of ``format_version``; a sealed one needs ``rings``."""
    if format_version != SEALED_FORMAT:
        return RecordCodec(graph)
    if rings is None:
        raise GraphCliError(
            "config.invalid",
            f"graph {graph} is sealed, and no key store settings are configured here",
            details={"graph_id": str(graph)},
        )
    return RecordCodec(graph, rings.ring(graph))
