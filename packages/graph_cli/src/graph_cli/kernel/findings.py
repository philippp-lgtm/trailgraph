"""Finding types and the finding-code grammar (contracts R.4-R.7, B9.11).

Finding codes are owned by the component that produces them; each owner
exports ``FINDING_CODES: tuple[FindingCodeSpec, ...]``. The kernel only
defines the shapes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue, canonical_bytes

FINDING_CODE_RE: Final = re.compile(
    r"^(model|data|integrity)\.[a-z_]+\.[a-z0-9_]+$|^ext\.[a-z][a-z0-9_]{1,31}\.[a-z0-9_.]+$"
)

Scope = Literal["model", "data", "integrity", "ext"]
Layer = Literal[
    "structural",
    "referential",
    "semantic",
    "lifecycle",
    "mutability",
    "temporal",
    "rule",
    "audit",
    "storage",
    "ext",
]
When = Literal["release", "write", "batch", "verify"]
FixtureKind = Literal["broken_ontology", "broken_changeset", "tamper", "none"]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class FindingCodeSpec:
    code: str
    scope: Scope
    layer: Layer
    default_severity: Severity
    when: When
    fixture: FixtureKind
    summary: str

    def __post_init__(self) -> None:
        if not FINDING_CODE_RE.match(self.code):
            raise ValueError(f"invalid finding code: {self.code!r}")
        parts = self.code.split(".")
        if parts[0] != self.scope:
            raise ValueError(f"{self.code!r} does not start with its scope {self.scope!r}")
        if self.scope != "ext" and parts[1] != self.layer:
            raise ValueError(f"{self.code!r} does not match its layer {self.layer!r}")


@dataclass(frozen=True, slots=True)
class Finding:
    """One validation result. ``target`` keys: ``id``, ``rev``, ``type``, ``path``, ``module``."""

    severity: Severity
    code: str
    target: Mapping[str, JsonValue]
    message: str
    expected: JsonValue | None = None
    actual: JsonValue | None = None
    rule_id: str | None = None
    hint: str | None = None
    connecting: JsonValue | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "severity": self.severity.value,
            "code": self.code,
            "target": dict(self.target),
            "message": self.message,
        }
        for key in ("expected", "actual", "rule_id", "hint", "connecting"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


def _sort_key(finding: Finding) -> tuple[bytes, str, str]:
    return (canonical_bytes(finding.target), finding.code, finding.message)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Deterministic order: target (canonical bytes), then code, then message (B7.9)."""
    return sorted(findings, key=_sort_key)


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.severity is Severity.ERROR for f in findings)
