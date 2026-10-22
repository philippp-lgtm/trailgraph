"""Phase timings of one command (kernel.timing): own time per phase, no overlap."""

from collections.abc import Iterator

import pytest

from graph_cli.kernel import timing
from graph_cli.kernel.timing import Timings


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """A hand-driven ``perf_counter_ns``: ``now[0]`` is the current reading."""
    now = [0]
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: now[0])
    yield now


def test_a_phase_counts_its_own_time(clock: list[int]) -> None:
    timings = Timings()
    clock[0] = 2_000
    with timings.phase("snapshot"):
        clock[0] = 7_000
    clock[0] = 10_000
    assert timings.to_json() == {"total": 10, "phases": {"snapshot": 5, "other": 5}}


def test_a_nested_phase_is_taken_out_of_the_outer_one(clock: list[int]) -> None:
    timings = Timings()
    with timings.phase("authorize"):
        clock[0] = 1_000
        with timings.phase("audit"):
            clock[0] = 4_000
        clock[0] = 5_000
    assert timings.to_json() == {
        "total": 5,
        "phases": {"audit": 3, "authorize": 2, "other": 0},
    }


def test_a_repeated_phase_adds_up(clock: list[int]) -> None:
    timings = Timings()
    for _ in range(3):
        with timings.phase("snapshot"):
            clock[0] += 2_000
        clock[0] += 1_000
    assert timings.to_json() == {"total": 9, "phases": {"snapshot": 6, "other": 3}}


def test_a_failing_phase_still_counts(clock: list[int]) -> None:
    timings = Timings()
    with pytest.raises(RuntimeError), timings.phase("lock"):
        clock[0] = 3_000
        raise RuntimeError("timeout")
    assert timings.to_json()["phases"] == {"lock": 3, "other": 0}


def test_until_now_claims_what_no_phase_claimed(clock: list[int]) -> None:
    timings = Timings(start_ns=-4_000)  # started before this object was built
    with timings.phase("model"):
        clock[0] = 1_000
    timings.until_now("startup")
    clock[0] = 3_000
    assert timings.to_json() == {
        "total": 7,
        "phases": {"model": 1, "startup": 4, "other": 2},
    }
