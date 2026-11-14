"""The migration file ``graph_cli.migration/1``: parsing, checking against two models, the hash."""

import pytest
from support.modules import compile_modules, header

from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.migration import (
    MigrationDoc,
    Retype,
    check_migration,
    migration_hash,
    parse_migration,
)
from graph_cli.ontology_layer.model import CompiledModel

# ---- parsing ----------------------------------------------------------------------------------

PREFACE = "format: graph_cli.migration/1\nfrom: 1.0.0\nsummary: Test migration.\n"

ALL_STEPS = (
    PREFACE
    + """steps:
  - retype: {from: ex:Gadget, to: ex:Item, attributes: {name: title}}
  - fill: {type: ex:Item, attribute: priority, value: "1"}
  - map_values: {type: ex:Item, attribute: condition, values: {worn: used}}
  - map_values: {type: ex:Item, attribute: state, values: {held: paused}}
  - map_qualifier_values: {relation: ex:link, qualifier: kind, values: {heavy: medium}}
  - drop: {type: ex:Item, attribute: legacy}
  - drop_edges: {relation: ex:stale}
  - rename_qualifier: {relation: ex:link, from: remark, to: note}
  - drop_qualifier: {relation: ex:link, qualifier: weight}
  - convert: {type: ex:Item, attribute: note, to: lang_string}
  - convert: {type: ex:Item, attribute: body, to: lang_text}
  - convert: {type: ex:Item, attribute: custodian, to: many}
  - convert: {type: ex:Item, attribute: tags, to: single}
"""
)


def parsed(text: str) -> MigrationDoc:
    result = parse_migration(text)
    assert result.problems == (), [p.to_json() for p in result.problems]
    assert result.doc is not None
    return result.doc


def problems_of(text: str) -> list[tuple[str, str]]:
    result = parse_migration(text)
    assert result.doc is None
    return [(p.path, p.message) for p in result.problems]


def test_every_step_parses() -> None:
    doc = parsed(ALL_STEPS)
    assert (doc.from_version, doc.summary) == ("1.0.0", "Test migration.")
    assert [type(step).NAME for step in doc.steps] == [
        "retype",
        "fill",
        "map_values",
        "map_values",
        "map_qualifier_values",
        "drop",
        "drop_edges",
        "rename_qualifier",
        "drop_qualifier",
        "convert",
        "convert",
        "convert",
        "convert",
    ]
    first = doc.steps[0]
    assert isinstance(first, Retype)
    assert (first.from_, first.to, dict(first.attributes)) == (
        "ex:Gadget",
        "ex:Item",
        {"name": "title"},
    )


def test_a_migration_without_steps_is_valid() -> None:
    assert parsed(PREFACE).steps == ()
    assert parsed(PREFACE + "steps: []\n").steps == ()
    assert parsed(PREFACE + "steps:\n").steps == ()


def test_the_document_normalizes_to_json() -> None:
    doc = parsed(
        PREFACE + "steps:\n  - retype: {from: ex:A, to: ex:B}\n  - drop_edges: {relation: ex:r}\n"
    )
    assert doc.to_json() == {
        "format": "graph_cli.migration/1",
        "from": "1.0.0",
        "summary": "Test migration.",
        "steps": [
            {"retype": {"from": "ex:A", "to": "ex:B", "attributes": {}}},
            {"drop_edges": {"relation": "ex:r"}},
        ],
    }


def test_the_hash_covers_the_document_not_the_bytes() -> None:
    plain = parsed(
        PREFACE + "steps:\n  - map_values: {type: ex:A, attribute: a, values: {x: y, p: q}}\n"
    )
    spelled = parsed(
        "# a comment\nsummary: 'Test migration.'\nformat: graph_cli.migration/1\nfrom: '1.0.0'\n"
        "steps:\n  - map_values:\n      values: {p: q, x: y}\n      attribute: a\n"
        "      type: ex:A\n"
    )
    assert migration_hash(plain) == migration_hash(spelled)
    other = parsed(
        PREFACE + "steps:\n  - map_values: {type: ex:A, attribute: a, values: {x: z, p: q}}\n"
    )
    assert migration_hash(other) != migration_hash(plain)
    assert migration_hash(plain).startswith("sha256:")
    assert migration_hash(parsed(PREFACE.replace("Test", "Other"))) != migration_hash(
        parsed(PREFACE)
    )


@pytest.mark.parametrize(
    ("text", "path", "fragment"),
    [
        ("a: [", "", "not strict YAML"),
        ("- a\n", "", "expected a mapping"),
        ("from: 1.0.0\nsummary: S.\n", "", "'format' is required"),
        (
            "format: graph_cli.migration/2\nfrom: 1.0.0\nsummary: S.\n",
            "format",
            "expected graph_cli.migration/1",
        ),
        ("format: graph_cli.migration/1\nsummary: S.\n", "", "'from' is required"),
        ("format: graph_cli.migration/1\nfrom: '1.0'\nsummary: S.\n", "from", "MAJOR.MINOR.PATCH"),
        ("format: graph_cli.migration/1\nfrom: 1.0.0\n", "", "'summary' is required"),
        (
            "format: graph_cli.migration/1\nfrom: 1.0.0\nsummary: '  '\n",
            "summary",
            "summary is required",
        ),
        (PREFACE + "extra: 1\n", "extra", "unknown key 'extra'"),
        (PREFACE + "steps: nope\n", "steps", "expected a list"),
        (PREFACE + "steps:\n  - plain\n", "steps[0]", "a step is a mapping with one key"),
        (
            PREFACE + "steps:\n  - {drop_edges: {relation: 'ex:r'}, fill: {}}\n",
            "steps[0]",
            "one key",
        ),
        (
            PREFACE + "steps:\n  - frobnicate: {}\n",
            "steps[0].frobnicate",
            "unknown step 'frobnicate'",
        ),
        (
            PREFACE + "steps:\n  - drop_edges: {relation: 'ex:r', extra: 1}\n",
            "steps[0].drop_edges.extra",
            "unknown key",
        ),
        (PREFACE + "steps:\n  - drop_edges: {}\n", "steps[0].drop_edges", "'relation' is required"),
        (PREFACE + "steps:\n  - drop_edges: x\n", "steps[0].drop_edges", "expected a mapping"),
        (
            PREFACE + "steps:\n  - drop_edges: {relation: link}\n",
            "steps[0].drop_edges.relation",
            "relation CURIE",
        ),
        (
            PREFACE + "steps:\n  - drop_edges: {relation: 'ex:Link'}\n",
            "steps[0].drop_edges.relation",
            "relation CURIE",
        ),
        (
            PREFACE + "steps:\n  - retype: {from: Item, to: 'ex:B'}\n",
            "steps[0].retype.from",
            "class CURIE",
        ),
        (
            PREFACE + "steps:\n  - retype: {from: 'ex:a', to: 'ex:B'}\n",
            "steps[0].retype.from",
            "class CURIE",
        ),
        (
            PREFACE + "steps:\n  - retype: {from: 'ex:A', to: 'ex:B', attributes: {Old: new}}\n",
            "steps[0].retype.attributes.Old",
            "local name",
        ),
        (
            PREFACE + "steps:\n  - retype: {from: 'ex:A', to: 'ex:B', attributes: {old: New}}\n",
            "steps[0].retype.attributes.old",
            "local name",
        ),
        (
            PREFACE + "steps:\n  - retype: {from: 'ex:A', to: 'ex:B', attributes: [a]}\n",
            "steps[0].retype.attributes",
            "expected a mapping",
        ),
        (
            PREFACE + "steps:\n  - fill: {type: 'ex:A', attribute: Size, value: x}\n",
            "steps[0].fill.attribute",
            "local name",
        ),
        (
            PREFACE + "steps:\n  - fill: {type: 'ex:A', attribute: size, value: [x]}\n",
            "steps[0].fill.value",
            "expected a string",
        ),
        (
            PREFACE + "steps:\n  - fill: {type: 'ex:A', attribute: size, value: ~}\n",
            "steps[0].fill.value",
            "expected a string",
        ),
        (
            PREFACE + "steps:\n  - map_values: {type: 'ex:A', attribute: a, values: {}}\n",
            "steps[0].map_values.values",
            "must not be empty",
        ),
        (
            PREFACE + "steps:\n  - map_values: {type: 'ex:A', attribute: a, values: {x: [y]}}\n",
            "steps[0].map_values.values.x",
            "expected a string",
        ),
        (
            PREFACE
            + "steps:\n  - map_qualifier_values: {relation: 'ex:r', qualifier: q, values: {}}\n",
            "steps[0].map_qualifier_values.values",
            "must not be empty",
        ),
        (
            PREFACE + "steps:\n  - rename_qualifier: {relation: 'ex:r', from: a}\n",
            "steps[0].rename_qualifier",
            "'to' is required",
        ),
        (
            PREFACE + "steps:\n  - drop_qualifier: {relation: 'r', qualifier: q}\n",
            "steps[0].drop_qualifier.relation",
            "relation CURIE",
        ),
        (
            PREFACE + "steps:\n  - convert: {type: 'ex:A', attribute: a, to: upper}\n",
            "steps[0].convert.to",
            "is not one of lang_string",
        ),
    ],
)
def test_parse_refusals_name_their_path(text: str, path: str, fragment: str) -> None:
    found = problems_of(text)
    assert any(p == path and fragment in m for p, m in found), found


def test_one_run_reports_every_problem() -> None:
    found = problems_of(
        PREFACE + "steps:\n  - fill: {type: bare, attribute: Bad}\n  - frobnicate: {}\n"
    )
    assert [p for p, _ in found] == [
        "steps[0].fill",
        "steps[0].fill.type",
        "steps[0].fill.attribute",
        "steps[1].frobnicate",
    ]


# ---- checking ---------------------------------------------------------------------------------

OLD = """datatypes:
  Code: {summary: A code., base: string, max_length: 4}
enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, worn: {summary: Worn.}, broken: {summary: Broken.}}
  Kind:
    summary: Kind.
    values: {light: {summary: Light.}, heavy: {summary: Heavy.}}
lifecycles:
  Flow:
    summary: Flow.
    states:
      open: {summary: Open., initial: true}
      held: {summary: Held.}
      closed: {summary: Closed., terminal: true}
    transitions:
      - {from: open, to: held}
      - {from: held, to: closed}
classes:
  Item:
    summary: Item.
    attributes:
      title: {summary: Title., type: string, role: label}
      note: {summary: Note., type: string}
      body: {summary: Body., type: text}
      tags: {summary: Tags., type: string, many: true}
      owner: {summary: Owner., type: string}
      condition: {summary: Condition., type: Condition}
      state: {summary: State., lifecycle: Flow}
      legacy: {summary: Legacy., type: string}
      code: {summary: Code., type: Code}
  Gadget:
    summary: Gadget.
    attributes:
      name: {summary: Name., type: string}
  Part:
    summary: Part.
relations:
  link:
    summary: Link.
    domain: Item
    range: Item
    qualifiers:
      kind: {summary: Kind., type: Kind}
      remark: {summary: Remark., type: string}
      weight: {summary: Weight., type: string}
  stale: {summary: Stale., domain: Item, range: Item}
  uses: {summary: Uses., domain: Item, range: Part}
"""

NEW = """datatypes:
  Code: {summary: A code., base: string, max_length: 4}
  Rank: {summary: A rank., base: integer, min: 1, max: 5}
enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, used: {summary: Used.}, broken: {summary: Broken.}}
  Kind:
    summary: Kind.
    values: {light: {summary: Light.}, heavy: {summary: Heavy.}, medium: {summary: Medium.}}
lifecycles:
  Flow:
    summary: Flow.
    states:
      open: {summary: Open., initial: true}
      paused: {summary: Paused.}
      closed: {summary: Closed., terminal: true}
    transitions:
      - {from: open, to: paused}
      - {from: paused, to: closed}
classes:
  Item:
    summary: Item.
    attributes:
      title: {summary: Title., type: string, role: label}
      note: {summary: Note., type: lang_string}
      body: {summary: Body., type: lang_text}
      tags: {summary: Tags., type: string}
      custodian: {summary: Custodian., type: string, many: true, renamed_from: owner}
      condition: {summary: Condition., type: Condition}
      state: {summary: State., lifecycle: Flow}
      code: {summary: Code., type: Code}
      priority: {summary: Priority., type: Rank}
      grade: {summary: Grade., type: Condition}
  Shape:
    summary: Abstract shape.
    abstract: true
  Fresh:
    summary: A new class.
relations:
  link:
    summary: Link.
    domain: Item
    range: Item
    qualifiers:
      kind: {summary: Kind., type: Kind}
      note: {summary: Note., type: string}
  uses: {summary: Uses., domain: Item, range: "aa:Part"}
"""

AREA = (
    header("area_a", "aa", "1.0.0")
    + """classes:
  Part:
    summary: Part.
    renamed_from: ex:Part
"""
)


def old_model() -> CompiledModel:
    main = header("example", "ex", "1.0.0") + OLD
    return compile_modules("example", {"example": main})


def new_model() -> CompiledModel:
    main = header("example", "ex", "2.0.0", ("area_a",)) + NEW
    return compile_modules("example", {"example": main, "area_a": AREA})


def check(text: str, *, language: str | None = "en") -> list[tuple[str, str]]:
    old, new = old_model(), new_model()
    doc = parsed(PREFACE + text)
    found = check_migration(doc, old, new, derive_renames(old, new), language=language)
    return [(p.path, p.message) for p in found]


def test_every_step_checks_clean() -> None:
    old, new = old_model(), new_model()
    renames = derive_renames(old, new)
    assert dict(renames.classes) == {"ex:Part": "aa:Part"}
    assert dict(renames.attributes) == {("ex:Item", "owner"): "custodian"}
    doc = parsed(ALL_STEPS)
    assert check_migration(doc, old, new, renames, language="en") == ()


def refused(text: str, path: str, fragment: str, *, language: str | None = "en") -> None:
    found = check(text, language=language)
    assert any(p == path and fragment in m for p, m in found), found


@pytest.mark.parametrize(
    ("step", "path", "fragment"),
    [
        (
            "retype: {from: 'ex:Nope', to: 'ex:Item'}",
            "steps[0].retype.from",
            "no class ex:Nope in the old model",
        ),
        (
            "retype: {from: 'ex:Gadget', to: 'ex:Gadget'}",
            "steps[0].retype.to",
            "no class ex:Gadget in the new model",
        ),
        ("retype: {from: 'ex:Gadget', to: 'ex:Shape'}", "steps[0].retype.to", "abstract"),
        ("retype: {from: 'ex:Part', to: 'ex:Item'}", "steps[0].retype.from", "renamed to aa:Part"),
        ("retype: {from: 'files:File', to: 'ex:Item'}", "steps[0].retype.from", "built-in term"),
        (
            "retype: {from: 'ex:Gadget', to: 'ex:Item', attributes: {nope: title}}",
            "steps[0].retype.attributes.nope",
            "no attribute 'nope' in the old model",
        ),
        (
            "retype: {from: 'ex:Gadget', to: 'ex:Item', attributes: {name: nope}}",
            "steps[0].retype.attributes.name",
            "no attribute 'nope' in the new model",
        ),
        (
            "fill: {type: 'ex:Item', attribute: nope, value: x}",
            "steps[0].fill.attribute",
            "no attribute 'nope'",
        ),
        (
            "fill: {type: 'ex:Missing', attribute: x, value: x}",
            "steps[0].fill.type",
            "no class ex:Missing in the new model",
        ),
        (
            "fill: {type: 'ex:Gadget', attribute: name, value: x}",
            "steps[0].fill.type",
            "no class ex:Gadget in the new model",
        ),
        (
            "fill: {type: 'files:File', attribute: name, value: x}",
            "steps[0].fill.type",
            "built-in term",
        ),
        (
            "fill: {type: 'ex:Item', attribute: priority, value: '9'}",
            "steps[0].fill.value",
            "greater than 5",
        ),
        (
            "fill: {type: 'ex:Item', attribute: priority, value: abc}",
            "steps[0].fill.value",
            "is not an integer",
        ),
        (
            "fill: {type: 'ex:Item', attribute: grade, value: nope}",
            "steps[0].fill.value",
            "is not one of new, used, broken",
        ),
        (
            "fill: {type: 'ex:Item', attribute: code, value: toolong}",
            "steps[0].fill.value",
            "longer than 4",
        ),
        (
            "map_values: {type: 'ex:Item', attribute: condition, values: {worn: gone}}",
            "steps[0].map_values.values.worn",
            "'gone' is not one of new, used, broken",
        ),
        (
            "map_values: {type: 'ex:Item', attribute: condition, values: {zzz: used}}",
            "steps[0].map_values.values.zzz",
            "'zzz' is not one of new, worn, broken in the old model",
        ),
        (
            "map_values: {type: 'ex:Item', attribute: state, values: {held: gone}}",
            "steps[0].map_values.values.held",
            "is not one of open, paused, closed",
        ),
        (
            "map_values: {type: 'ex:Item', attribute: title, values: {a: b}}",
            "steps[0].map_values.attribute",
            "not an enum or lifecycle",
        ),
        (
            "map_values: {type: 'ex:Item', attribute: nope, values: {a: b}}",
            "steps[0].map_values.attribute",
            "no attribute 'nope'",
        ),
        (
            "map_qualifier_values: {relation: 'ex:link', qualifier: kind, values: {heavy: huge}}",
            "steps[0].map_qualifier_values.values.heavy",
            "'huge' is not one of",
        ),
        (
            "map_qualifier_values: {relation: 'ex:link', qualifier: kind, values: {zzz: medium}}",
            "steps[0].map_qualifier_values.values.zzz",
            "in the old model",
        ),
        (
            "map_qualifier_values: {relation: 'ex:link', qualifier: note, values: {a: b}}",
            "steps[0].map_qualifier_values.qualifier",
            "not an enum qualifier",
        ),
        (
            "map_qualifier_values: {relation: 'ex:link', qualifier: nope, values: {a: b}}",
            "steps[0].map_qualifier_values.qualifier",
            "no qualifier 'nope'",
        ),
        (
            "map_qualifier_values: {relation: 'ex:gone', qualifier: kind, values: {a: b}}",
            "steps[0].map_qualifier_values.relation",
            "no relation ex:gone in the new model",
        ),
        (
            "drop: {type: 'ex:Item', attribute: nope}",
            "steps[0].drop.attribute",
            "no attribute 'nope'",
        ),
        ("drop: {type: 'ex:Item', attribute: owner}", "steps[0].drop.attribute", "is renamed"),
        (
            "drop: {type: 'ex:Fresh', attribute: legacy}",
            "steps[0].drop.type",
            "no counterpart in the old model",
        ),
        (
            "drop: {type: 'ex:Gadget', attribute: name}",
            "steps[0].drop.type",
            "no class ex:Gadget in the new model",
        ),
        (
            "drop_edges: {relation: 'ex:nope'}",
            "steps[0].drop_edges.relation",
            "no relation ex:nope in the old model",
        ),
        (
            "drop_edges: {relation: 'core:supersedes'}",
            "steps[0].drop_edges.relation",
            "built-in term",
        ),
        (
            "rename_qualifier: {relation: 'ex:link', from: nope, to: note}",
            "steps[0].rename_qualifier.from",
            "no qualifier 'nope'",
        ),
        (
            "rename_qualifier: {relation: 'ex:link', from: remark, to: nope}",
            "steps[0].rename_qualifier.to",
            "no qualifier 'nope' in the new model",
        ),
        (
            "rename_qualifier: {relation: 'ex:nope', from: a, to: b}",
            "steps[0].rename_qualifier.relation",
            "no relation ex:nope in the new model",
        ),
        (
            "drop_qualifier: {relation: 'ex:link', qualifier: nope}",
            "steps[0].drop_qualifier.qualifier",
            "no qualifier 'nope'",
        ),
        (
            "drop_qualifier: {relation: 'provenance:supported_by', qualifier: a}",
            "steps[0].drop_qualifier.relation",
            "built-in term",
        ),
        (
            "convert: {type: 'ex:Item', attribute: title, to: lang_string}",
            "steps[0].convert.to",
            "goes from string to string",
        ),
        (
            "convert: {type: 'ex:Item', attribute: body, to: lang_string}",
            "steps[0].convert.to",
            "goes from text to lang_text",
        ),
        (
            "convert: {type: 'ex:Item', attribute: tags, to: many}",
            "steps[0].convert.to",
            "from single to many",
        ),
        (
            "convert: {type: 'ex:Item', attribute: custodian, to: single}",
            "steps[0].convert.to",
            "from many to single",
        ),
        (
            "convert: {type: 'ex:Item', attribute: priority, to: many}",
            "steps[0].convert.attribute",
            "no counterpart in the old model",
        ),
        (
            "convert: {type: 'ex:Item', attribute: nope, to: many}",
            "steps[0].convert.attribute",
            "no attribute 'nope'",
        ),
    ],
)
def test_check_refusals_name_their_path(step: str, path: str, fragment: str) -> None:
    refused(f"steps:\n  - {step}\n", path, fragment)


def test_a_conversion_to_a_language_type_needs_a_graph_language() -> None:
    step = "steps:\n  - convert: {type: 'ex:Item', attribute: note, to: lang_string}\n"
    refused(step, "steps[0].convert.to", "needs a graph language", language=None)
    assert check(step) == []


def test_a_language_tagged_fill_value_needs_a_tag_without_a_graph_language() -> None:
    new = compile_modules(
        "example",
        {
            "example": header("example", "ex", "2.0.0")
            + OLD.replace(
                "note: {summary: Note., type: string}", "note: {summary: N., type: lang_string}"
            ).replace("      legacy: {summary: Legacy., type: string}\n", "")
        },
    )

    def run(value: str, language: str | None) -> list[str]:
        doc = parsed(
            PREFACE + f"steps:\n  - fill: {{type: 'ex:Item', attribute: note, value: {value}}}\n"
        )
        found = check_migration(doc, old_model(), new, Renames({}, {}, {}), language=language)
        return [p.message for p in found]

    assert run("plain", "en") == []
    assert any("language" in m for m in run("plain", None))
    assert run("'plain@en'", None) == []


def test_a_retype_from_class_that_is_renamed_is_refused_through_the_derived_renames() -> None:
    old, new = old_model(), new_model()
    doc = parsed(PREFACE + "steps:\n  - retype: {from: 'ex:Part', to: 'ex:Item'}\n")
    assert check_migration(doc, old, new, Renames({}, {}, {}), language=None) == ()
    assert check_migration(doc, old, new, derive_renames(old, new), language=None) != ()


def test_drop_edges_of_a_renamed_relation_is_refused() -> None:
    plain = (
        "classes:\n  A:\n    summary: A.\nrelations:\n"
        "  {name}: {{summary: R., domain: A, range: A{extra}}}\n"
    )
    old = compile_modules(
        "example",
        {"example": header("example", "ex", "1.0.0") + plain.format(name="link", extra="")},
    )
    new = compile_modules(
        "example",
        {
            "example": header("example", "ex", "2.0.0")
            + plain.format(name="join", extra=", renamed_from: link")
        },
    )
    doc = parsed(PREFACE + "steps:\n  - drop_edges: {relation: 'ex:link'}\n")
    found = check_migration(doc, old, new, derive_renames(old, new), language=None)
    assert [(p.path, "renamed to ex:join" in p.message) for p in found] == [
        ("steps[0].drop_edges.relation", True)
    ]


def test_the_keys_of_a_retype_are_checked_through_the_models_of_their_side() -> None:
    """A merge: the attribute map names an old attribute of the removed class and a new one
    of the target, and a later map_values step finds its old enum through the retype."""
    old, new = old_model(), new_model()
    found = check_migration(
        parsed(
            PREFACE
            + "steps:\n  - retype: {from: 'ex:Gadget', to: 'ex:Item', attributes: {name: title}}\n"
            "  - map_values: {type: 'ex:Item', attribute: condition, values: {worn: used}}\n"
        ),
        old,
        new,
        derive_renames(old, new),
        language=None,
    )
    assert found == ()
