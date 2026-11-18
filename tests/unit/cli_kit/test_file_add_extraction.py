"""``graph file add`` never fails because of the extraction after its commit (file metadata M12):
whatever goes wrong there is one warning, ``file.extraction_incomplete``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from graph_cli.app_cli.commands import files
from graph_cli.kernel.errors import GraphCliError


class _Service:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def extract(self, request: object) -> object:
        raise self.error


def _run(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> tuple[Any, Any]:
    context = SimpleNamespace(actor=object(), file_metadata=_Service(error))
    monkeypatch.setattr(files, "context", lambda state: context)
    state = SimpleNamespace(warnings=())
    outcome = files._extract(state, "graph-id", "file-id", {})  # type: ignore[arg-type]
    return outcome, state.warnings


@pytest.mark.parametrize(
    "error", [OSError("no space left"), RuntimeError("bug"), KeyError("x"), ValueError("v")]
)
def test_an_unexpected_error_is_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    outcome, warnings = _run(monkeypatch, error)
    assert outcome == ([], None)
    (warning,) = warnings
    assert warning.code == "file.extraction_incomplete" and warning.details == {"cause": "internal"}
    assert "no space" not in warning.message  # the text of an unexpected error stays out


def test_a_caller_without_the_right_to_write_records_is_told_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = GraphCliError(
        "access.denied",
        "write.create on graph:g/type:files:FormatMetadata is not permitted",
        details={
            "action": "write.create",
            "resource": "graph:g/type:files:FormatMetadata",
            "rule": "default.deny",
        },
    )
    outcome, warnings = _run(monkeypatch, denied)
    assert outcome == ([], None)
    (warning,) = warnings
    assert warning.details == {
        "cause": "access.denied",
        "action": "write.create",
        "resource": "graph:g/type:files:FormatMetadata",
    }
    assert "you may not write its metadata records" in warning.message
    assert "write.create on graph:g/type:files:FormatMetadata" in warning.message
    assert "graph-admin files extract" in warning.message


def test_another_error_that_ends_the_run_is_named_by_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, warnings = _run(monkeypatch, GraphCliError("backend.lock_timeout", "slow"))
    (warning,) = warnings
    assert warning.details == {"cause": "backend.lock_timeout"}
    assert "backend.lock_timeout" in warning.message
