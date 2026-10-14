"""IR -> compiled ontology (contracts B7.3, B7.7).

``compile_bundle`` is the whole pipeline: load (parse + imports), validate,
compile. Compilation only runs on a bundle without error findings, so it can
assume every reference resolves. The output is deterministic: the same
sources always give the same ``artifact_hash``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Finding, has_errors, sort_findings
from graph_cli.ontology_layer.bundle import IrBundle, ModuleSource, TermKind, load_bundle
from graph_cli.ontology_layer.expressions import Cond, lower
from graph_cli.ontology_layer.ir import Doc, IrAttribute, IrModule
from graph_cli.ontology_layer.model import (
    AttributeDef,
    AttributeKind,
    Bound,
    ClassDef,
    CompiledModel,
    DatatypeDef,
    EnumDef,
    GuardDef,
    LifecycleDef,
    ModuleInfo,
    NamedValue,
    QualifierDef,
    RelationDef,
    RuleDef,
    TermDoc,
    TransitionDef,
)
from graph_cli.ontology_layer.report import Report
from graph_cli.ontology_layer.validator import attribute_default, validate_model


@dataclass(frozen=True, slots=True)
class CompileOutcome:
    findings: tuple[Finding, ...]
    model: CompiledModel | None  # None when there is any error finding


def compile_bundle(main: str, source: ModuleSource) -> CompileOutcome:
    report = Report()
    bundle = load_bundle(main, source, report)
    if bundle is None or has_errors(report.findings):
        return CompileOutcome(tuple(sort_findings(report.findings)), None)
    findings = sort_findings([*report.findings, *validate_model(bundle)])
    if has_errors(findings):
        return CompileOutcome(tuple(findings), None)
    return CompileOutcome(tuple(findings), compile_model(bundle))


def _doc(doc: Doc) -> TermDoc:
    return TermDoc(doc.summary, doc.description, doc.deprecated, dict(doc.annotations))


class _Compiler:
    def __init__(self, bundle: IrBundle) -> None:
        self.b = bundle

    def ref(self, module_id: str, ref: str, kind: TermKind) -> str:
        curie = self.b.resolve(module_id, ref, kind)
        assert curie is not None, f"unresolved {kind} {ref!r} in a validated bundle"
        return curie

    def typed(self, module_id: str, ref: str) -> tuple[AttributeKind, str]:
        resolved = self.b.resolve_type(module_id, ref)
        assert resolved is not None
        kind, type_ref = resolved
        assert kind in ("base", "datatype", "enum")
        return kind, type_ref  # type: ignore[return-value]

    def attribute(self, owner: str, module_id: str, attribute: IrAttribute) -> AttributeDef:
        if attribute.lifecycle is not None:
            kind: AttributeKind = "lifecycle"
            type_ref = self.ref(module_id, attribute.lifecycle, "lifecycle")
        else:
            assert attribute.type is not None
            kind, type_ref = self.typed(module_id, attribute.type)
        default, _ = attribute_default(self.b, module_id, attribute)
        return AttributeDef(
            type=type_ref,
            kind=kind,
            required=attribute.required,
            many=attribute.many,
            unique=attribute.unique,
            default=default,
            mutability=attribute.mutability,
            declared_in=owner,
            renamed_from=attribute.renamed_from,
            doc=_doc(attribute.doc),
        )

    def lower(self, module_id: str, conds: tuple[Cond, ...]) -> JsonValue:
        return lower(
            conds,
            lambda ref: self.ref(module_id, ref, "relation"),
            lambda ref: self.ref(module_id, ref, "class"),
        )

    def module_terms(self, module_id: str, module: IrModule, out: _Terms) -> None:
        curie = self.b.curie
        for name, d in module.datatypes.items():
            out.datatypes[curie(module_id, name)] = DatatypeDef(d.base, dict(d.facets), _doc(d.doc))
        for name, e in module.enums.items():
            out.enums[curie(module_id, name)] = EnumDef(
                tuple(NamedValue(v.name, v.summary) for v in e.values), _doc(e.doc)
            )
        prefix = module.info.prefix
        for name, lc in module.lifecycles.items():
            transitions = tuple(
                TransitionDef(
                    from_state=source,
                    to_state=t.to,
                    summary=t.summary,
                    guards=tuple(
                        GuardDef(f"{prefix}.{g.id}", g.summary, self.lower(module_id, g.require))
                        for g in t.guards
                    ),
                )
                for t in lc.transitions
                for source in t.from_states
            )
            out.lifecycles[curie(module_id, name)] = LifecycleDef(
                states=tuple(NamedValue(s.name, s.summary) for s in lc.states.values()),
                initial=tuple(s.name for s in lc.states.values() if s.initial),
                terminal=tuple(s.name for s in lc.states.values() if s.terminal),
                transitions=transitions,
                doc=_doc(lc.doc),
            )
        for name, cls in module.classes.items():
            class_curie = curie(module_id, name)
            attributes = {
                attr_name: self.attribute(owner, owner_module, attribute)
                for attr_name, (owner, owner_module, attribute) in self.b.effective_attributes(
                    class_curie
                ).items()
            }
            out.classes[class_curie] = ClassDef(
                abstract=cls.abstract,
                extends=self.b.parent(module_id, cls),
                ancestors=self.b.ancestors(class_curie),
                attributes=attributes,
                display_prefix=cls.display_prefix,
                renamed_from=cls.renamed_from,
                mappings=module.mappings.get(name, {}),
                doc=_doc(cls.doc),
            )
        for name, rel in module.relations.items():
            qualifiers = {}
            for q_name, q in rel.qualifiers.items():
                q_kind, q_type = self.typed(module_id, q.type)
                qualifiers[q_name] = QualifierDef(q_type, q_kind, q.required, q.many, q.doc.summary)
            out.relations[curie(module_id, name)] = RelationDef(
                domain=tuple(sorted(self.ref(module_id, r, "class") for r in rel.domain)),
                range=tuple(sorted(self.ref(module_id, r, "class") for r in rel.range)),
                out=Bound(rel.out.min, rel.out.max),
                into=Bound(rel.into.min, rel.into.max),
                characteristics=rel.characteristics,
                inverse_label=rel.inverse_label,
                qualifiers=dict(sorted(qualifiers.items())),
                renamed_from=rel.renamed_from,
                mappings=module.mappings.get(name, {}),
                doc=_doc(rel.doc),
            )
        for rule in module.rules:
            then = self.lower(module_id, rule.then)
            expr = (
                then if rule.if_ is None else {"implies": [self.lower(module_id, rule.if_), then]}
            )
            out.rules[f"{prefix}.{rule.id}"] = RuleDef(
                id=f"{prefix}.{rule.id}",
                severity=rule.severity,
                when=rule.when,
                target=self.ref(module_id, rule.scope, "class"),
                expr=expr,
                doc=_doc(rule.doc),
            )


@dataclass
class _Terms:
    datatypes: dict[str, DatatypeDef] = field(default_factory=dict)
    enums: dict[str, EnumDef] = field(default_factory=dict)
    lifecycles: dict[str, LifecycleDef] = field(default_factory=dict)
    classes: dict[str, ClassDef] = field(default_factory=dict)
    relations: dict[str, RelationDef] = field(default_factory=dict)
    rules: dict[str, RuleDef] = field(default_factory=dict)


def _sorted[T](items: Mapping[str, T]) -> dict[str, T]:
    return dict(sorted(items.items()))


def compile_model(bundle: IrBundle) -> CompiledModel:
    """Compile a validated bundle (no error findings) into its runtime form."""
    compiler = _Compiler(bundle)
    out = _Terms()
    for module_id in bundle.order:
        compiler.module_terms(module_id, bundle.modules[module_id], out)
    main = bundle.modules[bundle.main].info
    modules = tuple(
        ModuleInfo(
            id=info.id,
            prefix=info.prefix,
            namespace=info.namespace,
            version=info.version,
            summary=info.doc.summary,
            extensions=dict(bundle.modules[module_id].extensions),
        )
        for module_id in bundle.order
        for info in (bundle.modules[module_id].info,)
    )
    rules = tuple(_sorted(out.rules).values())
    return CompiledModel(
        bundle_id=main.id,
        bundle_version=main.version,
        modules=modules,
        prefixes={m.prefix: m.namespace for m in sorted(modules, key=lambda m: m.prefix)},
        datatypes=_sorted(out.datatypes),
        enums=_sorted(out.enums),
        lifecycles=_sorted(out.lifecycles),
        classes=_sorted(out.classes),
        relations=_sorted(out.relations),
        rules=rules,
    )
