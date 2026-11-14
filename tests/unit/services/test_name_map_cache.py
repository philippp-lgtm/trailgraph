"""The name maps a server shares between its requests (migration plan M7, step 8).

A request builds its read service anew, so ``NameMaps`` take a process-wide ``NameMapCache``: a
complete map (every file of its chain readable) is built once per (graph, pin); one that met a
damaged file is used for its request only, so the next request tries again.
"""

import threading
from uuid import uuid4

from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin
from graph_cli.services.names import NameMapCache, NameMaps
from graph_cli.temporal_knowledge.name_map import NameMap


def entry(version: str = "2.0.0", graph: GraphId | None = None) -> GraphEntry:
    return GraphEntry(
        id=graph or GraphId(uuid4()),
        name="example_graph",
        state="active",
        pin=OntologyPin("inventory", version, f"sha256:{version}"),
        alias_prefixes={},
        extensions=(),
    )


class Builds:
    """``NameMaps._build`` replaced: counts the builds and says whether each was complete."""

    def __init__(self, complete: bool = True) -> None:
        self.complete = complete
        self.count = 0

    def __call__(self, found: GraphEntry) -> tuple[NameMap, bool]:
        self.count += 1
        return NameMap([found.pin.version], {}), self.complete


def maps(shared: NameMapCache | None, builds: Builds) -> NameMaps:
    made = NameMaps(None, None, None, shared)  # type: ignore[arg-type]
    made._build = builds  # type: ignore[method-assign]
    return made


def test_a_complete_map_outlives_the_request() -> None:
    shared, builds = NameMapCache(), Builds()
    found = entry()
    first = maps(shared, builds).of(found)
    again = maps(shared, builds)  # the next request's service
    assert again.of(found) is first and again.of(found) is first
    assert builds.count == 1 and len(shared) == 1


def test_without_a_shared_cache_a_map_lasts_one_service() -> None:
    builds, found = Builds(), entry()
    one = maps(None, builds)
    assert one.of(found) is one.of(found)
    maps(None, builds).of(found)
    assert builds.count == 2


def test_a_map_that_met_a_damaged_file_is_not_shared() -> None:
    shared, builds, found = NameMapCache(), Builds(complete=False), entry()
    one = maps(shared, builds)
    assert one.of(found) is one.of(found)  # still once per request
    assert len(shared) == 0
    maps(shared, builds).of(found)
    assert builds.count == 2  # the next request reads the files again
    builds.complete = True
    maps(shared, builds).of(found)
    assert len(shared) == 1


def test_a_map_is_per_graph_and_pin() -> None:
    shared, builds = NameMapCache(), Builds()
    graph = GraphId(uuid4())
    service = maps(shared, builds)
    service.of(entry("2.0.0", graph))
    service.of(entry("3.0.0", graph))
    service.of(entry("2.0.0"))
    assert builds.count == 3 and len(shared) == 3


def test_the_cache_is_bounded_and_thread_safe() -> None:
    shared = NameMapCache(limit=4)
    entries = [entry() for _ in range(40)]

    def fill(part: list[GraphEntry]) -> None:
        for found in part:
            shared.put((found.id, found.pin), NameMap([found.pin.version], {}))
            shared.get((found.id, found.pin))

    threads = [threading.Thread(target=fill, args=(entries[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(shared) <= 4
