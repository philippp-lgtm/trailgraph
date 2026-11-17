"""The ``format`` extractor (file metadata M5): detected media type, size, mismatch.

Always enabled with the ``files`` module; every other extractor dispatches on its result.
The type comes from the bytes alone. ``media_type_mismatch`` says that the type the file was
recorded with cannot be true of the detected one.
"""

from __future__ import annotations

import os
from typing import Final

from graph_cli.extractors.detect import DETECT_BYTES, compatible, detect_content
from graph_cli.extractors.model import ExtractionResult, Extractor, ModuleRef, RunInput
from graph_cli.kernel.canonical import JsonValue

NAME: Final = "format"
VERSION: Final = 1


def _always(detected: str, recorded: str) -> bool:
    del detected, recorded
    return True


def run(source: RunInput) -> ExtractionResult:
    with open(source.path, "rb") as handle:
        head = handle.read(DETECT_BYTES)
        size = os.fstat(handle.fileno()).st_size
    detected = detect_content(head)
    values: dict[str, JsonValue] = {
        "detected_media_type": detected.media_type,
        "media_type_mismatch": not compatible(source.recorded_media_type, detected.media_type),
        "size": size,
    }
    if detected.text_encoding is not None:
        values["text_encoding"] = detected.text_encoding
    return ExtractionResult(status="ok", values=values)


FORMAT: Final = Extractor(
    name=NAME,
    version=VERSION,
    module=ModuleRef("files", "1.1.0"),
    record_class="files:FormatMetadata",
    accepts=_always,
    run=run,
    summary="Media type detected from the bytes, size and whether the recorded type fits.",
)
