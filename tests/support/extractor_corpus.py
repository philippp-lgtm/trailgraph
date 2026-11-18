"""The golden corpus of the extractors (``tests/fixtures/files/``) and its goldens."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from graph_cli.extractors.media_types import SNIFF_BYTES
from graph_cli.extractors.media_types import detect as recorded_type
from graph_cli.extractors.model import ExtractionResult, Extractor, RunInput
from graph_cli.kernel.canonical import JsonValue
from support.fixtures import UPDATE_GOLDENS

CORPUS: Final = Path(__file__).resolve().parents[1] / "fixtures" / "files"
GOLDENS: Final = Path(__file__).resolve().parents[1] / "goldens" / "extractors"
NOT_CORPUS: Final = frozenset({"generate.py", "manifest.json", "README.md"})


def corpus_names() -> list[str]:
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    return sorted(manifest)


def recorded_media_type(name: str) -> str:
    """The type ``file add`` would record: the manifest's explicit one, else the add-time table."""
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    override = manifest[name].get("recorded_media_type")
    if override is not None:
        return str(override)
    head = (CORPUS / name).read_bytes()[:SNIFF_BYTES]
    return recorded_type(head, name)


def result_json(result: ExtractionResult) -> dict[str, JsonValue]:
    return {
        "status": result.status,
        "problem": result.problem,
        "values": dict(result.values),
        "parts": [{"name": p.name, "values": dict(p.values)} for p in result.parts],
    }


def run_corpus(extractor: Extractor, detected_of: dict[str, str]) -> dict[str, JsonValue]:
    """What the extractor says about every corpus file it applies to; the others are listed
    as ``not_applicable``. ``detected_of`` maps a file to the format extractor's type."""
    out: dict[str, JsonValue] = {}
    for name in corpus_names():
        recorded = recorded_media_type(name)
        if not extractor.accepts(detected_of[name], recorded):
            out[name] = "not_applicable"
            continue
        result = extractor.run(RunInput(CORPUS / name, recorded))
        out[name] = {"recorded_media_type": recorded, "result": result_json(result)}
    return out


def golden_path(extractor: Extractor) -> Path:
    return GOLDENS / f"{extractor.name}@{extractor.version}.json"


def assert_version_golden(extractor: Extractor, produced: dict[str, JsonValue]) -> None:
    """Pin the output of ``name@version``.

    The golden of a version is append-only. ``GRAPH_CLI_UPDATE_GOLDENS=1`` creates a missing
    one and adds the entries of corpus files the golden does not have yet, but never changes
    an entry that is there: output that changes without a version bump fails.
    """
    path = golden_path(extractor)
    text = json.dumps(produced, indent=2, sort_keys=True) + "\n"
    if UPDATE_GOLDENS:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        else:
            existing = json.loads(path.read_text())
            unchanged = all(produced.get(name) == entry for name, entry in existing.items())
            if unchanged and set(produced) - set(existing):
                path.write_text(text)
    assert path.exists(), (
        f"missing golden {path.name}; a new extractor or version needs one: "
        "run with GRAPH_CLI_UPDATE_GOLDENS=1"
    )
    assert path.read_text() == text, (
        f"{extractor.name}@{extractor.version} produces other output than its golden: bump "
        f"`version` of the extractor (any change that can change output does, a library "
        f"upgrade included) and create the new golden; for a version that was never released, "
        f"delete {path.name} and regenerate"
    )
