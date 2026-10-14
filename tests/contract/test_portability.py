"""Byte-identical tx lines from InMemoryStore and FileStore (contracts B4.4 portability)."""

from pathlib import Path

from support.stores import StoreEnv

from graph_cli.kernel.canonical import jsonl_line
from graph_cli.kernel.records import tx_to_json
from graph_cli.storage.files.store import FileStore


def test_same_input_gives_byte_identical_tx_lines(ontology: str, tmp_path: Path) -> None:
    memory = StoreEnv("memory", ontology, tmp_path / "m", seed=7)
    files = StoreEnv("files", ontology, tmp_path / "f", seed=7)
    for env in (memory, files):
        env.random(30)
    assert isinstance(files.store, FileStore)
    file_bytes = b"".join(data for _, data in files.store.txlog.read_all())
    memory_bytes = b"".join(jsonl_line(tx_to_json(r)) for r in memory.store.changes_since(0))
    assert file_bytes == memory_bytes
    assert memory.store.head() == files.store.head()
    file_events = b"".join(data for _, data in files.store.auditlog.read_all())
    memory_events = b"".join(jsonl_line(e) for e in memory.audit_events())
    assert file_events == memory_events
