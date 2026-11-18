"""The view file format ``graph_cli.view/1``: shape, flavours, limits, problems."""

import pytest
from support.views import BROKEN_VIEWS, VALID, read_dir

from graph_cli.kinetic import card
from graph_cli.kinetic.view_format import (
    ENTITY_MAX_BYTES,
    FIELDS_MAX_DEFAULT,
    GROUPS_MAX_DEFAULT,
    ITEMS_MAX_DEFAULT,
    VALUE_CHARS_DEFAULT,
    AffordancesSection,
    FieldsSection,
    FilesSection,
    ListSection,
    NeighborsSection,
    RelationRef,
    RelationsSection,
    RowRelation,
    RowsSection,
    ViewDef,
    load_view,
    placeholder_name,
    view_name,
    where_parts,
)

ENTITY_HEAD = (
    "schema: graph_cli.view/1\nname: probe\nmodule: organization\nsummary: A probe.\n"
    "use_when: Testing.\ntakes: org:Team\n"
)
SET_HEAD = (
    "schema: graph_cli.view/1\nname: probe\nmodule: operations\nsummary: A probe.\n"
    "use_when: Testing.\n"
)


def parsed(text: str) -> ViewDef:
    defn, problems = load_view("probe.yaml", text)
    assert [p.code for p in problems] == []
    assert defn is not None
    return defn


def codes(text: str, file_name: str = "probe.yaml") -> list[str]:
    defn, problems = load_view(file_name, text)
    assert defn is None
    return [p.code for p in problems]


def entity(sections: str, extra: str = "") -> str:
    return ENTITY_HEAD + extra + "sections:\n" + sections


def setview(sections: str, extra: str = "") -> str:
    return SET_HEAD + extra + "sections:\n" + sections


def test_the_entity_limit_is_the_cards_hard_limit() -> None:
    assert ENTITY_MAX_BYTES == card.BYTE_LIMIT


def test_every_valid_fixture_loads_and_reads_back_from_json() -> None:
    sources = read_dir(VALID)
    assert len(sources) >= 6
    for file_name, text in sources.items():
        defn, problems = load_view(file_name, text)
        assert problems == [] and defn is not None and defn.name == view_name(file_name)
        assert ViewDef.from_json(defn.to_json()) == defn


def test_an_entity_view_is_read_in_full() -> None:
    defn, _ = load_view("unit_overview.yaml", (VALID / "unit_overview.yaml").read_text())
    assert defn is not None
    assert (defn.name, defn.module, defn.takes, defn.default, defn.entity) == (
        "unit_overview",
        "organization",
        "org:OrgUnit",
        True,
        True,
    )
    assert (defn.max_bytes, defn.page_bytes, defn.status) == (4096, None, "active")
    assert [s.kind for s in defn.sections] == [
        "fields",
        "relations",
        "neighbors",
        "rows",
        "files",
        "affordances",
    ]
    fields, relations, neighbors, rows, files, affordances = defn.sections
    assert fields == FieldsSection("fields", None, ("name", "alt_labels"), 300, 2)
    assert relations == RelationsSection(
        "relations",
        None,
        (
            RelationRef("org:charged_to", "out"),
            RelationRef("org:part_of", "in"),
            RelationRef("ops:applies_to", "in"),
        ),
        4,
        3,
    )
    assert neighbors == NeighborsSection(
        id="positions",
        relation="org:in_unit",
        title="Positions",
        direction="in",
        where=("status=$status",),
        order=("title",),
        fields=("title", "status"),
        relations=(RowRelation("org:holds", "in", ("work_email",), (), (), 5),),
        max=20,
    )
    assert rows == RowsSection("members", "members_of_unit", None, {"unit": "$entity"}, 10)
    assert files == FilesSection("files") and affordances == AffordancesSection("affordances")
    (status,) = defn.params
    assert (status.name, status.default, status.required) == ("status", "active", False)
    assert defn.examples[0].params == {"status": "active"}


def test_a_set_view_is_read_in_full() -> None:
    defn, _ = load_view("org_catalog.yaml", (VALID / "org_catalog.yaml").read_text())
    assert defn is not None
    assert (defn.takes, defn.entity, defn.default, defn.page_bytes) == (None, False, False, 65536)
    units, positions, people, completions = defn.sections
    assert units == ListSection(
        "units", ("org:OrgUnit",), None, (), (), "all", (RowRelation(None),)
    )
    assert isinstance(positions, ListSection)
    assert positions.relations == (
        RowRelation(None),
        RowRelation("org:holds", "in", ("name",), ("work_email:exists=true",), (), 50),
    )
    assert isinstance(people, ListSection) and people.types == ("ppl:Employee", "ppl:External")
    assert completions == RowsSection(
        "completions",
        "training_completions",
        None,
        {"titles": ["Safety", "Security"], "since": "$since"},
        None,
    )


def test_a_section_id_defaults_to_the_kind_and_must_be_unique() -> None:
    defn = parsed(entity("  - kind: fields\n    fields: [name]\n"))
    assert defn.sections[0].id == "fields"
    assert codes(entity("  - kind: fields\n  - kind: fields\n")) == ["view.duplicate_section"]
    assert codes(entity("  - kind: files\n  - kind: fields\n    id: files\n")) == [
        "view.duplicate_section"
    ]
    two = entity(
        "  - kind: fields\n    id: one\n    fields: [name]\n"
        "  - kind: fields\n    id: two\n    fields: [name]\n"
    )
    assert [s.id for s in parsed(two).sections] == ["one", "two"]
    assert codes(entity("  - kind: files\n    id: Bad-Id\n")) == ["view.invalid_value"]


def test_fields_may_be_all_names_or_left_out() -> None:
    assert parsed(entity("  - kind: fields\n")).sections[0] == FieldsSection()
    assert parsed(entity("  - kind: fields\n    fields: all\n")).sections[0] == FieldsSection(
        fields="all"
    )
    named = parsed(entity("  - kind: fields\n    fields: [name, title]\n"))
    assert named.sections[0] == FieldsSection(fields=("name", "title"), max=2)
    assert codes(entity("  - kind: fields\n    fields: everything\n")) == ["view.invalid_value"]
    assert codes(entity("  - kind: fields\n    fields: [name, name]\n")) == ["view.invalid_value"]
    assert codes(entity("  - kind: fields\n    fields: []\n")) == ["view.invalid_value"]
    assert codes(entity("  - kind: fields\n    fields: [Name]\n")) == ["view.invalid_value"]


def test_row_relations_take_all_or_a_list() -> None:
    one = parsed(setview("  - kind: list\n    type: ops:Task\n    relations: all\n"))
    many = parsed(
        setview(
            "  - kind: list\n    type: ops:Task\n    relations:\n"
            "      - all\n      - {relation: ops:of_process, fields: all, max: 7}\n"
        )
    )
    assert one.sections[0].to_json()["relations"] == [RowRelation().to_json()]
    assert isinstance(many.sections[0], ListSection)
    assert many.sections[0].relations == (
        RowRelation(None),
        RowRelation("ops:of_process", "out", "all", (), (), 7),
    )
    twice = setview("  - kind: list\n    type: ops:Task\n    relations: [all, all]\n")
    assert codes(twice) == ["view.invalid_value"]
    same = setview(
        "  - kind: list\n    type: ops:Task\n    relations: [ops:of_process, ops:of_process]\n"
    )
    # a bare name is no item here (only all and mappings are): one problem per item
    assert codes(same) == ["view.invalid_value", "view.invalid_value"]
    both = setview(
        "  - kind: list\n    type: ops:Task\n    relations:\n"
        "      - {relation: ops:of_process}\n      - {relation: ops:of_process}\n"
    )
    assert codes(both) == ["view.invalid_value"]
    over = "  - kind: list\n    type: ops:Task\n    relations:\n      - {relation: x:y, max: 501}\n"
    assert codes(setview(over)) == ["view.invalid_value"]


def test_type_is_one_class_or_a_list() -> None:
    one = parsed(setview("  - kind: list\n    type: ops:Task\n"))
    assert isinstance(one.sections[0], ListSection) and one.sections[0].types == ("ops:Task",)
    assert codes(setview("  - kind: list\n")) == ["view.missing_key"]
    assert codes(setview("  - kind: list\n    type: [ops:Task, ops:Task]\n")) == [
        "view.invalid_value"
    ]


def test_every_kind_of_the_other_flavour_is_refused() -> None:
    for kind in ("list",):
        assert codes(entity(f"  - kind: {kind}\n    type: ops:Task\n")) == ["view.flavour_mismatch"]
    only_entity = {
        "fields": "    fields: [name]\n",
        "relations": "",
        "files": "",
        "affordances": "",
        "neighbors": "    relation: org:in_unit\n",
    }
    for kind, body in only_entity.items():
        assert codes(setview(f"  - kind: {kind}\n{body}")) == ["view.flavour_mismatch"], kind


def test_keys_of_the_other_flavour_are_refused() -> None:
    assert codes(setview("  - kind: list\n    type: ops:Task\n", "default: true\n")) == [
        "view.flavour_mismatch"
    ]
    assert codes(setview("  - kind: list\n    type: ops:Task\n", "max_bytes: 2048\n")) == [
        "view.flavour_mismatch"
    ]
    assert codes(entity("  - kind: files\n", "page_bytes: 2048\n")) == ["view.flavour_mismatch"]
    rows = "  - kind: rows\n    function: some_function\n    max: 5\n"
    assert codes(setview(rows)) == ["view.flavour_mismatch"]
    assert parsed(entity(rows)).sections[0] == RowsSection("rows", "some_function", max=5)
    assert parsed(setview(rows.replace("    max: 5\n", ""))).sections[0] == RowsSection(
        "rows", "some_function"
    )


def test_defaults_of_the_entity_sections() -> None:
    assert parsed(entity("  - kind: relations\n")).sections[0] == RelationsSection()
    rows = parsed(entity("  - kind: rows\n    function: some_function\n")).sections[0]
    assert isinstance(rows, RowsSection) and rows.max == 10
    neighbors = parsed(entity("  - kind: neighbors\n    relation: org:in_unit\n")).sections[0]
    assert isinstance(neighbors, NeighborsSection)
    assert (neighbors.max, neighbors.direction, neighbors.fields, neighbors.relations) == (
        10,
        "out",
        None,
        (),
    )


@pytest.mark.parametrize(
    ("sections", "extra"),
    [
        ("  - kind: fields\n    value_chars: 2001\n", ""),
        ("  - kind: fields\n    max: 33\n", ""),
        ("  - kind: fields\n    max: 0\n", ""),
        ("  - kind: relations\n    max_groups: 9\n", ""),
        ("  - kind: relations\n    max_items: 11\n", ""),
        ("  - kind: neighbors\n    relation: org:in_unit\n    max: 51\n", ""),
        ("  - kind: rows\n    function: some_function\n    max: 51\n", ""),
        ("  - kind: files\n", "max_bytes: 6145\n"),
        ("  - kind: files\n", "max_bytes: 100\n"),
        ("  - kind: files\n    title: " + "t" * 101 + "\n", ""),
        ("  - kind: files\n", "summary: " + "s" * 101 + "\n"),
        ("  - kind: fields\n    max: many\n", ""),
    ],
)
def test_a_value_above_its_limit_is_refused(sections: str, extra: str) -> None:
    text = ENTITY_HEAD.replace(
        "summary: A probe.\n", "" if "summary" in extra else "summary: A probe.\n"
    )
    assert codes(text + extra + "sections:\n" + sections) == ["view.invalid_value"]


def test_a_set_view_page_limit() -> None:
    body = "  - kind: list\n    type: ops:Task\n"
    assert parsed(setview(body, "page_bytes: 262144\n")).page_bytes == 262_144
    assert codes(setview(body, "page_bytes: 262145\n")) == ["view.invalid_value"]


def test_the_section_count_is_limited() -> None:
    def files(count: int) -> str:
        return "".join(f"  - kind: fields\n    id: s{i}\n" for i in range(count))

    assert len(parsed(entity(files(32))).sections) == 32
    assert codes(entity(files(33))) == ["view.invalid_value"]
    assert codes(entity("  []\n")) == ["view.invalid_value"]  # an empty list is refused


def test_where_and_order_are_lists_of_strings() -> None:
    text = setview(
        '  - kind: list\n    type: ops:Task\n    where: ["status=$s", "due_on:lt=2026-01-01"]\n'
        '    order: [due_on, "title:desc"]\n'
    )
    section = parsed(text).sections[0]
    assert isinstance(section, ListSection)
    assert section.where == ("status=$s", "due_on:lt=2026-01-01")
    assert section.order == ("due_on", "title:desc")
    assert codes(setview("  - kind: list\n    type: ops:Task\n    where: status=open\n")) == [
        "view.invalid_value"
    ]
    nine = ", ".join(f"a{i}=1" for i in range(9))
    assert codes(setview(f"  - kind: list\n    type: ops:Task\n    where: [{nine}]\n")) == [
        "view.invalid_value"
    ]


def test_args_are_literals_lists_or_placeholders() -> None:
    text = entity(
        "  - kind: rows\n    function: some_function\n    args:\n"
        "      a: $entity\n      b: 5\n      c: true\n      d: [x, y]\n      e: text\n"
    )
    rows = parsed(text).sections[0]
    assert isinstance(rows, RowsSection)
    assert rows.args == {"a": "$entity", "b": 5, "c": True, "d": ["x", "y"], "e": "text"}
    nested = entity("  - kind: rows\n    function: f_one\n    args: {a: {b: 1}}\n")
    assert codes(nested) == ["view.invalid_value"]
    assert codes(entity("  - kind: rows\n    function: f_one\n    args: {A: 1}\n")) == [
        "view.invalid_value"
    ]
    assert codes(entity("  - kind: rows\n    function: f_one\n    args: [a]\n")) == [
        "view.invalid_value"
    ]


def test_the_view_parameter_grammar_is_the_function_one() -> None:
    text = entity(
        "  - kind: files\n",
        "params:\n  kind:\n    type: enum\n    enum: ppl:ExternalKind\n    many: true\n"
        "    summary: Kinds.\n  since:\n    type: date\n    default: 2026-01-01\n"
        "    summary: Day.\n",
    )
    kinds, since = parsed(text).params
    assert (kinds.type, kinds.enum, kinds.many, kinds.required) == (
        "enum",
        "ppl:ExternalKind",
        True,
        True,
    )
    assert (since.type, since.default, since.required) == ("date", "2026-01-01", False)
    reserved = entity(
        "  - kind: files\n", "params:\n  entity:\n    type: string\n    summary: S.\n"
    )
    assert codes(reserved) == ["view.invalid_value"]
    missing = entity("  - kind: files\n", "params:\n  p:\n    type: string\n")
    assert codes(missing) == ["view.missing_key"]
    values = entity(
        "  - kind: files\n", "params:\n  p:\n    type: enum\n    values: [a]\n    summary: S.\n"
    )
    assert sorted(codes(values)) == ["view.missing_key", "view.unknown_key"]


def test_deprecation_and_replacement() -> None:
    old = entity("  - kind: files\n", "status: deprecated\nreplaced_by: other_view\n")
    defn = parsed(old)
    assert (defn.status, defn.replaced_by) == ("deprecated", "other_view")
    assert codes(entity("  - kind: files\n", "replaced_by: other_view\n")) == ["view.invalid_value"]
    assert codes(entity("  - kind: files\n", "status: deprecated\nreplaced_by: probe\n")) == [
        "view.invalid_value"
    ]
    assert codes(entity("  - kind: files\n", "status: retired\n")) == ["view.invalid_value"]


def test_the_default_flag_is_read() -> None:
    assert parsed(entity("  - kind: files\n", "default: true\n")).default is True
    assert parsed(entity("  - kind: files\n", "default: false\n")).default is False
    assert parsed(entity("  - kind: files\n")).default is False
    assert codes(entity("  - kind: files\n", "default: maybe\n")) == ["view.invalid_value"]


def test_every_broken_fixture_that_the_parser_refuses() -> None:
    expected = {
        "invalid_yaml": "view.invalid_yaml",
        "missing_key": "view.missing_key",
        "unknown_key": "view.unknown_key",
        "invalid_value": "view.invalid_value",
        "unsupported_schema": "view.unsupported_schema",
        "name_mismatch": "view.name_mismatch",
        "card": "view.reserved_name",
        "flavour_mismatch": "view.flavour_mismatch",
        "duplicate_section": "view.duplicate_section",
    }
    sources = read_dir(BROKEN_VIEWS)
    for case, code in expected.items():
        assert codes(sources[f"{case}.yaml"], f"{case}.yaml") == [code], case


def test_a_problem_names_view_path_and_line() -> None:
    _, problems = load_view("probe.yaml", entity("  - kind: fields\n    max: 99\n"))
    (problem,) = problems
    assert (problem.name, problem.kind, problem.code, problem.path) == (
        "probe",
        "view",
        "view.invalid_value",
        "sections[0].max",
    )
    assert problem.line == 9
    assert problem.to_json()["view"] == "probe"


def test_an_empty_file_and_a_foreign_root_are_refused() -> None:
    assert codes("") == ["view.invalid_yaml"]
    assert codes("- a\n- b\n") == ["view.invalid_value"]


def test_all_problems_of_a_file_come_in_one_answer() -> None:
    text = ENTITY_HEAD + "colour: red\nmax_bytes: 9000\nsections:\n  - kind: fields\n    max: 99\n"
    assert sorted(codes(text)) == ["view.invalid_value", "view.invalid_value", "view.unknown_key"]


def test_placeholders_are_whole_values_only() -> None:
    assert placeholder_name("$status") == "status"
    assert placeholder_name("$entity") == "entity"
    for value in ("status", "$", "$Status", "x$y", "$a b", 5, None, ["$a"], "$1a"):
        assert placeholder_name(value) is None, value
    assert where_parts("status=$state") == ("status", "state")
    assert where_parts("due_on:lt=$before") == ("due_on:lt", "before")
    assert where_parts("status=open") == ("status", None)
    assert where_parts("title=a $state b") == ("title", None)
    assert where_parts("title=$state$x") == ("title", None)
    assert where_parts("exists") == ("exists", None)


def test_a_placeholder_must_be_a_whole_value() -> None:
    sections = "  - kind: list\n    type: ops:Task\n    where: [{entry}]\n"
    ok = setview(sections.format(entry='"status=$s"'))
    assert parsed(ok).sections[0].to_json()["where"] == ["status=$s"]
    for entry in ('"title:prefix=a$s"', '"status:in=$s,x"', '"status:in=$a,$b"', '"title=x $s y"'):
        assert codes(setview(sections.format(entry=entry))) == ["view.placeholder_position"], entry
    # a literal that merely looks like one after another character is a literal
    assert parsed(setview(sections.format(entry='"title=$"'))).name == "probe"
    assert parsed(setview(sections.format(entry='"title=$S"'))).name == "probe"


def test_a_placeholder_in_an_argument_must_be_the_whole_argument() -> None:
    rows = "  - kind: rows\n    function: f_one\n    args: {{a: {value}}}\n"
    assert parsed(entity(rows.format(value="$p"))).name == "probe"
    for value in ('"x $p"', "[$p]", '["a", "$p"]', '"$p-1"'):
        assert codes(entity(rows.format(value=value))) == ["view.placeholder_position"], value
    assert parsed(entity(rows.format(value='"$P"'))).name == "probe"  # not a placeholder


def test_a_fields_list_shows_all_its_names_unless_max_says_otherwise() -> None:
    names = "[name, title, status]"
    section = parsed(entity(f"  - kind: fields\n    fields: {names}\n")).sections[0]
    assert isinstance(section, FieldsSection) and section.max == 3  # not the default of 8
    assert (
        parsed(entity(f"  - kind: fields\n    fields: {names}\n    max: 5\n"))
        .sections[0]
        .to_json()["max"]
        == 5
    )
    short = f"  - kind: fields\n    fields: {names}\n    max: 2\n"
    assert codes(entity(short)) == ["view.invalid_value"]
    many = ", ".join(f"a{i}" for i in range(33))
    assert codes(entity(f"  - kind: fields\n    fields: [{many}]\n")) == ["view.invalid_value"]
    thirty_two = ", ".join(f"a{i}" for i in range(32))
    listed = parsed(entity(f"  - kind: fields\n    fields: [{thirty_two}]\n")).sections[0]
    assert isinstance(listed, FieldsSection) and listed.max == 32
    for other in ("all", ""):  # all and the generated choice keep the default of 8
        body = "  - kind: fields\n" + (f"    fields: {other}\n" if other else "")
        assert parsed(entity(body)).sections[0].to_json()["max"] == 8


def test_a_deprecated_view_cannot_be_the_default() -> None:
    body = "  - kind: files\n"
    assert codes(entity(body, "status: deprecated\ndefault: true\n")) == ["view.invalid_value"]
    assert parsed(entity(body, "status: deprecated\ndefault: false\n")).default is False


def test_a_list_names_at_most_sixteen_classes() -> None:
    many = ", ".join(f"ops:C{i}" for i in range(17))
    assert codes(setview(f"  - kind: list\n    type: [{many}]\n")) == ["view.invalid_value"]
    sixteen = ", ".join(f"ops:C{i}" for i in range(16))
    assert (
        len(
            parsed(setview(f"  - kind: list\n    type: [{sixteen}]\n"))
            .sections[0]
            .to_json()["type"]
        )
        == 16
    )  # type: ignore[arg-type]


def test_the_defaults_are_the_cards() -> None:
    assert (FIELDS_MAX_DEFAULT, VALUE_CHARS_DEFAULT) == (card.FIELDS_MAX, card.VALUE_CHARS)
    assert (GROUPS_MAX_DEFAULT, ITEMS_MAX_DEFAULT) == (card.GROUPS_MAX, card.ITEMS_MAX)


def test_an_entity_view_has_at_most_one_files_and_one_affordances_section() -> None:
    for kind in ("files", "affordances"):
        two = f"  - kind: {kind}\n  - kind: {kind}\n    id: again\n"
        assert codes(entity(two)) == ["view.duplicate_section"], kind
    one_each = "  - kind: files\n  - kind: affordances\n"
    assert [s.kind for s in parsed(entity(one_each)).sections] == ["files", "affordances"]
