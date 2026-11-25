"""The types of the result columns of a function (tool manifest plan TM6): what the query makes
certain, what an author declares and how the two meet.

Every rule of the inference has a test that fails when the rule is broken (checked by mutating
``kinetic.return_types`` and watching the test fail); the tests read the types through
``compile.column_types``, which is what the manifest and ``describe function`` use.
"""

import pytest

from graph_cli.kinetic.compile import column_types, compile_catalog
from graph_cli.kinetic.format import load_function
from graph_cli.kinetic.return_types import COLUMN_KINDS, ColumnType, contradicts, merge
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel

ONTOLOGY = """\
format: graph_cli.ir/1
module: {id: sample, prefix: smp, version: 1.0.0, summary: Sample module.}
datatypes:
  Code: {summary: A code., base: string, max_length: 8}
  Heading: {summary: A heading., base: lang_string}
enums:
  Kind:
    summary: A kind.
    values:
      big: {summary: Big.}
      small: {summary: Small.}
lifecycles:
  Flow:
    summary: A flow.
    states:
      open: {summary: Open., initial: true}
      done: {summary: Done., terminal: true}
    transitions:
      - {from: open, to: done, summary: Finish.}
classes:
  Thing:
    summary: A thing.
    attributes:
      name: {summary: Name., type: string}
      tags: {summary: Tags., type: string, many: true}
      title: {summary: Title., type: Heading}
      titles: {summary: Titles., type: lang_text, many: true}
      body: {summary: Body., type: text}
      home: {summary: Home page., type: iri}
      code: {summary: Code., type: Code}
      amount: {summary: Amount., type: decimal}
      born: {summary: Born., type: date}
      seen: {summary: Seen., type: datetime}
      rank: {summary: Rank., type: integer}
      flag: {summary: Flag., type: boolean}
      kind: {summary: Kind., type: Kind}
      state: {summary: State., lifecycle: Flow}
      data: {summary: Data., type: json}
      span: {summary: Span., type: duration}
      size: {summary: Size., type: integer}
  Special:
    summary: A special thing.
    is_a: Thing
    attributes:
      extra: {summary: Extra., type: string}
  Other:
    summary: Something else.
    attributes:
      size: {summary: Size as text., type: string}
      name: {summary: Name., type: string}
relations:
  knows:
    summary: One thing knows another.
    domain: Thing
    range: Thing
    qualifiers:
      since: {summary: Since., type: date}
      weight: {summary: Weight., type: decimal}
  likes:
    summary: A thing likes another.
    domain: Thing
    range: Thing
    qualifiers:
      since: {summary: Since., type: string}
  owns:
    summary: A thing owns another thing.
    domain: Thing
    range: Other
"""


def sample_model() -> CompiledModel:
    outcome = compile_bundle("sample", LayeredSource(MappingSource({"sample": ONTOLOGY.encode()})))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


MODEL = sample_model()


def source(
    query: str, columns: list[str], extra: str = "", declared: dict[str, str] | None = None
) -> str:
    """A function file with ``query`` and a column per name; ``declared`` adds lines to a column
    (``{"n": "type: integer; many: true"}``), ``extra`` more top-level keys."""
    lines = [
        "schema: graph_cli.function/1",
        "name: probe",
        "summary: A probe.",
        "use_when: Probing.",
    ]
    lines += extra.splitlines()
    lines += ["query: |", *(f"  {line}" for line in query.strip().splitlines()), "returns:"]
    for column in columns:
        lines += [f"  - column: {column}", f"    summary: The {column}."]
        if declared and column in declared:
            lines += [f"    {line}" for line in declared[column].split("; ")]
    lines += ["examples:", "  - question: Probe?", ""]
    return "\n".join(lines)


def types_of(
    query: str,
    *columns: str,
    declared: dict[str, str] | None = None,
) -> tuple[ColumnType | None, ...]:
    """The types of the columns of ``query`` (the columns are named in ``RETURN ... AS``)."""
    text = source(query, list(columns), declared=declared)
    document, problems = compile_catalog({"probe.yaml": text}, MODEL)
    assert [p.to_json() for p in problems] == []
    (entry,) = document["functions"]
    defn, found = load_function("probe.yaml", text)
    assert defn is not None and found == []
    return column_types(defn, str(entry["query"]), MODEL)


def one(query: str, declared: str | None = None) -> ColumnType | None:
    """The type of the one column ``c`` of ``query``."""
    return types_of(query, "c", declared={"c": declared} if declared else None)[0]


def attr(name: str) -> ColumnType | None:
    return one(f"MATCH (t:Thing) RETURN t.{name} AS c")


# ---- the datatypes of attributes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "kind"),
    [
        ("name", "string"),
        ("body", "string"),
        ("home", "string"),
        ("code", "string"),
        ("span", "string"),
        ("state", "string"),
        ("amount", "decimal"),
        ("born", "date"),
        ("seen", "datetime"),
        ("rank", "integer"),
        ("flag", "boolean"),
        ("data", "any"),
        ("title", "lang"),
    ],
)
def test_an_attribute_has_the_kind_of_its_base_type(attribute: str, kind: str) -> None:
    found = attr(attribute)
    assert found == ColumnType(kind)


def test_an_enum_attribute_names_its_enum() -> None:
    assert attr("kind") == ColumnType("enum", enum="smp:Kind")


def test_a_many_attribute_is_a_list() -> None:
    assert attr("tags") == ColumnType("string", many=True)
    assert attr("titles") == ColumnType("lang", many=True)


def test_an_attribute_is_always_nullable() -> None:
    for name in ("name", "rank", "tags", "title"):
        found = attr(name)
        assert found is not None and found.nullable


def test_an_attribute_of_a_subclass_variable_is_found_in_the_subclass_only() -> None:
    assert one("MATCH (s:Special) RETURN s.extra AS c") == ColumnType("string")
    assert one("MATCH (s:Special) RETURN s.name AS c") == ColumnType("string")


def test_a_variable_of_several_classes_types_what_they_agree_on() -> None:
    assert one("MATCH (x) RETURN x.born AS c") == ColumnType("date")
    assert one("MATCH (x:Thing|Other) RETURN x.name AS c") == ColumnType("string")


def test_an_attribute_the_classes_declare_differently_stays_untyped() -> None:
    assert one("MATCH (x) RETURN x.size AS c") is None
    assert one("MATCH (x:Thing|Other) RETURN x.size AS c") is None


# ---- nodes ---------------------------------------------------------------------------------


def test_a_node_variable_is_an_entity_of_the_class_of_its_label() -> None:
    assert one("MATCH (t:Thing) RETURN t AS c") == ColumnType(
        "entity", of_class="smp:Thing", nullable=False
    )


def test_the_class_is_the_label_not_the_subclasses_it_matches() -> None:
    found = one("MATCH (t:Thing) RETURN t AS c")
    assert found is not None and found.of_class == "smp:Thing"
    special = one("MATCH (t:Special) RETURN t AS c")
    assert special is not None and special.of_class == "smp:Special"


@pytest.mark.parametrize("pattern", ["(t)", "(t:Thing|Other)", "(t:Thing:Special)"])
def test_a_node_without_one_label_has_no_class(pattern: str) -> None:
    found = one(f"MATCH {pattern} RETURN t AS c")
    assert found == ColumnType("entity", nullable=False)


def test_a_label_given_later_names_the_class() -> None:
    found = one("MATCH (t) MATCH (t:Special) RETURN t AS c")
    assert found == ColumnType("entity", of_class="smp:Special", nullable=False)


def test_a_node_bound_by_optional_match_is_nullable_and_one_bound_by_match_is_not() -> None:
    query = "MATCH (a:Thing) OPTIONAL MATCH (a)-[:knows]->(b:Thing) RETURN a AS c, b AS d"
    first, second = types_of(query, "c", "d")
    assert first is not None and not first.nullable
    assert second is not None and second.nullable and second.of_class == "smp:Thing"


def test_a_node_first_bound_by_match_stays_not_null_in_a_later_optional_match() -> None:
    found = one("MATCH (a:Thing) OPTIONAL MATCH (a)-[:knows]->(:Thing) RETURN a AS c")
    assert found is not None and not found.nullable


def test_a_node_of_a_variable_length_pattern_is_typed() -> None:
    found = one("MATCH (a:Thing)-[:knows*1..3]->(b:Special) RETURN b AS c")
    assert found == ColumnType("entity", of_class="smp:Special", nullable=False)


# ---- an OPTIONAL MATCH does not narrow what was bound before ------------------------------


def test_an_optional_match_does_not_give_a_node_bound_before_a_class() -> None:
    query = "MATCH (x) OPTIONAL MATCH (x:Other) RETURN x AS c, x.size AS d, x.id AS e"
    entity, size, ident = types_of(query, "c", "d", "e")
    assert entity == ColumnType("entity", nullable=False)
    assert size is None  # the classes of x still disagree on size
    assert ident == ColumnType("ref", nullable=False)


def test_an_optional_match_does_not_narrow_the_relations_of_a_relationship_bound_before() -> None:
    plain = "MATCH (a:Thing)-[r]->(b:Thing) RETURN r.since AS c"
    optional = (
        "MATCH (a:Thing)-[r]->(b:Thing) OPTIONAL MATCH (a)-[r:knows]->(b) RETURN r.since AS c"
    )
    assert one(plain) is None and one(optional) is None


def test_an_optional_match_leaves_a_value_from_with_or_unwind_untyped() -> None:
    assert one("MATCH (t:Thing) WITH t AS u OPTIONAL MATCH (u:Special) RETURN u AS c") is None
    query = (
        "MATCH (t:Thing) WITH collect(t) AS ts UNWIND ts AS u "
        "OPTIONAL MATCH (u:Special) RETURN u AS c"
    )
    assert one(query) is None


def test_an_optional_match_keeps_the_nullability_and_class_of_a_node_bound_before() -> None:
    query = "MATCH (t:Thing) OPTIONAL MATCH (t:Special)-[:knows]->(u) RETURN t AS c, u AS d"
    first, second = types_of(query, "c", "d")
    assert first == ColumnType("entity", of_class="smp:Thing", nullable=False)
    assert second == ColumnType("entity", nullable=True)


def test_an_optional_match_types_a_name_that_went_out_of_scope_before_it() -> None:
    query = "MATCH (t:Thing) WITH t.name AS n OPTIONAL MATCH (t:Other) RETURN t AS c"
    assert one(query) == ColumnType("entity", of_class="smp:Other", nullable=True)


def test_a_match_still_narrows_what_was_bound_before_it() -> None:
    assert one("MATCH (x) MATCH (x:Special) RETURN x AS c") == ColumnType(
        "entity", of_class="smp:Special", nullable=False
    )
    found = one("MATCH (x) MATCH (x:Other) RETURN x.size AS c")
    assert found == ColumnType("string")


# ---- system fields and qualifiers ------------------------------------------------------------


def test_id_and_alias_are_references_of_the_class_of_the_label() -> None:
    first, second = types_of("MATCH (t:Thing) RETURN t.id AS c, t.alias AS d", "c", "d")
    assert first == ColumnType("ref", of_class="smp:Thing", nullable=False)
    assert second == ColumnType("ref", of_class="smp:Thing", nullable=True)


def test_the_id_of_a_node_without_a_class_is_a_reference_of_no_class() -> None:
    assert one("MATCH (t) RETURN t.id AS c") == ColumnType("ref", nullable=False)


def test_the_id_of_an_optional_node_is_nullable() -> None:
    found = one("MATCH (a:Thing) OPTIONAL MATCH (a)-[:knows]->(b:Thing) RETURN b.id AS c")
    assert found is not None and found.nullable


def test_the_other_system_fields_have_their_kinds() -> None:
    expected = {
        "rev": "integer",
        "tx_seq": "integer",
        "created_at": "datetime",
        "recorded_at": "datetime",
        "type": "string",
        "record_status": "string",
        "tx_id": "string",
        "content_hash": "string",
        "ontology_version": "string",
    }
    query = "MATCH (t:Thing) RETURN " + ", ".join(f"t.{n} AS {n}" for n in expected)
    found = types_of(query, *expected)
    assert {
        n: (None if t is None else t.kind) for n, t in zip(expected, found, strict=True)
    } == expected
    assert all(t is not None and not t.nullable for t in found)


def test_a_validity_field_is_not_typed() -> None:
    assert one("MATCH (t:Thing) RETURN t.valid_from AS c") is None


def test_a_qualifier_of_a_relationship_has_the_type_of_the_qualifier() -> None:
    assert one("MATCH (:Thing)-[k:knows]->(:Thing) RETURN k.since AS c") == ColumnType("date")
    assert one("MATCH (:Thing)-[k:knows]->(:Thing) RETURN k.weight AS c") == ColumnType("decimal")


def test_a_qualifier_the_relations_declare_differently_stays_untyped() -> None:
    assert one("MATCH (:Thing)-[k:knows|likes]->(:Thing) RETURN k.since AS c") is None


def test_a_qualifier_of_a_variable_length_relationship_is_not_typed() -> None:
    found = one("MATCH (a:Thing)-[k:knows*1..2]->(:Thing) RETURN size(k) AS c")
    assert found is None


# ---- aggregates and literals -----------------------------------------------------------------


def test_count_is_an_integer_that_is_never_null() -> None:
    expected = ColumnType("integer", nullable=False)
    assert one("MATCH (t:Thing) RETURN count(*) AS c") == expected
    assert one("MATCH (t:Thing) RETURN count(t) AS c") == expected
    assert one("MATCH (t:Thing) RETURN count(DISTINCT t.name) AS c") == expected


def test_collect_is_a_list_of_what_it_collects_and_never_null() -> None:
    assert one("MATCH (t:Thing) RETURN collect(t.born) AS c") == ColumnType(
        "date", many=True, nullable=False
    )
    assert one("MATCH (t:Thing) RETURN collect(DISTINCT t.kind) AS c") == ColumnType(
        "enum", many=True, enum="smp:Kind", nullable=False
    )
    assert one("MATCH (t:Thing) RETURN collect(t) AS c") == ColumnType(
        "entity", many=True, of_class="smp:Thing", nullable=False
    )


def test_collect_of_something_untyped_or_a_list_is_a_list_of_anything() -> None:
    expected = ColumnType("any", many=True, nullable=False)
    assert one("MATCH (t:Thing) RETURN collect(t.rank + 1) AS c") == expected
    assert one("MATCH (t:Thing) RETURN collect(t.tags) AS c") == expected


def test_a_literal_has_the_kind_of_its_value_and_is_never_null() -> None:
    query = "MATCH (t:Thing) RETURN 'a' AS s, 1 AS i, 1.5 AS d, true AS b"
    found = types_of(query, "s", "i", "d", "b")
    assert found == tuple(
        ColumnType(kind, nullable=False) for kind in ("string", "integer", "decimal", "boolean")
    )


def test_the_null_literal_is_untyped() -> None:
    assert one("MATCH (t:Thing) RETURN null AS c") is None


# ---- what stays untyped ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "sum(t.rank)",
        "avg(t.rank)",
        "min(t.rank)",
        "max(t.born)",
        "coalesce(t.name, 'none')",
        "CASE WHEN t.rank > 1 THEN 'a' ELSE 'b' END",
        "t.rank + 1",
        "t.name + 'x'",
        "size(t.tags)",
        "labels(t)",
        "t.tags[0]",
        "toString(t.rank)",
        "properties(t)",
        "$p",
    ],
)
def test_what_the_query_does_not_make_certain_stays_untyped(expression: str) -> None:
    extra = "params:\n  p: {type: string, summary: A value.}\n" if "$p" in expression else ""
    query = f"MATCH (t:Thing) RETURN {expression} AS c"
    text = source(query, ["c"], extra=extra)
    document, problems = compile_catalog({"probe.yaml": text}, MODEL)
    assert [p.to_json() for p in problems] == []
    (entry,) = document["functions"]
    defn, _ = load_function("probe.yaml", text)
    assert defn is not None
    assert column_types(defn, str(entry["query"]), MODEL) == (None,)


def test_a_relationship_is_untyped() -> None:
    assert one("MATCH (:Thing)-[k:knows]->(:Thing) RETURN k AS c") is None


def test_a_value_through_with_as_is_untyped() -> None:
    assert one("MATCH (t:Thing) WITH t.name AS n RETURN n AS c") is None
    assert one("MATCH (t:Thing) WITH t AS u RETURN u AS c") is None
    assert one("MATCH (t:Thing) WITH t.name AS name RETURN name AS c") is None
    assert one("MATCH (t:Thing) WITH count(t) AS n RETURN n AS c") is None


def test_a_node_passed_on_by_its_own_name_through_with_keeps_its_type() -> None:
    found = one("MATCH (t:Thing) WITH t ORDER BY t.name LIMIT 3 RETURN t AS c")
    assert found == ColumnType("entity", of_class="smp:Thing", nullable=False)
    assert one("MATCH (t:Thing) WITH t RETURN t.born AS c") == ColumnType("date")
    star = one("MATCH (t:Thing) WITH * RETURN t.born AS c")
    assert star == ColumnType("date")


def test_a_node_dropped_by_with_is_not_typed_by_a_later_variable_of_the_same_name() -> None:
    query = "MATCH (t:Thing) WITH t.name AS x MATCH (u:Other) WITH x AS t RETURN t AS c"
    assert one(query) is None


def test_a_variable_from_unwind_is_untyped() -> None:
    assert one("MATCH (t:Thing) UNWIND t.tags AS tag RETURN tag AS c") is None


# ---- declared types --------------------------------------------------------------------------


def test_a_declared_type_types_a_column_the_query_leaves_open() -> None:
    declared = one("MATCH (t:Thing) RETURN sum(t.rank) AS c", declared="type: integer")
    assert declared == ColumnType("integer")
    assert declared is not None and declared.nullable


def test_a_declaration_carries_its_enum_class_and_many() -> None:
    query = "MATCH (t:Thing) RETURN t.name AS c"
    # the column is typed by the query; declarations are read, not applied, where they agree
    assert one(query, "type: string") == ColumnType("string")
    open_query = "MATCH (t:Thing) RETURN coalesce(t.kind, 'big') AS c"
    assert one(open_query, "type: enum; enum: smp:Kind") == ColumnType("enum", enum="smp:Kind")
    assert one(open_query, "type: string; many: true") == ColumnType("string", many=True)
    assert one(open_query, "type: ref; class: smp:Thing") == ColumnType("ref", of_class="smp:Thing")
    assert one(open_query, "type: entity") == ColumnType("entity")
    assert one(open_query, "type: any") == ColumnType("any")


def test_an_agreeing_declaration_keeps_the_inferred_nullability() -> None:
    found = one("MATCH (t:Thing) RETURN count(t) AS c", declared="type: integer")
    assert found == ColumnType("integer", nullable=False)


def test_a_declared_class_is_kept_for_a_node_of_no_known_class() -> None:
    found = one("MATCH (t) RETURN t.id AS c", declared="type: ref; class: smp:Thing")
    assert found == ColumnType("ref", of_class="smp:Thing", nullable=False)


def test_a_declared_any_never_contradicts_but_its_list_must_match() -> None:
    for query in (
        "MATCH (t:Thing) RETURN count(t) AS c",
        "MATCH (t:Thing) RETURN t AS c",
        "MATCH (t:Thing) RETURN t.born AS c",
    ):
        assert one(query, "type: any") is not None
    assert one("MATCH (t:Thing) RETURN t.tags AS c", "type: any; many: true") is not None
    for query, declared in (
        ("MATCH (t:Thing) RETURN t.tags AS c", "type: any"),
        ("MATCH (t:Thing) RETURN t.born AS c", "type: any; many: true"),
    ):
        text = source(query, ["c"], declared={"c": declared})
        _, problems = compile_catalog({"probe.yaml": text}, MODEL)
        assert [p.code for p in problems] == ["function.returns_type_mismatch"], declared
    assert not contradicts(ColumnType("any"), ColumnType("entity", of_class="smp:Thing"))


def test_a_declaration_may_not_contradict_what_the_query_makes_certain() -> None:
    for query, declared in (
        ("MATCH (t:Thing) RETURN count(t) AS c", "type: string"),
        ("MATCH (t:Thing) RETURN t.born AS c", "type: datetime"),
        ("MATCH (t:Thing) RETURN t.born AS c", "type: date; many: true"),
        ("MATCH (t:Thing) RETURN t.tags AS c", "type: string"),
        ("MATCH (t:Thing) RETURN t.kind AS c", "type: string"),
        ("MATCH (t:Thing) RETURN t AS c", "type: ref; class: smp:Thing"),
        ("MATCH (t:Thing) RETURN t AS c", "type: entity; class: smp:Special"),
        ("MATCH (t:Thing) RETURN t.id AS c", "type: ref; class: smp:Other"),
    ):
        text = source(query, ["c"], declared={"c": declared})
        _, problems = compile_catalog({"probe.yaml": text}, MODEL)
        assert [p.code for p in problems] == ["function.returns_type_mismatch"], (query, declared)
        assert problems[0].path == "returns[0].type"
        assert problems[0].details["column"] == "c"


def test_a_mismatch_names_what_was_declared_and_what_the_query_makes() -> None:
    text = source("MATCH (t:Thing) RETURN t.born AS c", ["c"], declared={"c": "type: string"})
    _, problems = compile_catalog({"probe.yaml": text}, MODEL)
    (problem,) = problems
    assert problem.details["declared"] == "string" and problem.details["inferred"] == "date"
    assert "declare that, or drop the type" in problem.message


def test_a_declared_enum_or_class_must_be_a_term_of_the_model() -> None:
    query = "MATCH (t:Thing) RETURN coalesce(t.kind, 'big') AS c"
    for declared, code, path in (
        ("type: enum; enum: smp:Missing", "function.unknown_enum", "returns[0].enum"),
        ("type: ref; class: smp:Missing", "function.unknown_class", "returns[0].class"),
        ("type: entity; class: smp:Missing", "function.unknown_class", "returns[0].class"),
    ):
        text = source(query, ["c"], declared={"c": declared})
        _, problems = compile_catalog({"probe.yaml": text}, MODEL)
        assert [(p.code, p.path) for p in problems] == [(code, path)], declared


def test_the_declaration_is_stored_in_the_entry_and_nothing_else_is() -> None:
    query = "MATCH (t:Thing) RETURN t.name AS a, sum(t.rank) AS b"
    text = source(query, ["a", "b"], declared={"b": "type: integer"})
    document, problems = compile_catalog({"probe.yaml": text}, MODEL)
    assert problems == []
    (entry,) = document["functions"]
    returns = entry["returns"]
    assert returns[0] == {"column": "a", "summary": "The a."}  # inferred types are not stored
    assert returns[1] == {
        "column": "b",
        "summary": "The b.",
        "type": "integer",
        "class": None,
        "enum": None,
        "many": False,
    }


def test_an_entry_without_declarations_keeps_the_bytes_it_had() -> None:
    from graph_cli.kernel.canonical import hash_document

    text = source("MATCH (t:Thing) RETURN t.name AS a", ["a"])
    document, _ = compile_catalog({"probe.yaml": text}, MODEL)
    (entry,) = document["functions"]
    assert set(entry["returns"][0]) == {"column", "summary"}
    assert entry["hash"] == hash_document({k: v for k, v in entry.items() if k != "hash"})


def test_a_query_that_no_longer_resolves_leaves_only_the_declared_types() -> None:
    text = source(
        "MATCH (t:Thing) RETURN t.name AS a, sum(t.rank) AS b",
        ["a", "b"],
        declared={"b": "type: integer"},
    )
    defn, _ = load_function("probe.yaml", text)
    assert defn is not None
    assert column_types(
        defn, "MATCH (t:`smp:Gone`) RETURN t.name AS a, sum(t.rank) AS b", MODEL
    ) == (
        None,
        ColumnType("integer"),
    )


# ---- the declaration in the file -------------------------------------------------------------


def declaration_problems(declared: str) -> list[tuple[str, str | None]]:
    text = source("MATCH (t:Thing) RETURN t.name AS c", ["c"], declared={"c": declared})
    defn, problems = load_function("probe.yaml", text)
    assert defn is None
    return [(p.code, p.path) for p in problems]


def test_every_kind_is_declarable() -> None:
    for kind in COLUMN_KINDS:
        extra = "; enum: smp:Kind" if kind == "enum" else ""
        text = source(
            "MATCH (t:Thing) RETURN t.name AS c", ["c"], declared={"c": f"type: {kind}{extra}"}
        )
        defn, problems = load_function("probe.yaml", text)
        assert problems == [] and defn is not None, kind
        assert defn.returns[0].type == kind


def test_the_declaration_keys_are_checked() -> None:
    assert declaration_problems("type: number") == [("function.invalid_value", "returns[0].type")]
    assert declaration_problems("type: enum") == [("function.missing_key", "returns[0].enum")]
    assert declaration_problems("type: string; enum: smp:Kind") == [
        ("function.invalid_value", "returns[0].enum")
    ]
    assert declaration_problems("type: string; class: smp:Thing") == [
        ("function.invalid_value", "returns[0].class")
    ]
    assert declaration_problems("many: true") == [("function.missing_key", "returns[0].type")]
    assert declaration_problems("type: string; many: sometimes") == [
        ("function.invalid_value", "returns[0].many")
    ]
    assert declaration_problems("type: string; colour: red") == [
        ("function.unknown_key", "returns[0].colour")
    ]


def test_a_declaration_reads_back_from_the_entry() -> None:
    from graph_cli.kinetic.format import FunctionDef

    text = source(
        "MATCH (t:Thing) RETURN sum(t.rank) AS c",
        ["c"],
        declared={"c": "type: integer; many: true"},
    )
    document, problems = compile_catalog({"probe.yaml": text}, MODEL)
    assert problems == []
    (entry,) = document["functions"]
    again = FunctionDef.from_json(entry, str(entry["source_query"]))
    assert again.returns[0].declared == ColumnType("integer", many=True)


# ---- comparing and merging -------------------------------------------------------------------


def test_contradicts_compares_kind_list_enum_and_class_where_the_inference_names_them() -> None:
    inferred = ColumnType("ref", of_class="smp:Thing")
    assert not contradicts(ColumnType("ref", of_class="smp:Thing"), inferred)
    assert contradicts(ColumnType("string"), inferred)
    assert contradicts(ColumnType("ref", many=True, of_class="smp:Thing"), inferred)
    assert contradicts(ColumnType("ref", of_class="smp:Other"), inferred)
    assert not contradicts(ColumnType("ref", of_class="smp:Other"), ColumnType("ref"))
    assert contradicts(ColumnType("enum", enum="smp:A"), ColumnType("enum", enum="smp:B"))
    assert not contradicts(ColumnType("enum", enum="smp:A"), ColumnType("enum"))


def test_merge_prefers_the_inference_and_takes_what_the_declaration_adds() -> None:
    declared = ColumnType("ref", of_class="smp:Thing")
    inferred = ColumnType("ref", nullable=False)
    assert merge(declared, inferred) == ColumnType("ref", of_class="smp:Thing", nullable=False)
    assert merge(declared, None) == declared and merge(None, inferred) == inferred
    assert merge(None, None) is None
