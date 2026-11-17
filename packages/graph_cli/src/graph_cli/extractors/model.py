"""The extractor contract (file metadata M10) and the shapes of a run.

An extractor is code with a version contract: any change that can change its output for some
input bumps ``version``, a library upgrade included. The golden corpus pins the output of every
version. Extractors know nothing of the graph; the service that runs them (``services``) turns
a result into a reconcile document of its record class.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue

STATUS_OK: Final = "ok"
STATUS_FAILED: Final = "failed"

# Problem codes of the limits and of the extractor process itself (a failed record's ``problem``).
PROBLEM_TIME: Final = "limit.time"
PROBLEM_MEMORY: Final = "limit.memory"
PROBLEM_OUTPUT: Final = "limit.output"
PROBLEM_ERROR: Final = "extractor.error"
PROBLEM_CRASHED: Final = "extractor.crashed"
PROBLEM_UNKNOWN_EXTRACTOR: Final = "extractor.unknown"


@dataclass(frozen=True)
class ModuleRef:
    """The module an extractor's classes live in and the lowest version that has them."""

    id: str
    min_version: str


@dataclass(frozen=True)
class PartResult:
    """One part of a record (a SQLite table): its name inside the record and its values."""

    name: str
    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class ExtractionResult:
    """What one extractor says about one file.

    ``ok`` carries ``values`` (attribute name to value; an attribute left out is unset on the
    record) and ``parts``. ``failed`` means the type applies but the bytes cannot be read as
    it: ``problem`` is a stable code (``pdf.unreadable``, ``limit.time``, ...), values stay
    empty. ``not_applicable`` is never produced here: the service decides it from ``accepts``.
    """

    status: Literal["ok", "failed"]
    problem: str | None = None
    values: Mapping[str, JsonValue] = field(default_factory=dict)
    parts: tuple[PartResult, ...] = ()

    @staticmethod
    def failed(problem: str) -> ExtractionResult:
        return ExtractionResult(status=STATUS_FAILED, problem=problem)


@dataclass(frozen=True)
class Transient:
    """A run that says nothing about the bytes (no permission to read the file, a child killed
    by the system). Never recorded: reported and retried on the next run."""

    detail: str


@dataclass(frozen=True)
class RunInput:
    """What an extractor sees: the file's read-only path and the media type it was recorded
    with when it was added (never its name)."""

    path: Path
    recorded_media_type: str


@dataclass(frozen=True)
class Extractor:
    """One versioned extractor.

    ``accepts(detected, recorded)`` decides from the format record whether the extractor
    applies; ``run`` is called only then, in the child process. ``record_class`` is the CURIE of
    the class of its record; extractors that write parts also name ``part_class`` and the
    ``part_relation`` from the record to them.
    """

    name: str
    version: int
    module: ModuleRef
    record_class: str
    accepts: Callable[[str, str], bool]
    run: Callable[[RunInput], ExtractionResult]
    summary: str
    accepted_types: tuple[str, ...] = ()
    part_class: str | None = None
    part_relation: str | None = None


def accepts_types(*media_types: str) -> Callable[[str, str], bool]:
    """An ``accepts`` function for extractors that dispatch on the detected type alone."""
    accepted = frozenset(media_types)

    def accepts(detected: str, recorded: str) -> bool:
        del recorded
        return detected in accepted

    return accepts
