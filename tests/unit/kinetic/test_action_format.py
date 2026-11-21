"""The action file format ``graph_cli.action/1``: shape, names, placeholders, problems."""

from support.actions import VALID, edited, valid_sources

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kinetic.action_format import (
    ActionDef,
    PreconditionDef,
    action_name,
    json_node,
    load_action,
    node_json,
    path_text,
    scan_op,
    spread_edges,
    substitute,
)
from graph_cli.kinetic.params import ParamDef

FILE = "assign_position.yaml"
TEXT = (VALID / FILE).read_text()


def codes(file_name: str, text: str) -> list[str]:
    defn, problems = load_action(file_name, text)
    assert defn is None
    return [p.code for p in problems]


def probe_ops(ops: str, params: str = "") -> str:
    return (
        "schema: graph_cli.action/1\nname: probe\nsummary: A probe.\nuse_when: Testing.\n"
        + (f"params:\n{params}" if params else "")
        + f"ops:\n{ops}"
        + "examples:\n  - request: A request.\n"
    )


def test_a_valid_file_is_read_in_full() -> None:
    defn, problems = load_action(FILE, TEXT)
    assert problems == []
    assert defn is not None
    assert (defn.name, defn.module, defn.featured, defn.status, defn.reason) == (
        "assign_position",
        "organization",
        True,
        "active",
        "optional",
    )
    assert defn.params[0] == ParamDef("person", "ref", "Who takes over the position.", "ppl:Person")
    assert [p.id for p in defn.preconditions] == ["position_open", "position_vacant"]
    assert defn.preconditions[1].on == "position"
    assert defn.ops == (
        {
            "op": "link",
            "id": "$person",
            "rel": "org:holds",
            "target": "$position",
            "qualifiers": {"since": "$since"},
        },
    )
    assert [e.request for e in defn.examples] == [
        "The new recruiter starts as the support lead on the first of March."
    ]


def test_every_valid_fixture_loads() -> None:
    for file_name, text in valid_sources().items():
        defn, problems = load_action(file_name, text)
        assert problems == [] and defn is not None and defn.name == action_name(file_name)


def test_reason_defaults_and_is_read() -> None:
    defn, _ = load_action("offboard_employee.yaml", (VALID / "offboard_employee.yaml").read_text())
    assert defn is not None and defn.reason == "required"
    assert codes(FILE, TEXT.replace("featured: true", "reason: maybe")) == ["action.invalid_value"]


def test_the_declaration_and_source_read_back_from_their_json_forms() -> None:
    for file_name, text in valid_sources().items():
        defn, _ = load_action(file_name, text)
        assert defn is not None
        assert ActionDef.from_json(defn.to_json(), defn.source_json()) == defn
    # the parsed conditions are rebuilt from the stored JSON, not stored
    defn, _ = load_action(FILE, TEXT)
    assert defn is not None
    again = ActionDef.from_json(defn.to_json(), defn.source_json())
    assert again.preconditions[0].conds == defn.preconditions[0].conds != ()


def test_conditions_go_through_the_ontology_parser_with_the_path_prefixed() -> None:
    bad = TEXT.replace("equals_string_in: [planned, active]", "equals_string_in: 3, nope: 1")
    _, problems = load_action(FILE, bad)
    assert problems and all(p.code == "action.invalid_condition" for p in problems)
    assert problems[0].path is not None and problems[0].path.startswith("preconditions[0].require")
    assert problems[0].message.startswith("preconditions[0].require")
    assert problems[0].kind == "action" and problems[0].name == "assign_position"


def test_a_precondition_needs_its_keys_and_a_unique_id() -> None:
    twice = TEXT.replace("id: position_vacant", "id: position_open")
    assert codes(FILE, twice) == ["action.invalid_value"]
    missing = TEXT.replace(
        "    on: position\n    summary: The position is planned",
        "    summary: The position is planned",
    )
    assert codes(FILE, missing) == ["action.missing_key"]
    defn, problems = load_action(
        FILE, TEXT[: TEXT.index("preconditions:")] + TEXT[TEXT.index("ops:") :]
    )
    assert problems == [] and defn is not None and defn.preconditions == ()


def test_name_summary_and_example_limits() -> None:
    assert codes(FILE, TEXT.replace("name: assign_position", "name: Assign")) == [
        "action.invalid_value"
    ]
    summary = "summary: Record that a person takes over a position nobody holds, from a date."
    assert codes(FILE, TEXT.replace(summary, f"summary: {'x' * 101}")) == ["action.invalid_value"]
    request = "The new recruiter starts as the support lead on the first of March."
    assert codes(FILE, TEXT.replace(request, "y" * 201)) == ["action.invalid_value"]
    # an example is a request in words; no parameters
    assert codes(FILE, TEXT.replace("  - request:", "  - params: {}\n    request:")) == [
        "action.unknown_key"
    ]


def test_ops_must_be_a_non_empty_list_of_known_ops() -> None:
    assert codes("probe.yaml", probe_ops("  - {op: purge, id: $a}\n")) == ["action.invalid_value"]
    assert codes("probe.yaml", probe_ops("  - {op: retract}\n")) == ["action.missing_key"]
    assert codes("probe.yaml", probe_ops("  - {op: retract, id: $a, colour: 1}\n")) == [
        "action.unknown_key"
    ]


def test_state_dependent_and_document_keys_are_refused() -> None:
    for key in (
        "expect_rev: 2",
        "target_rev: 2",
        "edge_id: x",
        "reason: why",
        "idempotency_key: k",
    ):
        text = probe_ops("  - {op: retract, id: $a, " + key + "}\n")
        assert codes("probe.yaml", text) == ["action.invalid_op"], key


def test_the_dynamic_unlink_has_exactly_its_keys() -> None:
    ok = probe_ops("  - {op: unlink, id: $a, rel: org:holds, all: true}\n")
    defn, problems = load_action("probe.yaml", ok)
    assert problems == [] and defn is not None
    assert codes(
        "probe.yaml", probe_ops("  - {op: unlink, id: $a, rel: org:holds, all: false}\n")
    ) == ["action.invalid_value"]
    assert codes(
        "probe.yaml",
        probe_ops("  - {op: unlink, id: $a, rel: org:holds, direction: up, all: true}\n"),
    ) == ["action.invalid_value"]
    assert codes(
        "probe.yaml", probe_ops("  - {op: unlink, id: $a, rel: org:holds, target: $b, all: true}\n")
    ) == ["action.unknown_key"]
    assert codes(
        "probe.yaml", probe_ops("  - {op: unlink, id: $a, rel: org:holds, direction: in}\n")
    ) == ["action.missing_key"]
    assert codes("probe.yaml", probe_ops("  - {op: unlink, id: $a, rel: org:holds}\n")) == [
        "action.invalid_op"
    ]


def test_entity_references_are_placeholders_or_locals() -> None:
    assert codes("probe.yaml", probe_ops("  - {op: retract, id: PER-0001}\n")) == [
        "action.literal_reference"
    ]
    assert codes(
        "probe.yaml", probe_ops("  - {op: retract, id: 0194f0f0-1234-7abc-8def-0123456789ab}\n")
    ) == ["action.literal_reference"]
    assert codes("probe.yaml", probe_ops("  - {op: retract, id: abcdef0123456789}\n")) == [
        "action.literal_reference"
    ]
    ok = probe_ops("  - {op: retract, id: '@made'}\n")
    assert load_action("probe.yaml", ok)[1] == []


def test_placeholders_stand_only_where_a_whole_reference_or_value_goes() -> None:
    for op in (
        "{op: create, type: $t}",
        "{op: link, id: $a, rel: $r, target: $b}",
        "{op: transition, id: $a, to: $s}",
        "{op: update, id: $a, set: {$name: 1}}",
        "{op: update, id: $a, unset: [$name]}",
        "{op: create, type: org:Role, local: $l}",
    ):
        found = codes("probe.yaml", probe_ops(f"  - {op}\n"))
        assert found and set(found) == {"action.placeholder_position"}, op
    assert codes("probe.yaml", probe_ops("  - {op: $kind, id: $a}\n")) == ["action.invalid_value"]


def test_placeholder_uses_describe_each_position() -> None:
    op = {
        "op": "create",
        "type": "org:Role",
        "local": "r",
        "attrs": {"name": "$n", "code": "x"},
        "attrs_text": {"since": "$t"},
        "edges": [{"rel": "org:carries", "target": "$p", "qualifiers": {"q": "$q"}}],
        "valid_from": "$from",
    }
    scan = scan_op(op, 2)
    by_path = {path_text(u.path): u for u in scan.uses}
    assert set(by_path) == {
        "ops[2].attrs.name",
        "ops[2].attrs_text.since",
        "ops[2].edges[0].target",
        "ops[2].edges[0].qualifiers.q",
        "ops[2].valid_from",
    }
    assert by_path["ops[2].attrs_text.since"].text
    assert by_path["ops[2].edges[0].target"].role == "entity"
    assert by_path["ops[2].edges[0].qualifiers.q"].rel == "org:carries"
    assert (
        by_path["ops[2].valid_from"].droppable and not by_path["ops[2].edges[0].target"].droppable
    )
    update = scan_op({"op": "update", "id": "$a", "add": {"tags": ["$x", "y"], "more": "$many"}}, 0)
    roles = {path_text(u.path): u.role for u in update.uses}
    assert roles == {
        "ops[0].id": "entity",
        "ops[0].add.tags[0]": "item",
        "ops[0].add.more": "items",
    }
    supersede = scan_op({"op": "supersede", "by": "$a", "targets": "$many"}, 0)
    assert {u.role for u in supersede.uses} == {"entity", "entities"}


def test_substitute_replaces_and_drops_entries() -> None:
    op: JsonObject = {
        "op": "update",
        "id": "$a",
        "set": {"title": "$t", "note": "$n"},
        "add": {"l": ["$x"]},
    }
    scan = scan_op(op, 0)
    values = {"a": "ID", "t": "Lead", "x": "one"}

    def value_of(use):
        from graph_cli.kinetic.action_format import ABSENT

        return values.get(use.name, ABSENT)

    assert substitute(op, scan.uses, value_of) == {
        "op": "update",
        "id": "ID",
        "set": {"title": "Lead"},
        "add": {"l": ["one"]},
    }
    assert op["set"] == {"title": "$t", "note": "$n"}  # the template is not changed


def test_a_list_target_spreads_into_one_edge_per_value() -> None:
    template: JsonObject = {
        "op": "create",
        "type": "ex:Thing",
        "edges": [
            {"rel": "ex:in", "target": "$places", "qualifiers": {"since": "2026-01-01"}},
            {"rel": "ex:near", "target": "@other"},
        ],
    }
    scan = scan_op(template, 0)
    assert scan.uses[0].field == "edges.target"
    spread = spread_edges(substitute(template, scan.uses, lambda use: ["B", "A", "B", "C"]))
    assert spread["edges"] == [
        {"rel": "ex:in", "target": "B", "qualifiers": {"since": "2026-01-01"}},
        {"rel": "ex:in", "target": "A", "qualifiers": {"since": "2026-01-01"}},
        {"rel": "ex:in", "target": "C", "qualifiers": {"since": "2026-01-01"}},
        {"rel": "ex:near", "target": "@other"},
    ]  # order kept, each value once, the other edge untouched
    assert spread["edges"][0]["qualifiers"] is not spread["edges"][1]["qualifiers"]  # type: ignore[index]
    empty = spread_edges(substitute(template, scan.uses, lambda use: []))
    assert empty["edges"] == [{"rel": "ex:near", "target": "@other"}]  # no value, no edge


def test_spreading_changes_nothing_else() -> None:
    single: JsonObject = {
        "op": "create",
        "type": "ex:T",
        "edges": [{"rel": "ex:in", "target": "A"}],
    }
    assert spread_edges(single) is single
    bare: JsonObject = {"op": "create", "type": "ex:T"}
    assert spread_edges(bare) is bare
    link: JsonObject = {"op": "link", "id": "A", "rel": "ex:in", "target": ["B", "C"]}
    assert spread_edges(link) is link  # only a create has edges to spread


def test_yaml_nodes_and_json_convert_both_ways() -> None:
    value = {"a": [1, "2", True, None, "true"], "b": {"c": "x"}}
    assert node_json(json_node(value)) == value


def test_a_precondition_def_compares_without_its_parsed_conditions() -> None:
    a = PreconditionDef("i", "p", "s", [{"attr": "x"}])
    assert a == PreconditionDef("i", "p", "s", [{"attr": "x"}], conds=())


def test_edited_fixture_helper_still_loads() -> None:
    changed = edited(FILE, "name: assign_position", "name: assign_position ")
    assert FILE in changed
