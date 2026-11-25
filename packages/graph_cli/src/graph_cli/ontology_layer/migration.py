"""The migration file ``graph_cli.migration/1`` of a version directory.

A migration accompanies a major release and says what the ontology cannot: how stored values
and shapes change. Renames of classes, relations and attributes come from ``renamed_from``
(``diff.derive_renames``), not from this file; the steps apply after them.

Two phases, both returning problems as data (``path`` and ``message``) instead of raising:

- ``parse_migration`` checks the shape of the file: format, ``from``, ``summary``, ``steps``
  and the keys and grammar of every step;
- ``check_migration`` checks every step against the old (pinned) and the new (target) model.
  Each key says which side it names: types and relations of ``retype.from``, ``drop`` (attribute),
  ``drop_edges``, ``drop_qualifier`` (qualifier), ``rename_qualifier.from`` and the keys of
  ``map_values`` and ``map_qualifier_values`` belong to the old model, everything else to the new.

Types and relations are CURIEs, attributes and qualifiers local names, values plain strings.
``core:`` and built-in module terms are never migrated. ``migration_hash`` is the hash of the
normalized document, not of the file bytes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Final

from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.yaml_strict import (
    YamlError,
    YamlMap,
    YamlNode,
    YamlScalar,
    YamlSeq,
    load_strict,
)
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.model import AttributeDef, ClassDef, CompiledModel, RelationDef
from graph_cli.ontology_layer.semver import is_version
from graph_cli.ontology_layer.values import check_facets, coerce_text
from graph_cli.ontology_layer.vocabulary import (
    BUILTIN_MODULES,
    CURIE_RE,
    LANG_BASES,
    LOWER_NAME_RE,
    MIGRATION_FORMAT,
    TYPE_NAME_RE,
)

CONVERSIONS: Final = ("lang_string", "lang_text", "many", "single")
# The base type a conversion to a language-tagged type starts from.
_LANG_SOURCE: Final = {"lang_string": "string", "lang_text": "text"}


@dataclass(frozen=True, slots=True)
class MigrationProblem:
    path: str
    message: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"path": self.path, "message": self.message}


# ---- steps ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retype:
    """Every entity of exactly class ``from_`` (old) gets class ``to`` (new); ``attributes``
    renames stored attributes ``{old: new}``."""

    NAME: ClassVar[str] = "retype"
    from_: str
    to: str
    attributes: Mapping[str, str]

    def to_json(self) -> JsonValue:
        return {self.NAME: {"from": self.from_, "to": self.to, "attributes": dict(self.attributes)}}


@dataclass(frozen=True, slots=True)
class Fill:
    """Set ``value`` where the attribute (new) is absent, on ``type`` (new) and subclasses."""

    NAME: ClassVar[str] = "fill"
    type: str
    attribute: str
    value: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"type": self.type, "attribute": self.attribute, "value": self.value}}


@dataclass(frozen=True, slots=True)
class MapValues:
    """Rewrite enum values or lifecycle states ``{old: new}`` of an attribute (new)."""

    NAME: ClassVar[str] = "map_values"
    type: str
    attribute: str
    values: Mapping[str, str]

    def to_json(self) -> JsonValue:
        return {
            self.NAME: {
                "type": self.type,
                "attribute": self.attribute,
                "values": dict(self.values),
            }
        }


@dataclass(frozen=True, slots=True)
class MapQualifierValues:
    """The same for an enum qualifier (new) of a relation (new)."""

    NAME: ClassVar[str] = "map_qualifier_values"
    relation: str
    qualifier: str
    values: Mapping[str, str]

    def to_json(self) -> JsonValue:
        return {
            self.NAME: {
                "relation": self.relation,
                "qualifier": self.qualifier,
                "values": dict(self.values),
            }
        }


@dataclass(frozen=True, slots=True)
class Drop:
    """Remove the stored values of ``attribute`` (old name) on ``type`` (new)."""

    NAME: ClassVar[str] = "drop"
    type: str
    attribute: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"type": self.type, "attribute": self.attribute}}


@dataclass(frozen=True, slots=True)
class DropEdges:
    """Remove every edge of ``relation`` (old)."""

    NAME: ClassVar[str] = "drop_edges"
    relation: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"relation": self.relation}}


@dataclass(frozen=True, slots=True)
class RenameQualifier:
    """Rename a qualifier ``from_`` (old name) to ``to`` (new) on ``relation`` (new)."""

    NAME: ClassVar[str] = "rename_qualifier"
    relation: str
    from_: str
    to: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"relation": self.relation, "from": self.from_, "to": self.to}}


@dataclass(frozen=True, slots=True)
class DropQualifier:
    """Remove ``qualifier`` (old name) from the edges of ``relation`` (new)."""

    NAME: ClassVar[str] = "drop_qualifier"
    relation: str
    qualifier: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"relation": self.relation, "qualifier": self.qualifier}}


@dataclass(frozen=True, slots=True)
class Convert:
    """Convert the stored values of an attribute (new) by a fixed rule: ``lang_string`` and
    ``lang_text`` tag plain text with the graph language, ``many`` wraps a value in a list,
    ``single`` unwraps a list of at most one value."""

    NAME: ClassVar[str] = "convert"
    type: str
    attribute: str
    to: str

    def to_json(self) -> JsonValue:
        return {self.NAME: {"type": self.type, "attribute": self.attribute, "to": self.to}}


Step = (
    Retype
    | Fill
    | MapValues
    | MapQualifierValues
    | Drop
    | DropEdges
    | RenameQualifier
    | DropQualifier
    | Convert
)


@dataclass(frozen=True, slots=True)
class MigrationDoc:
    from_version: str
    summary: str
    steps: tuple[Step, ...]

    def to_json(self) -> dict[str, JsonValue]:
        """The normalized document: what ``migration_hash`` covers and a tx records."""
        return {
            "format": MIGRATION_FORMAT,
            "from": self.from_version,
            "summary": self.summary,
            "steps": [step.to_json() for step in self.steps],
        }


def class_moves(renames: Renames, retypes: Sequence[Retype]) -> dict[str, str]:
    """Old class CURIE to new across one release: the derived class renames and the
    ``retype`` steps. The one definition of where the entities of an old class went, for the
    policy guard (``admin.policy_renamed_term``), read-time mapping and its hints."""
    return {**renames.classes, **{r.from_: r.to for r in retypes}}


def migration_hash(doc: MigrationDoc) -> str:
    """``sha256:<hex>`` over the normalized document (``to_json``), not over the file bytes."""
    return hash_value(doc.to_json())


@dataclass(frozen=True, slots=True)
class ParsedMigration:
    """``doc`` is ``None`` when ``problems`` is not empty."""

    doc: MigrationDoc | None
    problems: tuple[MigrationProblem, ...]


# ---- parsing -------------------------------------------------------------------------------

_TOP_KEYS: Final = ("format", "from", "summary", "steps")


class _Problems:
    def __init__(self) -> None:
        self.items: list[MigrationProblem] = []

    def add(self, path: str, message: str) -> None:
        self.items.append(MigrationProblem(path, message))


class _Fields:
    """The keys of one mapping: unknown and missing keys are reported on creation, every
    getter reports what does not fit and returns an empty value, so one run finds all."""

    def __init__(
        self,
        problems: _Problems,
        node: YamlNode | None,
        path: str,
        required: tuple[str, ...],
        optional: tuple[str, ...] = (),
    ) -> None:
        self.problems = problems
        self.path = path
        self.failed = False
        self.items: dict[str, YamlNode] = {}
        if node is None or (isinstance(node, YamlScalar) and node.is_null()):
            node = YamlMap({}, {}, 0)
        if not isinstance(node, YamlMap):
            self.fail(path, "expected a mapping")
            return
        allowed = (*required, *optional)
        for key, value in node.items.items():
            if key in allowed:
                self.items[key] = value
            else:
                self.fail(_join(path, key), f"unknown key {key!r}; expected {', '.join(allowed)}")
        for key in required:
            if key not in node.items:
                self.fail(path, f"{key!r} is required")

    def fail(self, path: str, message: str) -> None:
        self.failed = True
        self.problems.add(path, message)

    def key_path(self, key: str) -> str:
        return _join(self.path, key)

    def text(self, key: str, grammar: re.Pattern[str] | None = None, what: str = "") -> str:
        node = self.items.get(key)
        if node is None:
            return ""
        text = _scalar_text(node)
        if text is None:
            self.fail(self.key_path(key), "expected a string")
            return ""
        if grammar is not None and not grammar.match(text):
            self.fail(self.key_path(key), f"{text!r} is not {what or grammar.pattern}")
            return ""
        return text

    def name(self, key: str) -> str:
        return self.text(key, LOWER_NAME_RE, "a local name (lowercase letters, digits, _)")

    def curie(self, key: str, kind: str) -> str:
        """A class or relation CURIE; a bare name is refused."""
        name_grammar = TYPE_NAME_RE if kind == "class" else LOWER_NAME_RE
        text = self.text(key)
        if not text:
            return ""
        match = CURIE_RE.match(text)
        if match is None or not name_grammar.match(match.group(2)):
            self.fail(self.key_path(key), f"{text!r} is not a {kind} CURIE (write prefix:name)")
            return ""
        return text

    def choice(self, key: str, options: tuple[str, ...]) -> str:
        text = self.text(key)
        if text and text not in options:
            self.fail(self.key_path(key), f"{text!r} is not one of {', '.join(options)}")
            return ""
        return text

    def text_map(self, key: str, *, names: bool = False) -> Mapping[str, str]:
        """``{old: new}`` of strings; ``names`` requires local names on both sides. An empty
        map is refused when the key is required (the step would do nothing)."""
        node = self.items.get(key)
        if node is None:
            return MappingProxyType({})
        path = self.key_path(key)
        if isinstance(node, YamlScalar) and node.is_null():
            node = YamlMap({}, {}, 0)
        if not isinstance(node, YamlMap):
            self.fail(path, "expected a mapping")
            return MappingProxyType({})
        result: dict[str, str] = {}
        for old, item in node.items.items():
            text = _scalar_text(item)
            if text is None:
                self.fail(f"{path}.{old}", "expected a string")
                continue
            if names:
                for label, word in (("name", old), ("value", text)):
                    if not LOWER_NAME_RE.match(word):
                        self.fail(f"{path}.{old}", f"{label} {word!r} is not a local name")
            result[old] = text
        return MappingProxyType(result)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _scalar_text(node: YamlNode) -> str | None:
    if not isinstance(node, YamlScalar) or node.is_null():
        return None
    return node.text


def parse_migration(data: bytes | str) -> ParsedMigration:
    """Check the shape of a migration file (``graph_cli.migration/1``).

    The terms the steps name are not looked up here (``check_migration``). A ``steps`` key
    that is absent or empty is a migration without steps (renames only)."""
    problems = _Problems()
    try:
        root = load_strict(data)
    except YamlError as exc:
        problems.add("", f"not strict YAML: {exc}")
        return ParsedMigration(None, tuple(problems.items))
    if not isinstance(root, YamlMap):
        problems.add("", "expected a mapping with format, from, summary and steps")
        return ParsedMigration(None, tuple(problems.items))
    fields = _Fields(problems, root, "", ("format", "from", "summary"), ("steps",))
    if "format" in fields.items and _scalar_text(fields.items["format"]) != MIGRATION_FORMAT:
        fields.fail("format", f"expected {MIGRATION_FORMAT}")
    from_version = fields.text("from")
    if from_version and not is_version(from_version):
        fields.fail("from", f"{from_version!r} is not MAJOR.MINOR.PATCH")
    summary = fields.text("summary")
    if "summary" in fields.items and not summary.strip():
        fields.fail("summary", "a summary is required")
    steps = _steps(problems, fields.items.get("steps"))
    if problems.items:
        return ParsedMigration(None, tuple(problems.items))
    return ParsedMigration(MigrationDoc(from_version, summary, steps), ())


def _steps(problems: _Problems, node: YamlNode | None) -> tuple[Step, ...]:
    if node is None or (isinstance(node, YamlScalar) and node.is_null()):
        return ()
    if not isinstance(node, YamlSeq):
        problems.add("steps", "expected a list")
        return ()
    steps: list[Step] = []
    for index, item in enumerate(node.items):
        step = _step(problems, item, f"steps[{index}]")
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _step(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    if not isinstance(node, YamlMap) or len(node.items) != 1:
        problems.add(path, f"a step is a mapping with one key: {', '.join(_BUILDERS)}")
        return None
    name, body = next(iter(node.items.items()))
    builder = _BUILDERS.get(name)
    if builder is None:
        problems.add(f"{path}.{name}", f"unknown step {name!r}; expected {', '.join(_BUILDERS)}")
        return None
    return builder(problems, body, f"{path}.{name}")


def _retype(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("from", "to"), ("attributes",))
    step = Retype(
        f.curie("from", "class"), f.curie("to", "class"), f.text_map("attributes", names=True)
    )
    return None if f.failed else step


def _fill(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("type", "attribute", "value"))
    step = Fill(f.curie("type", "class"), f.name("attribute"), f.text("value"))
    return None if f.failed else step


def _map_values(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("type", "attribute", "values"))
    step = MapValues(f.curie("type", "class"), f.name("attribute"), f.text_map("values"))
    if not f.failed and not step.values:
        f.fail(f.key_path("values"), "values must not be empty")
    return None if f.failed else step


def _map_qualifier_values(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("relation", "qualifier", "values"))
    step = MapQualifierValues(
        f.curie("relation", "relation"), f.name("qualifier"), f.text_map("values")
    )
    if not f.failed and not step.values:
        f.fail(f.key_path("values"), "values must not be empty")
    return None if f.failed else step


def _drop(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("type", "attribute"))
    step = Drop(f.curie("type", "class"), f.name("attribute"))
    return None if f.failed else step


def _drop_edges(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("relation",))
    step = DropEdges(f.curie("relation", "relation"))
    return None if f.failed else step


def _rename_qualifier(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("relation", "from", "to"))
    step = RenameQualifier(f.curie("relation", "relation"), f.name("from"), f.name("to"))
    return None if f.failed else step


def _drop_qualifier(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("relation", "qualifier"))
    step = DropQualifier(f.curie("relation", "relation"), f.name("qualifier"))
    return None if f.failed else step


def _convert(problems: _Problems, node: YamlNode, path: str) -> Step | None:
    f = _Fields(problems, node, path, ("type", "attribute", "to"))
    step = Convert(f.curie("type", "class"), f.name("attribute"), f.choice("to", CONVERSIONS))
    return None if f.failed else step


_BUILDERS: Final[dict[str, Callable[[_Problems, YamlNode, str], Step | None]]] = {
    "retype": _retype,
    "fill": _fill,
    "map_values": _map_values,
    "map_qualifier_values": _map_qualifier_values,
    "drop": _drop,
    "drop_edges": _drop_edges,
    "rename_qualifier": _rename_qualifier,
    "drop_qualifier": _drop_qualifier,
    "convert": _convert,
}
STEP_NAMES: Final = tuple(_BUILDERS)  # the step kinds of migration.yaml, in documentation order


# ---- checking against the models ---------------------------------------------------------


def check_migration(
    doc: MigrationDoc,
    old: CompiledModel,
    new: CompiledModel,
    renames: Renames,
    *,
    language: str | None,
) -> tuple[MigrationProblem, ...]:
    """Every step against the old and the new model; ``renames`` are those of
    ``diff.derive_renames(old, new)``, ``language`` is the graph language (``None`` = none).

    ``fill`` values are checked with the text conversion and facets of the new attribute's
    type (the table of ``attrs_text``); a value of a type that has no text form is refused."""
    checker = _Checker(doc, old, new, renames, language)
    for index, step in enumerate(doc.steps):
        checker.check(step, f"steps[{index}].{step.NAME}")
    return tuple(checker.problems)


class _Checker:
    def __init__(
        self,
        doc: MigrationDoc,
        old: CompiledModel,
        new: CompiledModel,
        renames: Renames,
        language: str | None,
    ) -> None:
        self.doc = doc
        self.old = old
        self.new = new
        self.renames = renames
        self.language = language
        self.problems: list[MigrationProblem] = []
        self.builtin = BUILTIN_MODULES | {
            m.prefix for m in (*old.modules, *new.modules) if m.id in BUILTIN_MODULES
        }
        self.class_sources = {n: o for o, n in renames.classes.items()}
        self.relation_sources = {n: o for o, n in renames.relations.items()}
        self.attribute_sources = {(cls, n): o for (cls, o), n in renames.attributes.items()}

    def add(self, path: str, message: str) -> None:
        self.problems.append(MigrationProblem(path, message))

    def check(self, step: Step, path: str) -> None:
        match step:
            case Retype():
                self.retype(step, path)
            case Fill():
                self.fill(step, path)
            case MapValues():
                self.map_values(step, path)
            case MapQualifierValues():
                self.map_qualifier_values(step, path)
            case Drop():
                self.drop(step, path)
            case DropEdges():
                self.drop_edges(step, path)
            case RenameQualifier():
                self.rename_qualifier(step, path)
            case DropQualifier():
                self.drop_qualifier(step, path)
            case Convert():
                self.convert(step, path)

    # ---- terms ---------------------------------------------------------------------------

    def term[T](
        self, curie: str, terms: Mapping[str, T], side: str, kind: str, path: str
    ) -> T | None:
        """The term, or ``None`` after a problem: built-in terms are never migrated and the
        term must exist in the model of its side."""
        if curie.partition(":")[0] in self.builtin:
            self.add(
                path, f"{curie} is a built-in term; core and standard module terms are not migrated"
            )
            return None
        found = terms.get(curie)
        if found is None:
            self.add(path, f"no {kind} {curie} in the {side} model")
        return found

    def new_class(self, curie: str, path: str) -> ClassDef | None:
        return self.term(curie, self.new.classes, "new", "class", path)

    def new_attribute(
        self, cls: ClassDef | None, attribute: str, curie: str, path: str
    ) -> AttributeDef | None:
        if cls is None:
            return None
        found = cls.attributes.get(attribute)
        if found is None:
            self.add(path, f"{curie} has no attribute {attribute!r} in the new model")
        return found

    def new_relation(self, curie: str, path: str) -> RelationDef | None:
        return self.term(curie, self.new.relations, "new", "relation", path)

    def old_classes_of(self, new_class: str) -> list[str]:
        """The old classes whose entities become ``new_class``: its rename source, itself and
        the sources of retype steps that target it."""
        candidates = [self.class_sources.get(new_class), new_class]
        candidates += [
            s.from_ for s in self.doc.steps if isinstance(s, Retype) and s.to == new_class
        ]
        return list(dict.fromkeys(c for c in candidates if c is not None and c in self.old.classes))

    def old_attribute(self, new_class: str, attribute: str) -> AttributeDef | None:
        """The old attribute that became ``attribute`` of ``new_class`` (a rename, a retype
        attribute map or the same name), looked up in the old counterpart classes."""
        declared = self.new.classes[new_class].attributes[attribute].declared_in
        for old_class in self.old_classes_of(new_class):
            names = [
                o
                for s in self.doc.steps
                if isinstance(s, Retype) and (s.from_, s.to) == (old_class, new_class)
                for o, n in s.attributes.items()
                if n == attribute
            ]
            renamed = self.attribute_sources.get((declared, attribute))
            for name in (*names, *([renamed] if renamed else []), attribute):
                found = self.old.classes[old_class].attributes.get(name)
                if found is not None:
                    return found
        return None

    def old_relation_of(self, new_relation: str) -> RelationDef | None:
        source = self.relation_sources.get(new_relation, new_relation)
        return self.old.relations.get(source)

    # ---- steps ---------------------------------------------------------------------------

    def retype(self, step: Retype, path: str) -> None:
        source = self.term(step.from_, self.old.classes, "old", "class", f"{path}.from")
        target = self.new_class(step.to, f"{path}.to")
        if source is not None and step.from_ in self.renames.classes:
            self.add(
                f"{path}.from",
                f"{step.from_} is renamed to {self.renames.classes[step.from_]} by renamed_from;"
                " its entities move with the rename",
            )
        if target is not None and target.abstract:
            self.add(f"{path}.to", f"{step.to} is abstract and has no entities of its own")
        for old_name, new_name in step.attributes.items():
            if source is not None and old_name not in source.attributes:
                self.add(
                    f"{path}.attributes.{old_name}",
                    f"{step.from_} has no attribute {old_name!r} in the old model",
                )
            if target is not None and new_name not in target.attributes:
                self.add(
                    f"{path}.attributes.{old_name}",
                    f"{step.to} has no attribute {new_name!r} in the new model",
                )

    def fill(self, step: Fill, path: str) -> None:
        cls = self.new_class(step.type, f"{path}.type")
        attribute = self.new_attribute(cls, step.attribute, step.type, f"{path}.attribute")
        if attribute is None:
            return
        problem = self.value_problem(attribute, step.value)
        if problem is not None:
            self.add(f"{path}.value", problem)

    def value_problem(self, attribute: AttributeDef, text: str) -> str | None:
        kind = attribute.kind
        if kind in ("enum", "lifecycle"):
            names = self.value_names(attribute)
            return None if text in names else f"{text!r} is not one of {', '.join(names)}"
        base = self.new.value_base(kind, attribute.type)
        try:
            value = coerce_text(base, text)
        except ValueError as exc:
            return str(exc)
        if base in LANG_BASES and isinstance(value, str) and self.language is None:
            return (
                f'a {base} value needs a language tag ("<text>"@<tag>): the graph has no language'
            )
        violation = check_facets(value, self.new.facets(kind, attribute.type))
        return None if violation is None else f"{text!r} is {violation}"

    def value_names(self, attribute: AttributeDef) -> tuple[str, ...]:
        if attribute.kind == "enum":
            return self.new.enums[attribute.type].value_names()
        return self.new.lifecycles[attribute.type].state_names()

    def map_values(self, step: MapValues, path: str) -> None:
        cls = self.new_class(step.type, f"{path}.type")
        attribute = self.new_attribute(cls, step.attribute, step.type, f"{path}.attribute")
        if attribute is None:
            return
        if attribute.kind not in ("enum", "lifecycle"):
            self.add(
                f"{path}.attribute", f"{step.attribute!r} is not an enum or lifecycle attribute"
            )
            return
        targets = self.value_names(attribute)
        old = self.old_attribute(step.type, step.attribute)
        sources = self.old_value_names(old)
        for old_value, new_value in step.values.items():
            if new_value not in targets:
                self.add(
                    f"{path}.values.{old_value}",
                    f"{new_value!r} is not one of {', '.join(targets)} in the new model",
                )
            if sources is not None and old_value not in sources:
                self.add(
                    f"{path}.values.{old_value}",
                    f"{old_value!r} is not one of {', '.join(sources)} in the old model",
                )

    def old_value_names(self, attribute: AttributeDef | None) -> tuple[str, ...] | None:
        """The enum values or states of an old attribute; ``None`` when it has none (new, or
        another type: any old text is then possible)."""
        if attribute is None:
            return None
        if attribute.kind == "enum":
            return self.old.enums[attribute.type].value_names()
        if attribute.kind == "lifecycle":
            return self.old.lifecycles[attribute.type].state_names()
        return None

    def map_qualifier_values(self, step: MapQualifierValues, path: str) -> None:
        relation = self.new_relation(step.relation, f"{path}.relation")
        if relation is None:
            return
        qualifier = relation.qualifiers.get(step.qualifier)
        if qualifier is None:
            self.add(
                f"{path}.qualifier",
                f"{step.relation} has no qualifier {step.qualifier!r} in the new model",
            )
            return
        if qualifier.kind != "enum":
            self.add(f"{path}.qualifier", f"{step.qualifier!r} is not an enum qualifier")
            return
        targets = self.new.enums[qualifier.type].value_names()
        sources: tuple[str, ...] | None = None
        old_relation = self.old_relation_of(step.relation)
        if old_relation is not None:
            name = next(
                (
                    s.from_
                    for s in self.doc.steps
                    if isinstance(s, RenameQualifier)
                    and (s.relation, s.to) == (step.relation, step.qualifier)
                ),
                step.qualifier,
            )
            old_qualifier = old_relation.qualifiers.get(name)
            if old_qualifier is not None and old_qualifier.kind == "enum":
                sources = self.old.enums[old_qualifier.type].value_names()
        for old_value, new_value in step.values.items():
            if new_value not in targets:
                self.add(
                    f"{path}.values.{old_value}",
                    f"{new_value!r} is not one of {', '.join(targets)} in the new model",
                )
            if sources is not None and old_value not in sources:
                self.add(
                    f"{path}.values.{old_value}",
                    f"{old_value!r} is not one of {', '.join(sources)} in the old model",
                )

    def drop(self, step: Drop, path: str) -> None:
        if self.new_class(step.type, f"{path}.type") is None:
            return
        old_classes = self.old_classes_of(step.type)
        if not old_classes:
            self.add(f"{path}.type", f"{step.type} has no counterpart in the old model")
            return
        if not any(step.attribute in self.old.classes[c].attributes for c in old_classes):
            self.add(
                f"{path}.attribute",
                f"no attribute {step.attribute!r} on {', '.join(old_classes)} in the old model",
            )
            return
        renamed = any(
            old_name == step.attribute and self.new.is_a(step.type, owner)
            for (owner, old_name) in self.renames.attributes
        ) or any(
            isinstance(s, Retype) and s.to == step.type and step.attribute in s.attributes
            for s in self.doc.steps
        )
        if renamed:
            self.add(
                f"{path}.attribute",
                f"{step.attribute!r} is renamed; the rename moves its values, drop the new name"
                " instead",
            )

    def drop_edges(self, step: DropEdges, path: str) -> None:
        found = self.term(step.relation, self.old.relations, "old", "relation", f"{path}.relation")
        if found is not None and step.relation in self.renames.relations:
            self.add(
                f"{path}.relation",
                f"{step.relation} is renamed to {self.renames.relations[step.relation]} by"
                " renamed_from; its edges move with the rename",
            )

    def rename_qualifier(self, step: RenameQualifier, path: str) -> None:
        relation = self.new_relation(step.relation, f"{path}.relation")
        if relation is None:
            return
        if step.to not in relation.qualifiers:
            self.add(f"{path}.to", f"{step.relation} has no qualifier {step.to!r} in the new model")
        self.old_qualifier(step.relation, step.from_, f"{path}.from")

    def drop_qualifier(self, step: DropQualifier, path: str) -> None:
        if self.new_relation(step.relation, f"{path}.relation") is None:
            return
        self.old_qualifier(step.relation, step.qualifier, f"{path}.qualifier")

    def old_qualifier(self, new_relation: str, name: str, path: str) -> None:
        old_relation = self.old_relation_of(new_relation)
        if old_relation is None:
            self.add(path, f"{new_relation} has no counterpart in the old model")
        elif name not in old_relation.qualifiers:
            self.add(path, f"the old {new_relation} has no qualifier {name!r}")

    def convert(self, step: Convert, path: str) -> None:
        cls = self.new_class(step.type, f"{path}.type")
        attribute = self.new_attribute(cls, step.attribute, step.type, f"{path}.attribute")
        if attribute is None:
            return
        old = self.old_attribute(step.type, step.attribute)
        if old is None:
            self.add(f"{path}.attribute", f"{step.attribute!r} has no counterpart in the old model")
            return
        old_base = self.old.value_base(old.kind, old.type)
        new_base = self.new.value_base(attribute.kind, attribute.type)
        if step.to in LANG_BASES:
            if self.language is None:
                self.add(
                    f"{path}.to", f"convert to {step.to} needs a graph language; the graph has none"
                )
            if (old_base, new_base) != (_LANG_SOURCE[step.to], step.to):
                self.add(
                    f"{path}.to",
                    f"{step.to} converts {_LANG_SOURCE[step.to]} to {step.to}; the attribute goes"
                    f" from {old_base} to {new_base}",
                )
            return
        wanted = (step.to == "many", step.to == "single")
        if (old.many, attribute.many) != (wanted[1], wanted[0]):
            self.add(
                f"{path}.to",
                f"{step.to} needs the attribute to go from {'many' if wanted[1] else 'single'}"
                f" to {'many' if wanted[0] else 'single'}",
            )
        elif old_base != new_base:
            self.add(
                f"{path}.to", f"{step.to} keeps the type, but it goes from {old_base} to {new_base}"
            )
