"""Finding collection for model checks.

Model findings target ``{"module": <module id>, "path": <dotted IR path>}``.
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Finding
from graph_cli.ontology_layer.finding_codes import FINDING_SPECS


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(
        self,
        code: str,
        module: str,
        path: str,
        message: str,
        *,
        expected: JsonValue | None = None,
        actual: JsonValue | None = None,
        rule_id: str | None = None,
    ) -> None:
        spec = FINDING_SPECS[code]
        self.findings.append(
            Finding(
                severity=spec.default_severity,
                code=code,
                target={"module": module, "path": path},
                message=message,
                expected=expected,
                actual=actual,
                rule_id=rule_id,
            )
        )
