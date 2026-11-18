"""The golden corpus pins every extractor's output per version (file metadata M10)."""

from __future__ import annotations

import json
import re

import pytest
from support.extractor_corpus import (
    CORPUS,
    GOLDENS,
    NOT_CORPUS,
    assert_version_golden,
    corpus_names,
    run_corpus,
)

from graph_cli.extractors.detect import DETECT_BYTES, detect_content
from graph_cli.extractors.model import Extractor
from graph_cli.extractors.registry import extractors


def _detected() -> dict[str, str]:
    return {
        name: detect_content((CORPUS / name).read_bytes()[:DETECT_BYTES]).media_type
        for name in corpus_names()
    }


def test_the_manifest_lists_exactly_the_corpus_files() -> None:
    on_disk = {p.name for p in CORPUS.iterdir() if p.name not in NOT_CORPUS}
    assert on_disk == set(corpus_names())


def test_corpus_files_are_tiny() -> None:
    assert max((CORPUS / n).stat().st_size for n in corpus_names()) <= 4096


@pytest.mark.parametrize("extractor", list(extractors().values()), ids=lambda e: e.name)
def test_output_matches_the_golden_of_its_version(extractor: Extractor) -> None:
    assert_version_golden(extractor, run_corpus(extractor, _detected()))


def test_every_golden_belongs_to_a_known_extractor_version() -> None:
    known = extractors()
    for path in GOLDENS.glob("*.json"):
        match = re.fullmatch(r"([a-z][a-z0-9_]*)@([1-9][0-9]*)", path.stem)
        assert match, path.name
        assert match[1] in known, f"golden of an unknown extractor: {path.name}"
        assert int(match[2]) <= known[match[1]].version, f"golden of a future version: {path.name}"
        json.loads(path.read_text())


def test_every_extractor_has_a_golden_for_its_current_version() -> None:
    for extractor in extractors().values():
        assert (GOLDENS / f"{extractor.name}@{extractor.version}.json").exists()
