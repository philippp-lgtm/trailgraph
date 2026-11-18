"""A child process with test-only extractors that misbehave, for the runner's limit paths.

Run as ``python -I tests/support/extractor_child.py``: the same entry point as
``graph_cli.extractors.child``, with extra names in the registry.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from graph_cli.extractors import registry
from graph_cli.extractors.child import main
from graph_cli.extractors.model import (
    ExtractionResult,
    Extractor,
    ModuleRef,
    RunInput,
    accepts_types,
)


def _spin(source: RunInput) -> ExtractionResult:
    while True:
        pass


def _sleep(source: RunInput) -> ExtractionResult:
    time.sleep(60)
    return ExtractionResult(status="ok")


def _eat(source: RunInput) -> ExtractionResult:
    blocks = [bytearray(64 * 1024 * 1024) for _ in range(32)]
    return ExtractionResult(status="ok", values={"blocks": len(blocks)})


def _boom(source: RunInput) -> ExtractionResult:
    raise ValueError("secret file content must not leak")


def _kill(source: RunInput) -> ExtractionResult:
    os.kill(os.getpid(), signal.SIGKILL)
    return ExtractionResult(status="ok")


def _segv(source: RunInput) -> ExtractionResult:
    os.kill(os.getpid(), signal.SIGSEGV)
    return ExtractionResult(status="ok")


def _noisy(source: RunInput) -> ExtractionResult:
    return ExtractionResult(status="ok", values={"text": "x" * (2 * 1024 * 1024)})


def _echo(source: RunInput) -> ExtractionResult:
    data = source.path.read_bytes()
    return ExtractionResult(
        status="ok",
        values={"size": len(data), "recorded": source.recorded_media_type},
        parts=(),
    )


def _environ(source: RunInput) -> ExtractionResult:
    return ExtractionResult(status="ok", values={"names": sorted(os.environ)})


def _oserror(source: RunInput) -> ExtractionResult:
    source.path.read_bytes()
    raise OSError("the bytes did it")


def _noise_on_stdout(source: RunInput) -> ExtractionResult:
    sys.stdout.write("not json")
    return ExtractionResult(status="ok")


for name, run in {
    "spin": _spin,
    "sleep": _sleep,
    "eat": _eat,
    "boom": _boom,
    "kill": _kill,
    "segv": _segv,
    "noisy": _noisy,
    "echo": _echo,
    "chatty": _noise_on_stdout,
    "environ": _environ,
    "oserror": _oserror,
}.items():
    registry.REGISTRY[name] = Extractor(
        name=name,
        version=1,
        module=ModuleRef("files", "1.1.0"),
        record_class="files:FormatMetadata",
        accepts=accepts_types("text/plain"),
        run=run,
        summary="test only",
    )

if __name__ == "__main__":
    sys.exit(main())
