"""Authored IR ``graph_cli.ir/1``: typed module model and its parser (contracts B7.1, B7.2).

Parsing checks document shape only (keys, value types, name grammars,
summaries). Cross-references are checked by ``validator`` on the whole
bundle. References keep their authored text: a local name (``Item``) or a
CURIE of a visible module (``core:supersedes``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.yaml_strict import YamlError, YamlNode, YamlScalar, YamlSeq, load_strict
from graph_cli.ontology_layer.expressions import Cond, parse_conditions
from graph_cli.ontology_layer.reader import Reader, join
from graph_cli.ontology_layer.report import Report
from graph_cli.ontology_layer.semver import VersionRange, is_version
from graph_cli.ontology_layer.vocabulary import (
    ALL_FACETS,
    CHARACTERISTICS,
    CURIE_RE,
    DEFAULT_NAMESPACE,
    DISPLAY_PREFIX_RE,
    IR_FORMAT,
    LABEL_ROLES,
    LOWER_NAME_RE,
    MAPPING_KINDS,
    MODULE_ID_RE,
    MUTABILITY_CLASSES,
    NAMESPACE_NS_RE,
    PREFIX_RE,
    RESERVED_MODULE_IDS,
    RULE_TIMINGS,
    SEVERITIES,
    TARGET_INACTIVE_POLICIES,
    TYPE_NAME_RE,
)

_DOC: Final = ("summary", "description", "deprecated", "annotations")
_TOP_KEYS: Final = (
    "format",
    "module",
    "datatypes",
    "enums",
    "classes",
    "relations",
    "lifecycles",
    "rules",
    "mappings",
    "extensions",
    "markings",
)


@dataclass(frozen=True, slots=True)
class Doc:
    """Documentation and metadata shared by every term."""

    summary: str
    description: str | None = None
    deprecated: bool = False
    annotations: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IrImport:
    module: str
    range: VersionRange
    path: str


@dataclass(frozen=True, slots=True)
class IrModuleInfo:
    id: str
    prefix: str
    namespace: str
    version: str
    doc: Doc
    imports: tuple[IrImport, ...]


@dataclass(frozen=True, slots=True)
class IrDatatype:
    name: str
    base: str
    facets: dict[str, JsonValue]
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrEnumValue:
    name: str
    summary: str | None


@dataclass(frozen=True, slots=True)
class IrEnum:
    name: str
    values: tuple[IrEnumValue, ...]
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrAttribute:
    name: str
    type: str | None  # exactly one of type and lifecycle is set
    lifecycle: str | None
    required: bool
    many: bool
    unique: bool
    default: YamlNode | None  # converted per resolved type by the compiler
    mutability: str
    role: str | None  # one of LABEL_ROLES
    renamed_from: str | None
    doc: Doc
    path: str
    # Marking references as written (a local name or a CURIE of a visible module); a caller
    # must hold every one of them to see the attribute. ``()`` = unmarked.
    markings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IrMarking:
    """A marking declared by a module: a label for sensitive attributes, granted by policy."""

    name: str
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrClass:
    name: str
    abstract: bool
    is_a: str | None
    display_prefix: str | None
    renamed_from: str | None
    attributes: dict[str, IrAttribute]
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrQualifier:
    name: str
    type: str
    required: bool
    many: bool
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class Bounds:
    min: int
    max: int | None  # None = unbounded


@dataclass(frozen=True, slots=True)
class IrRelation:
    name: str
    domain: tuple[str, ...]  # () = any class
    range: tuple[str, ...]
    out: Bounds
    into: Bounds
    characteristics: tuple[str, ...]
    on_target_inactive: str  # one of TARGET_INACTIVE_POLICIES
    inverse_label: str | None
    qualifiers: dict[str, IrQualifier]
    renamed_from: str | None
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrState:
    name: str
    summary: str | None
    initial: bool
    terminal: bool


@dataclass(frozen=True, slots=True)
class IrGuard:
    id: str
    require: tuple[Cond, ...]
    summary: str
    path: str


@dataclass(frozen=True, slots=True)
class IrTransition:
    from_states: tuple[str, ...]
    to: str
    summary: str | None
    guards: tuple[IrGuard, ...]
    path: str


@dataclass(frozen=True, slots=True)
class IrLifecycle:
    name: str
    states: dict[str, IrState]
    transitions: tuple[IrTransition, ...]
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrRule:
    id: str
    severity: str
    when: str
    scope: str
    if_: tuple[Cond, ...] | None
    then: tuple[Cond, ...]
    doc: Doc
    path: str


@dataclass(frozen=True, slots=True)
class IrModule:
    info: IrModuleInfo
    datatypes: dict[str, IrDatatype]
    enums: dict[str, IrEnum]
    classes: dict[str, IrClass]
    relations: dict[str, IrRelation]
    lifecycles: dict[str, IrLifecycle]
    rules: tuple[IrRule, ...]
    mappings: dict[str, dict[str, tuple[str, ...]]]
    extensions: dict[str, JsonValue]
    markings: dict[str, IrMarking] = field(default_factory=dict)


def parse_module(text: str | bytes, module_name: str, report: Report) -> IrModule | None:
    """Parse one authored module file. ``module_name`` is the file's module id.

    Returns ``None`` when the file cannot be read as an IR module at all.
    """
    try:
        root = load_strict(text)
    except YamlError as exc:
        report.add("model.structural.invalid_yaml", module_name, "", str(exc))
        return None
    reader = Reader(report, module_name)
    top = reader.mapping(root, "", _TOP_KEYS)
    if top is None:
        return None
    format_text = reader.string(top.get("format"), "format")
    if format_text != IR_FORMAT:
        report.add(
            "model.structural.unsupported_format",
            module_name,
            "format",
            f"expected format {IR_FORMAT!r}",
            expected=IR_FORMAT,
            actual=format_text,
        )
        return None
    info = _module_info(reader, top.get("module"), module_name)
    if info is None:
        return None
    return _Parser(reader).module(info, top)


def _doc(reader: Reader, data: dict[str, YamlNode], path: str) -> Doc:
    summary = reader.string(data.get("summary"), join(path, "summary"))
    if summary is None or not summary.strip():
        if "summary" not in data or summary is not None:
            reader.report.add(
                "model.structural.missing_summary", reader.module, path, "a summary is required"
            )
        summary = ""
    annotations = reader.entries(data.get("annotations"), join(path, "annotations")) or {}
    payload: dict[str, JsonValue] = {}
    for ns, value in annotations.items():
        if reader.check_name(ns, join(path, f"annotations.{ns}"), NAMESPACE_NS_RE):
            payload[ns] = reader.free_json(value, join(path, f"annotations.{ns}"))
    return Doc(
        summary=summary,
        description=reader.string(data.get("description"), join(path, "description")),
        deprecated=reader.boolean(data.get("deprecated"), join(path, "deprecated")),
        annotations=payload,
    )


def _module_info(reader: Reader, node: YamlNode | None, module_name: str) -> IrModuleInfo | None:
    data = reader.mapping(
        node, "module", ("id", "prefix", "namespace", "version", "imports", *_DOC)
    )
    if data is None:
        return None
    module_id = reader.name(data.get("id"), "module.id", MODULE_ID_RE)
    prefix = reader.name(data.get("prefix"), "module.prefix", PREFIX_RE)
    version = reader.string(data.get("version"), "module.version")
    if version is not None and not is_version(version):
        reader.invalid("module.version", f"{version!r} is not MAJOR.MINOR.PATCH")
        version = None
    if module_id is not None and module_id != module_name:
        reader.invalid("module.id", f"module id {module_id!r} does not match {module_name!r}")
        module_id = None
    if module_id in RESERVED_MODULE_IDS:
        reader.report.add(
            "model.structural.reserved_name",
            module_name,
            "module.id",
            f"{module_id!r} is a reserved module id",
        )
        module_id = None
    for key in ("id", "prefix", "version"):
        if key not in data:
            reader.invalid(f"module.{key}", f"module.{key} is required")
    imports: list[IrImport] = []
    for index, item in enumerate(reader.sequence(data.get("imports"), "module.imports") or ()):
        path = f"module.imports[{index}]"
        entry = reader.mapping(item, path, ("module", "version"))
        if entry is None:
            continue
        target = reader.name(entry.get("module"), join(path, "module"), MODULE_ID_RE)
        range_text = reader.string(entry.get("version"), join(path, "version"))
        if target is None or range_text is None:
            if "module" not in entry or "version" not in entry:
                reader.invalid(path, "an import needs module and version")
            continue
        try:
            imports.append(IrImport(target, VersionRange.parse(range_text), path))
        except ValueError as exc:
            reader.invalid(join(path, "version"), str(exc))
    doc = _doc(reader, data, "module")
    if module_id is None or prefix is None or version is None:
        return None
    namespace = reader.string(data.get("namespace"), "module.namespace")
    return IrModuleInfo(
        id=module_id,
        prefix=prefix,
        namespace=namespace or DEFAULT_NAMESPACE.format(module=module_id),
        version=version,
        doc=doc,
        imports=tuple(imports),
    )


class _Parser:
    def __init__(self, reader: Reader) -> None:
        self.r = reader

    def module(self, info: IrModuleInfo, top: dict[str, YamlNode]) -> IrModule:
        extensions: dict[str, JsonValue] = {}
        for ns, value in (self.r.entries(top.get("extensions"), "extensions") or {}).items():
            if self.r.check_name(ns, f"extensions.{ns}", NAMESPACE_NS_RE):
                extensions[ns] = self.r.free_json(value, f"extensions.{ns}")
        return IrModule(
            info=info,
            datatypes=self._terms(top, "datatypes", TYPE_NAME_RE, self.datatype),
            enums=self._terms(top, "enums", TYPE_NAME_RE, self.enum),
            classes=self._terms(top, "classes", TYPE_NAME_RE, self.class_),
            relations=self._terms(top, "relations", LOWER_NAME_RE, self.relation),
            lifecycles=self._terms(top, "lifecycles", TYPE_NAME_RE, self.lifecycle),
            rules=self.rules(top.get("rules")),
            mappings=self.mappings(top.get("mappings")),
            extensions=extensions,
            markings=self._terms(top, "markings", LOWER_NAME_RE, self.marking),
        )

    def marking(self, name: str, node: YamlNode, path: str) -> IrMarking | None:
        data = self.r.mapping(node, path, ("summary", "description"))
        if data is None:
            return None
        return IrMarking(name, _doc(self.r, data, path), path)

    def _terms[T](
        self,
        top: dict[str, YamlNode],
        section: str,
        grammar: re.Pattern[str],
        build: Callable[[str, YamlNode, str], T | None],
    ) -> dict[str, T]:
        result: dict[str, T] = {}
        for name, node in (self.r.entries(top.get(section), section) or {}).items():
            path = f"{section}.{name}"
            if not self.r.check_name(name, path, grammar):
                continue
            term = build(name, node, path)
            if term is not None:
                result[name] = term
        return result

    def datatype(self, name: str, node: YamlNode, path: str) -> IrDatatype | None:
        data = self.r.mapping(node, path, ("base", *sorted(ALL_FACETS), *_DOC))
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        base = self.r.string(data.get("base"), join(path, "base"))
        if base is None:
            if "base" not in data:
                self.r.invalid(join(path, "base"), "a datatype needs a base type")
            return None
        facets: dict[str, JsonValue] = {}
        for facet in sorted(ALL_FACETS):
            if facet not in data:
                continue
            facet_path = join(path, facet)
            value: JsonValue
            if facet == "pattern":
                value = self.r.string(data[facet], facet_path)
            elif facet == "single_line":
                value = self.r.boolean(data[facet], facet_path)
            elif facet in ("min", "max"):
                node_value = data[facet]
                value = node_value.as_int() if isinstance(node_value, YamlScalar) else None
                if value is None:
                    self.r.invalid(facet_path, "expected an integer")
            else:
                value = self.r.integer(data[facet], facet_path)
            if value is not None:
                facets[facet] = value
        return IrDatatype(name, base, facets, doc, path)

    def enum(self, name: str, node: YamlNode, path: str) -> IrEnum | None:
        data = self.r.mapping(node, path, ("values", *_DOC))
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        values: list[IrEnumValue] = []
        for value, value_node in (
            self.r.entries(data.get("values"), join(path, "values")) or {}
        ).items():
            value_path = join(path, f"values.{value}")
            if not self.r.check_name(value, value_path, LOWER_NAME_RE):
                continue
            entry = self.r.mapping(value_node, value_path, ("summary",))
            if entry is not None:
                values.append(
                    IrEnumValue(
                        value, self.r.string(entry.get("summary"), join(value_path, "summary"))
                    )
                )
        if not values:
            self.r.invalid(join(path, "values"), "an enum needs at least one value")
            return None
        return IrEnum(name, tuple(values), doc, path)

    def rename_source(
        self, node: YamlNode | None, path: str, grammar: re.Pattern[str]
    ) -> str | None:
        """``renamed_from`` of a class or relation: a local name (same module) or the CURIE
        ``prefix:Name`` of a term of the old model, kept verbatim."""
        text = self.r.string(node, path)
        if text is None:
            return None
        match = CURIE_RE.match(text)
        if grammar.match(text if match is None else match.group(2)):
            return text
        self.r.invalid(path, f"{text!r} is not a name ({grammar.pattern}) or a prefix:name CURIE")
        return None

    def class_(self, name: str, node: YamlNode, path: str) -> IrClass | None:
        data = self.r.mapping(
            node, path, ("abstract", "is_a", "display_prefix", "renamed_from", "attributes", *_DOC)
        )
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        attributes: dict[str, IrAttribute] = {}
        for attr_name, attr_node in (
            self.r.entries(data.get("attributes"), join(path, "attributes")) or {}
        ).items():
            attr_path = join(path, f"attributes.{attr_name}")
            if self.r.check_name(attr_name, attr_path, LOWER_NAME_RE):
                attribute = self.attribute(attr_name, attr_node, attr_path)
                if attribute is not None:
                    attributes[attr_name] = attribute
        return IrClass(
            name=name,
            abstract=self.r.boolean(data.get("abstract"), join(path, "abstract")),
            is_a=self.r.string(data.get("is_a"), join(path, "is_a")),
            display_prefix=self.r.name(
                data.get("display_prefix"), join(path, "display_prefix"), DISPLAY_PREFIX_RE
            ),
            renamed_from=self.rename_source(
                data.get("renamed_from"), join(path, "renamed_from"), TYPE_NAME_RE
            ),
            attributes=attributes,
            doc=doc,
            path=path,
        )

    def attribute(self, name: str, node: YamlNode, path: str) -> IrAttribute | None:
        data = self.r.mapping(
            node,
            path,
            (
                "type",
                "lifecycle",
                "required",
                "many",
                "unique",
                "default",
                "mutability",
                "role",
                "renamed_from",
                "markings",
                *_DOC,
            ),
        )
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        markings = self.markings(data, path)
        type_ref = self.r.string(data.get("type"), join(path, "type"))
        lifecycle = self.r.string(data.get("lifecycle"), join(path, "lifecycle"))
        if ("type" in data) == ("lifecycle" in data):
            self.r.invalid(path, "an attribute needs exactly one of type and lifecycle")
            return None
        if type_ref is None and lifecycle is None:
            return None
        mutability = self.r.string(data.get("mutability"), join(path, "mutability"))
        if mutability is not None and mutability not in MUTABILITY_CLASSES:
            self.r.report.add(
                "model.mutability.unknown_class",
                self.r.module,
                join(path, "mutability"),
                f"unknown mutability class {mutability!r}",
                expected=list(MUTABILITY_CLASSES),
                actual=mutability,
            )
            mutability = None
        if mutability is None:
            mutability = "lifecycle" if lifecycle is not None else "content"
        return IrAttribute(
            name=name,
            type=type_ref,
            lifecycle=lifecycle,
            required=self.r.boolean(data.get("required"), join(path, "required")),
            many=self.r.boolean(data.get("many"), join(path, "many")),
            unique=self.r.boolean(data.get("unique"), join(path, "unique")),
            default=data.get("default"),
            mutability=mutability,
            role=self.r.choice(data.get("role"), join(path, "role"), LABEL_ROLES),
            renamed_from=self.r.name(
                data.get("renamed_from"), join(path, "renamed_from"), LOWER_NAME_RE
            ),
            doc=doc,
            path=path,
            markings=markings,
        )

    def markings(self, data: dict[str, YamlNode], path: str) -> tuple[str, ...]:
        """``markings`` of an attribute: a non-empty list of marking references (a local name
        or a CURIE), each named once. Resolution is the validator's."""
        if "markings" not in data:
            return ()
        key_path = join(path, "markings")
        node = data["markings"]
        if not isinstance(node, YamlSeq):
            self.r.invalid(key_path, "expected a list of markings")
            return ()
        refs = self.r.names(node, key_path)
        if refs is None:
            return ()
        if not refs:
            self.r.invalid(key_path, "a markings list names at least one marking")
            return ()
        for index, ref in enumerate(refs):
            match = CURIE_RE.match(ref)
            if not LOWER_NAME_RE.match(ref if match is None else match.group(2)):
                self.r.invalid(
                    f"{key_path}[{index}]",
                    f"{ref!r} is not a marking name ({LOWER_NAME_RE.pattern}) or a prefix:name"
                    " CURIE",
                )
                return ()
            if ref in refs[:index]:
                self.r.invalid(f"{key_path}[{index}]", f"marking {ref!r} is named twice")
                return ()
        return refs

    def _bounds(self, node: YamlNode | None, path: str) -> Bounds:
        data = self.r.mapping(node, path, ("min", "max"))
        if not data:
            return Bounds(0, None)
        minimum = self.r.integer(data.get("min"), join(path, "min"))
        maximum = self.r.integer(data.get("max"), join(path, "max"), minimum=1)
        return Bounds(minimum or 0, maximum)

    def relation(self, name: str, node: YamlNode, path: str) -> IrRelation | None:
        data = self.r.mapping(
            node,
            path,
            (
                "domain",
                "range",
                "cardinality",
                "characteristics",
                "on_target_inactive",
                "inverse_label",
                "qualifiers",
                "renamed_from",
                *_DOC,
            ),
        )
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        cardinality = (
            self.r.mapping(data.get("cardinality"), join(path, "cardinality"), ("out", "in")) or {}
        )
        characteristics = self.r.names(data.get("characteristics"), join(path, "characteristics"))
        for index, item in enumerate(characteristics or ()):
            if item not in CHARACTERISTICS:
                self.r.invalid(
                    f"{path}.characteristics[{index}]",
                    f"{item!r} is not one of {', '.join(CHARACTERISTICS)}",
                )
                characteristics = None
                break
        on_target_inactive = (
            self.r.choice(
                data["on_target_inactive"],
                join(path, "on_target_inactive"),
                TARGET_INACTIVE_POLICIES,
            )
            if "on_target_inactive" in data
            else TARGET_INACTIVE_POLICIES[0]
        )
        qualifiers: dict[str, IrQualifier] = {}
        for q_name, q_node in (
            self.r.entries(data.get("qualifiers"), join(path, "qualifiers")) or {}
        ).items():
            q_path = join(path, f"qualifiers.{q_name}")
            if not self.r.check_name(q_name, q_path, LOWER_NAME_RE):
                continue
            q_data = self.r.mapping(q_node, q_path, ("type", "required", "many", *_DOC))
            if q_data is None:
                continue
            q_doc = _doc(self.r, q_data, q_path)
            q_type = self.r.string(q_data.get("type"), join(q_path, "type"))
            if q_type is None:
                if "type" not in q_data:
                    self.r.invalid(join(q_path, "type"), "a qualifier needs a type")
                continue
            qualifiers[q_name] = IrQualifier(
                q_name,
                q_type,
                self.r.boolean(q_data.get("required"), join(q_path, "required")),
                self.r.boolean(q_data.get("many"), join(q_path, "many")),
                q_doc,
                q_path,
            )
        domain = self.r.names(data.get("domain"), join(path, "domain"))
        range_ = self.r.names(data.get("range"), join(path, "range"))
        if (
            domain is None
            or range_ is None
            or characteristics is None
            or on_target_inactive is None
        ):
            return None
        return IrRelation(
            name=name,
            domain=domain,
            range=range_,
            out=self._bounds(cardinality.get("out"), join(path, "cardinality.out")),
            into=self._bounds(cardinality.get("in"), join(path, "cardinality.in")),
            characteristics=tuple(sorted(set(characteristics))),
            on_target_inactive=on_target_inactive,
            inverse_label=self.r.name(
                data.get("inverse_label"), join(path, "inverse_label"), LOWER_NAME_RE
            ),
            qualifiers=qualifiers,
            renamed_from=self.rename_source(
                data.get("renamed_from"), join(path, "renamed_from"), LOWER_NAME_RE
            ),
            doc=doc,
            path=path,
        )

    def lifecycle(self, name: str, node: YamlNode, path: str) -> IrLifecycle | None:
        data = self.r.mapping(node, path, ("states", "transitions", *_DOC))
        if data is None:
            return None
        doc = _doc(self.r, data, path)
        states: dict[str, IrState] = {}
        for state, state_node in (
            self.r.entries(data.get("states"), join(path, "states")) or {}
        ).items():
            state_path = join(path, f"states.{state}")
            if not self.r.check_name(state, state_path, LOWER_NAME_RE):
                continue
            entry = self.r.mapping(state_node, state_path, ("summary", "initial", "terminal"))
            if entry is None:
                continue
            states[state] = IrState(
                state,
                self.r.string(entry.get("summary"), join(state_path, "summary")),
                self.r.boolean(entry.get("initial"), join(state_path, "initial")),
                self.r.boolean(entry.get("terminal"), join(state_path, "terminal")),
            )
        if not states:
            self.r.invalid(join(path, "states"), "a lifecycle needs at least one state")
            return None
        transitions: list[IrTransition] = []
        for index, item in enumerate(
            self.r.sequence(data.get("transitions"), join(path, "transitions")) or ()
        ):
            t_path = f"{path}.transitions[{index}]"
            t_data = self.r.mapping(item, t_path, ("from", "to", "summary", "guards"))
            if t_data is None:
                continue
            from_states = self.r.names(t_data.get("from"), join(t_path, "from"))
            to = self.r.string(t_data.get("to"), join(t_path, "to"))
            if not from_states or to is None:
                if "from" not in t_data or "to" not in t_data:
                    self.r.invalid(t_path, "a transition needs from and to")
                continue
            guards: list[IrGuard] = []
            for g_index, g_node in enumerate(
                self.r.sequence(t_data.get("guards"), join(t_path, "guards")) or ()
            ):
                g_path = f"{t_path}.guards[{g_index}]"
                g_data = self.r.mapping(g_node, g_path, ("id", "summary", "require"))
                if g_data is None:
                    continue
                g_doc = _doc(self.r, g_data, g_path)
                g_id = self.r.name(g_data.get("id"), join(g_path, "id"), LOWER_NAME_RE)
                require = parse_conditions(self.r, g_data.get("require"), join(g_path, "require"))
                if g_id is None or require is None:
                    if "id" not in g_data:
                        self.r.invalid(join(g_path, "id"), "a guard needs an id")
                    continue
                guards.append(IrGuard(g_id, require, g_doc.summary, g_path))
            transitions.append(
                IrTransition(
                    from_states,
                    to,
                    self.r.string(t_data.get("summary"), join(t_path, "summary")),
                    tuple(guards),
                    t_path,
                )
            )
        return IrLifecycle(name, states, tuple(transitions), doc, path)

    def rules(self, node: YamlNode | None) -> tuple[IrRule, ...]:
        result: list[IrRule] = []
        for index, item in enumerate(self.r.sequence(node, "rules") or ()):
            base = f"rules[{index}]"
            data = self.r.mapping(
                item, base, ("id", "severity", "when", "scope", "if", "then", *_DOC)
            )
            if data is None:
                continue
            rule_id = self.r.name(data.get("id"), join(base, "id"), LOWER_NAME_RE)
            path = f"rules.{rule_id}" if rule_id else base
            doc = _doc(self.r, data, path)
            severity = self.r.choice(data.get("severity"), join(path, "severity"), SEVERITIES)
            when = self.r.choice(data.get("when"), join(path, "when"), RULE_TIMINGS)
            scope = self.r.string(data.get("scope"), join(path, "scope"))
            if_ = parse_conditions(self.r, data["if"], join(path, "if")) if "if" in data else None
            then = parse_conditions(self.r, data.get("then"), join(path, "then"))
            missing = [k for k in ("id", "severity", "when", "scope", "then") if k not in data]
            if missing:
                self.r.invalid(path, f"a rule needs {', '.join(missing)}")
                continue
            if None in (rule_id, severity, when, scope, then) or ("if" in data and if_ is None):
                continue
            assert rule_id and severity and when and scope and then
            result.append(IrRule(rule_id, severity, when, scope, if_, then, doc, path))
        return tuple(result)

    def mappings(self, node: YamlNode | None) -> dict[str, dict[str, tuple[str, ...]]]:
        result: dict[str, dict[str, tuple[str, ...]]] = {}
        for term, entry in (self.r.entries(node, "mappings") or {}).items():
            path = f"mappings.{term}"
            data = self.r.mapping(entry, path, MAPPING_KINDS)
            if data is None:
                continue
            kinds: dict[str, tuple[str, ...]] = {}
            for kind, values in data.items():
                names = self.r.names(values, join(path, kind))
                if names is not None:
                    kinds[kind] = names
            result[term] = kinds
        return result
