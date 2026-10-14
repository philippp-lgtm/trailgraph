"""The union of every component's finding codes (contracts R.4).

The catalog sits above every owner, so it may import them all. Codes are
unique across owners.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.findings import FindingCodeSpec
from graph_cli.ontology_layer import finding_codes as ontology_codes
from graph_cli.storage import finding_codes as storage_codes

_OWNERS = (ontology_codes.FINDING_CODES, audit_codes.FINDING_CODES, storage_codes.FINDING_CODES)


def all_finding_codes() -> Mapping[str, FindingCodeSpec]:
    """Every finding code by name; a code owned twice is a bug."""
    merged: dict[str, FindingCodeSpec] = {}
    for specs in _OWNERS:
        for spec in specs:
            if spec.code in merged:
                raise ValueError(f"finding code {spec.code!r} has two owners")
            merged[spec.code] = spec
    return MappingProxyType(dict(sorted(merged.items())))
