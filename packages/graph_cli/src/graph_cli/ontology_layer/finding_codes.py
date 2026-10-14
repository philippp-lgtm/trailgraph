"""Finding codes owned by the ontology layer (contracts R.4, R.6, section 1.5.2).

Every ``model.*`` code has exactly one broken fixture under
``tests/fixtures/broken/<code>/`` (completeness test). ``data.*`` codes are
added together with ``validate_write``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Layer, Severity

_E = Severity.ERROR
_W = Severity.WARNING


def _model(layer: Layer, name: str, summary: str, severity: Severity = _E) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"model.{layer}.{name}",
        scope="model",
        layer=layer,
        default_severity=severity,
        when="release",
        fixture="broken_ontology",
        summary=summary,
    )


FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    # structural: document shape, names, local facts
    _model("structural", "invalid_yaml", "The file is not strict YAML."),
    _model("structural", "unsupported_format", "The file is not in format graph_cli.ir/1."),
    _model("structural", "unknown_key", "A key is not part of the IR."),
    _model("structural", "invalid_value", "A value has the wrong type or grammar."),
    _model("structural", "missing_summary", "A term has no summary."),
    _model("structural", "duplicate_term", "A name is defined twice in one module."),
    _model("structural", "unknown_datatype", "An attribute or datatype names an unknown type."),
    _model("structural", "cardinality_min_gt_max", "A cardinality minimum exceeds its maximum."),
    _model("structural", "invalid_pattern", "A pattern is not anchored or does not compile."),
    _model("structural", "reserved_name", "An attribute or qualifier uses a reserved name."),
    _model("structural", "invalid_default", "A default value does not fit its attribute."),
    _model("structural", "facet_not_applicable", "A facet does not apply to the base type."),
    # referential: names that must resolve
    _model("referential", "unknown_import", "An imported module does not exist."),
    _model("referential", "import_cycle", "Modules import each other in a cycle."),
    _model("referential", "import_version_mismatch", "An import's version is out of range."),
    _model("referential", "unknown_parent", "A class extends an unknown class."),
    _model("referential", "inheritance_cycle", "Classes extend each other in a cycle."),
    _model("referential", "unknown_domain", "A relation domain names an unknown class."),
    _model("referential", "unknown_range", "A relation range names an unknown class."),
    _model("referential", "unknown_lifecycle", "An attribute names an unknown lifecycle."),
    _model("referential", "unknown_relation", "A condition names an unknown relation."),
    _model("referential", "unknown_term", "A mapping or condition names an unknown term."),
    _model("referential", "inverse_label_collision", "An inverse label is used twice."),
    # semantic
    _model("semantic", "abstract_never_subclassed", "An abstract class has no subclass.", _W),
    _model("semantic", "qualifier_unknown_datatype", "A qualifier names an unknown type."),
    _model("semantic", "unique_on_non_scalar", "Uniqueness is declared on a non-scalar value."),
    _model("semantic", "attribute_redefined", "A class redefines an inherited attribute."),
    # lifecycle
    _model("lifecycle", "no_initial_state", "A lifecycle has no initial state."),
    _model("lifecycle", "unreachable_state", "A state cannot be reached from an initial state."),
    _model("lifecycle", "undefined_state", "A transition names an undefined state."),
    _model("lifecycle", "terminal_has_outgoing", "A terminal state has an outgoing transition."),
    _model("lifecycle", "guard_unknown_attribute", "A guard names an unknown attribute."),
    # mutability
    _model(
        "mutability",
        "lifecycle_attribute_not_lifecycle",
        "Lifecycle attributes and mutability 'lifecycle' must go together.",
    ),
    _model("mutability", "unknown_class", "A mutability class is unknown."),
    # rule
    _model("rule", "unknown_scope", "A rule scope names an unknown class."),
    _model("rule", "unknown_attribute", "A rule names an attribute its scope lacks."),
)

FINDING_SPECS: Final = {spec.code: spec for spec in FINDING_CODES}
assert len(FINDING_SPECS) == len(FINDING_CODES), "duplicate finding code"
