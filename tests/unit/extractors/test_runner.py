"""The isolated run: limits, crashes, transient failures (file metadata M11)."""

from __future__ import annotations

import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.extractor_corpus import CORPUS

from graph_cli.extractors.model import ExtractionResult, Transient
from graph_cli.extractors.runner import ExtractLimits, materialized, run_extractor

CHILD = (
    sys.executable,
    "-I",
    "-X",
    "utf8",
    str(Path(__file__).resolve().parents[2] / "support" / "extractor_child.py"),
)
FAST = ExtractLimits(timeout_seconds=3, max_memory_mib=256)


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    path.write_bytes(b"hello\n")
    return path


def _run(name: str, path: Path, limits: ExtractLimits = FAST) -> ExtractionResult | Transient:
    return run_extractor(name, path, "text/plain", limits, command=CHILD)


def test_the_real_child_runs_the_format_extractor() -> None:
    result = run_extractor("format", CORPUS / "notes.txt", "text/plain")
    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"
    assert result.values == {
        "detected_media_type": "text/plain",
        "media_type_mismatch": False,
        "size": 47,
        "text_encoding": "utf_8",
    }


def test_the_child_sees_the_recorded_type_and_the_bytes(sample: Path) -> None:
    result = _run("echo", sample)
    assert result == ExtractionResult(status="ok", values={"size": 6, "recorded": "text/plain"})


def test_the_child_gets_no_environment(sample: Path) -> None:
    """Nothing of the writer's process environment reaches the parser (the interpreter itself
    may set ``LC_CTYPE`` when it coerces the C locale)."""
    result = _run("environ", sample)
    assert isinstance(result, ExtractionResult)
    assert set(result.values["names"]) <= {"LC_CTYPE"}  # type: ignore[arg-type]


def test_a_busy_extractor_hits_the_time_limit(sample: Path) -> None:
    started = time.monotonic()
    result = _run("spin", sample, ExtractLimits(timeout_seconds=1, max_memory_mib=256))
    assert result == ExtractionResult.failed("limit.time")
    assert time.monotonic() - started < 10


def test_a_sleeping_extractor_hits_the_wall_clock(sample: Path) -> None:
    result = _run("sleep", sample, ExtractLimits(timeout_seconds=1, max_memory_mib=256))
    assert result == ExtractionResult.failed("limit.time")


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS ignores RLIMIT_AS")
def test_a_greedy_extractor_hits_the_memory_limit(sample: Path) -> None:
    result = _run("eat", sample, ExtractLimits(timeout_seconds=10, max_memory_mib=256))
    assert result == ExtractionResult.failed("limit.memory")


def test_an_extractor_bug_is_a_failed_result_without_the_message(sample: Path) -> None:
    result = _run("boom", sample)
    assert result == ExtractionResult.failed("extractor.error")


def test_a_crash_is_a_failed_result(sample: Path) -> None:
    assert _run("segv", sample) == ExtractionResult.failed("extractor.crashed")


def test_a_kill_from_outside_is_transient(sample: Path) -> None:
    result = _run("kill", sample)
    assert isinstance(result, Transient)
    assert "signal 9" in result.detail


def test_an_unreadable_path_is_transient(tmp_path: Path) -> None:
    result = run_extractor("format", tmp_path / "missing", "text/plain")
    assert isinstance(result, Transient)
    assert "FileNotFoundError" in result.detail


def test_output_above_the_cap_is_a_failed_result(sample: Path) -> None:
    assert _run("noisy", sample) == ExtractionResult.failed("limit.output")


def test_garbled_output_is_transient(sample: Path) -> None:
    result = _run("chatty", sample)
    assert isinstance(result, Transient)
    assert "no readable answer" in result.detail


def test_an_unknown_extractor_name_is_a_failed_result(sample: Path) -> None:
    assert run_extractor("nope", sample, "text/plain") == ExtractionResult.failed(
        "extractor.unknown"
    )


def test_a_command_that_does_not_start_is_transient(sample: Path) -> None:
    result = run_extractor("format", sample, "text/plain", command=("/nonexistent/python",))
    assert isinstance(result, Transient)


def test_materialized_writes_a_private_file_and_removes_it() -> None:
    with materialized(iter([b"abc", b"def"])) as path:
        assert path.read_bytes() == b"abcdef"
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        kept = path
    assert not kept.exists()
    assert not kept.parent.exists()


def test_materialized_removes_the_file_when_the_stream_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def chunks() -> Iterator[bytes]:
        yield b"abc"
        raise OSError("stream broke")

    with pytest.raises(OSError, match="stream broke"), materialized(chunks()):
        pass
    assert list(tmp_path.iterdir()) == []


def test_the_real_child_reads_a_materialized_blob() -> None:
    data = (CORPUS / "table.csv").read_bytes()
    with materialized(iter([data[:10], data[10:]])) as path:
        result = run_extractor("format", path, "text/csv")
    assert isinstance(result, ExtractionResult)
    assert result.values["size"] == len(data)
