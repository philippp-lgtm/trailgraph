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
    """Pin the output of ``name@version`` per corpus file.

    An entry of the golden is written once: ``GRAPH_CLI_UPDATE_GOLDENS=1`` adds the entries of
    new corpus files (or the whole golden of a new version) and never rewrites one that exists.
    Output that changes without a version bump therefore fails; the corpus can still grow.
    """
    path = golden_path(extractor)
    pinned: dict[str, JsonValue] = json.loads(path.read_text()) if path.exists() else {}
    missing = sorted(set(produced) - set(pinned))
    if UPDATE_GOLDENS and missing:
        pinned.update({name: produced[name] for name in missing})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
        missing = []
    assert not missing, (
        f"{path.name} has no entry for {missing}; a new extractor, version or corpus file "
        "needs one: run with GRAPH_CLI_UPDATE_GOLDENS=1"
    )
    changed = sorted(name for name in produced if produced[name] != pinned[name])
    assert not changed, (
        f"{extractor.name}@{extractor.version} produces other output than its golden for "
        f"{changed}: bump `version` of the extractor (any change that can change output does, "
        "a library upgrade included) and create the new golden; for a version that was never "
        f"released, delete {path.name} and regenerate"
    )
