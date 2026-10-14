"""Contract suite fixtures: every test runs on each backend x each toy ontology (B13.10)."""

from pathlib import Path

import pytest
from support.fixtures import TOY_ONTOLOGIES
from support.stores import BACKENDS, StoreEnv


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=TOY_ONTOLOGIES)
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def env(backend: str, ontology: str, tmp_path: Path) -> StoreEnv:
    return StoreEnv(backend, ontology, tmp_path)
