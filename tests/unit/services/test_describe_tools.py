"""The per-entry half of the tool manifest (tool manifest plan TM4, TM5): tool names including the
long form, the input schema of every parameter type, the controls of each kind of tool,
``_reason`` by obligation, descriptions, and what a caller who sees less is given (the enum values
and classes of the model it sees; entries that name something hidden do not appear)."""

import re
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support import tools
from support.tools import ITEMS, NOTHING, PERSONAL, Scene

from graph_cli.kernel.canonical import MAX_SAFE_INT
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.params import PARAM_TYPES, ParamDef, bind_json
from graph_cli.kinetic.return_types import ColumnType
from graph_cli.kinetic.tool_types import param_type
from graph_cli.services import describe_tools as dt

COMPANY = tools.company()
INVENTORY = tools.inventory()
STAFF = tools.staff()
FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER


def heads(scene: Scene, obligations: frozenset[str] = frozenset()) -> dict[str, dt.ToolHead]:
    found = [
        *(dt.function_head(e, scene.model, scene.functions) for e in scene.functions),
        *(dt.action_head(e, scene.model, scene.actions, obligations) for e in scene.actions),
        *(dt.view_head(e, scene.model, scene.views) for e in scene.views),
    ]
    dt.unique_names(found)
    return {h.name: h for h in found}


def props(head: dt.ToolHead) -> dict[str, Any]:
    found: dict[str, Any] = head.input_schema["properties"]  # type: ignore[assignment]
    return found


def required(head: dt.ToolHead) -> list[str]:
    found: list[str] = head.input_schema["required"]  # type: ignore[assignment]
    return found


# ---- names -------------------------------------------------------------------------------


def test_a_tool_is_named_by_the_kind_and_the_entry() -> None:
    assert dt.tool_name("function", "members_of_unit") == "run_members_of_unit"
    assert dt.tool_name("action", "assign_position") == "do_assign_position"
    assert dt.tool_name("view", "unit_overview") == "view_unit_overview"


def test_a_view_and_the_function_it_runs_do_not_clash() -> None:
    names = heads(INVENTORY.scene())
    assert {"run_items_in_location", "view_items_in_location"} <= set(names)
    assert names["run_items_in_location"].kind == "function"
    assert names["view_items_in_location"].kind == "view"
    assert names["view_items_in_location"].entry == "items_in_location"


def test_every_name_is_an_identifier_within_the_limit() -> None:
    for catalog in (COMPANY, INVENTORY):
        for name in heads(catalog.scene()):
            assert re.fullmatch(r"[a-z][a-z0-9_]*", name) and len(name) <= dt.MAX_TOOL_NAME


def test_a_name_that_fits_is_never_cut() -> None:
    fits = "a" * (dt.MAX_TOOL_NAME - len("view_"))
    assert dt.tool_name("view", fits) == f"view_{fits}"
    assert len(dt.tool_name("view", fits)) == dt.MAX_TOOL_NAME


def test_a_long_name_keeps_its_first_characters_and_ends_in_a_digest() -> None:
    longest = "a" * 63  # the entry name grammar allows 63 characters
    for kind, prefix in dt.PREFIXES.items():
        name = dt.tool_name(kind, longest)
        assert len(name) == dt.MAX_TOOL_NAME == 64 or len(prefix + longest) <= 64
        if len(prefix + longest) > 64:
            assert re.fullmatch(rf"{prefix}a+_[0-9a-f]{{8}}", name)
            assert name.startswith(prefix + "a" * 20)


def test_long_names_with_the_same_start_get_different_tools_and_the_name_is_stable() -> None:
    first = dt.tool_name("view", "v" * 62 + "ab")[:0] or dt.tool_name("view", "v" * 61 + "ab")
    second = dt.tool_name("view", "v" * 61 + "ac")
    assert first != second and first[:-8] == second[:-8]
    assert dt.tool_name("view", "v" * 61 + "ab") == first


def test_two_tools_with_one_name_are_refused() -> None:
    head = next(iter(heads(COMPANY.scene()).values()))
    with pytest.raises(GraphCliError) as info:
        dt.unique_names([head, head])
    assert info.value.code == "internal.error"
    other = replace(head, kind="view", entry="other")
    with pytest.raises(GraphCliError):
        dt.unique_names([head, other])


# ---- the schema of each parameter type -------------------------------------------------------


def schema_of(type_: str, **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"name": "p", "type": type_, "summary": "A parameter."} | extra
    if type_ == "ref":
        fields.setdefault("of_class", "ppl:Person")
    if type_ == "enum":
        fields.setdefault("enum", "ppl:ExternalKind")
    return dt.param_schema(param_type(ParamDef(**fields)), COMPANY.model)  # type: ignore[return-value]


def test_a_string_and_a_boolean() -> None:
    assert schema_of("string") == {"type": "string", "description": "A parameter."}
    assert schema_of("boolean") == {"type": "boolean", "description": "A parameter."}


def test_an_integer_has_the_safe_range() -> None:
    assert schema_of("integer") == {
        "type": "integer",
        "minimum": -MAX_SAFE_INT,
        "maximum": MAX_SAFE_INT,
        "description": "A parameter.",
    }


def test_a_decimal_is_text_with_the_grammar_of_the_parser() -> None:
    found = schema_of("decimal")
    assert found["type"] == "string" and "format" not in found
    assert re.fullmatch(found["pattern"], "-12.50") and not re.fullmatch(found["pattern"], "1e3")


def test_a_date_and_a_timestamp_say_their_format_and_their_pattern() -> None:
    date = schema_of("date")
    assert (date["type"], date["format"]) == ("string", "date")
    assert re.fullmatch(date["pattern"], "2026-11-25")
    stamp = schema_of("datetime")
    assert (stamp["type"], stamp["format"]) == ("string", "date-time")
    assert re.fullmatch(stamp["pattern"], "2026-11-25T10:00:00Z")
    assert not re.fullmatch(stamp["pattern"], "2026-11-25T10:00:00")  # the offset is required


def test_an_enum_lists_the_values_of_the_model_and_tells_their_summaries() -> None:
    found = schema_of("enum")
    assert found["type"] == "string"
    assert found["enum"] == ["contractor", "consultant", "auditor"]
    assert found["description"].startswith("A parameter. Values: contractor - Works on")
    assert "auditor - Examines the company" in found["description"]


def test_an_enum_whose_values_have_no_summaries_adds_no_list_to_the_description() -> None:
    plain = replace(
        COMPANY.model,
        enums={
            "ppl:ExternalKind": replace(
                COMPANY.model.enums["ppl:ExternalKind"],
                values=tuple(
                    replace(v, summary=None) for v in COMPANY.model.enums["ppl:ExternalKind"].values
                ),
            )
        },
    )
    found = dt.param_schema(
        param_type(ParamDef("p", "enum", "A parameter.", enum="ppl:ExternalKind")), plain
    )
    assert found["description"] == "A parameter." and found["enum"] == [
        "contractor",
        "consultant",
        "auditor",
    ]


def test_an_enum_the_model_lacks_stays_a_string() -> None:
    """An entry that no longer fits the ontology is listed; its call is ``*.stale``."""
    found = schema_of("enum", enum="ppl:NoSuchEnum")
    assert found == {"type": "string", "description": "A parameter."}


def test_a_reference_names_its_class_and_takes_ids_aliases_and_hex_digits() -> None:
    found = schema_of("ref")
    assert found["type"] == "string" and found[dt.EXTENSION] == {"class": "ppl:Person"}
    assert "ppl:Person" in found["description"] and "looked up" in found["description"]
    for text in ("ORG-0012", "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a", "1f0e5d6c7b8a"):
        assert re.fullmatch(found["pattern"], text), text
    assert not re.fullmatch(found["pattern"], "Ada Lovelace")


def test_many_wraps_the_schema_of_one_value_in_a_list_of_at_least_one() -> None:
    found = schema_of("ref", many=True)
    assert found["type"] == "array" and found["minItems"] == 1
    assert (
        found["items"][dt.EXTENSION] == {"class": "ppl:Person"}
        and found["items"]["type"] == "string"
    )
    assert found["description"].startswith("A parameter.")
    assert "description" not in found["items"]


def test_a_default_is_set_and_a_parameter_with_one_is_not_required() -> None:
    assert schema_of("string", default="x")["default"] == "x"
    assert schema_of("decimal", default=5)["default"] == "5"
    assert schema_of("string", many=True, default=["a"])["default"] == ["a"]


def test_an_optional_parameter_accepts_null_for_not_given_and_a_defaulted_one_does_not() -> None:
    optional = schema_of("string", optional=True)
    assert optional == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "A parameter.",
    }
    assert "anyOf" not in schema_of("string")
    assert "anyOf" not in schema_of("string", default="x")
    many = schema_of("string", many=True, optional=True)
    assert many["anyOf"][0]["type"] == "array" and many["anyOf"][1] == {"type": "null"}


def test_every_parameter_type_is_covered() -> None:
    for type_ in PARAM_TYPES:
        assert schema_of(type_)


# ---- the input schemas of the fixture catalogs -----------------------------------------------


def all_heads() -> list[dt.ToolHead]:
    return [h for catalog in (COMPANY, INVENTORY) for h in heads(catalog.scene()).values()]


def test_an_input_schema_is_a_closed_object_and_a_valid_schema() -> None:
    for head in [*all_heads(), *heads(STAFF.scene()).values()]:
        Draft202012Validator.check_schema(head.input_schema)
        assert head.input_schema["type"] == "object"
        assert head.input_schema["additionalProperties"] is False
        assert set(required(head)) <= set(props(head))


def test_a_function_takes_its_parameters_then_limit_and_offset() -> None:
    head = heads(COMPANY.scene())["run_training_completions"]
    assert list(props(head)) == ["titles", "since", "_limit", "_offset"]
    assert required(head) == ["titles"]
    assert props(head)["_limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 500,
        "description": "Rows per page, at most 500.",
        "default": 50,
    }
    assert props(head)["_offset"]["default"] == 0 and props(head)["_offset"]["minimum"] == 0


def test_an_action_takes_its_parameters_then_reason_and_dry_run() -> None:
    head = heads(COMPANY.scene())["do_assign_position"]
    assert list(props(head)) == ["person", "position", "since", "_reason", "_dry_run"]
    assert required(head) == ["person", "position", "since"]
    assert props(head)["_dry_run"]["default"] is False
    reason = props(head)["_reason"]
    assert reason["anyOf"][1] == {"type": "null"}
    assert reason["anyOf"][0]["minLength"] == 1 and reason["anyOf"][0]["maxLength"] == 2000


def test_a_reason_of_blanks_is_not_valid_input() -> None:
    """The binder refuses a blank reason, so the schema must not accept one."""
    head = heads(COMPANY.scene())["do_offboard_employee"]
    validator = input_validator(head)
    base = {"employee": "ORG-0001"}
    assert list(validator.iter_errors({**base, "_reason": "left"})) == []
    for blank in ("", " ", "   ", "\t\n"):
        assert list(validator.iter_errors({**base, "_reason": blank})), repr(blank)
    assert list(validator.iter_errors({**base, "_reason": " left "})) == []


def test_an_entity_view_needs_its_entity_and_a_set_view_pages_by_cursor() -> None:
    names = heads(COMPANY.scene())
    entity = names["view_unit_overview"]
    assert next(iter(props(entity))) == "_entity" and required(entity)[0] == "_entity"
    assert props(entity)["_entity"][dt.EXTENSION] == {"class": "org:OrgUnit"}
    assert "_cursor" not in props(entity) and "_page_bytes" not in props(entity)
    paged = names["view_members_report"]
    assert list(props(paged))[-2:] == ["_cursor", "_page_bytes"] and "_entity" not in props(paged)
    assert "_cursor" not in required(paged) and "_page_bytes" not in required(paged)
    assert props(paged)["_page_bytes"]["anyOf"][0]["minimum"] == 1024
    assert props(paged)["_page_bytes"]["anyOf"][0]["maximum"] == 262_144
    assert "at least 1024" in props(paged)["_page_bytes"]["description"]


def test_controls_start_with_an_underscore_and_no_parameter_can() -> None:
    controls = {dt.LIMIT, dt.OFFSET, dt.REASON, dt.DRY_RUN, dt.ENTITY, dt.CURSOR, dt.PAGE_BYTES_ARG}
    assert all(name.startswith("_") for name in controls)
    for head in all_heads():
        declared = {n for n in props(head) if not n.startswith("_")}
        assert declared == {p for p in props(head) if p not in controls}


def test_a_parameter_called_entity_or_limit_is_no_clash() -> None:
    """The company's function over shareholders names its parameter ``entity``."""
    head = heads(COMPANY.scene())["run_shareholders_of"]
    assert "entity" in props(head) and "_entity" not in props(head)


# ---- _reason by obligation ---------------------------------------------------------------


def test_a_reason_is_required_when_the_action_demands_one() -> None:
    names = heads(COMPANY.scene())
    assert "_reason" in required(names["do_offboard_employee"])
    assert names["do_offboard_employee"].annotations["reason"] == "required"
    assert "_reason" not in required(names["do_assign_position"])
    assert names["do_assign_position"].annotations["reason"] == "optional"


def test_a_reason_is_required_when_an_obligation_of_the_callers_policy_applies() -> None:
    plain = heads(COMPANY.scene())["do_assign_position"]
    needs = {
        n.action for n in next(a for a in COMPANY.actions if a.name == "assign_position").needs
    }
    assert needs
    for action in needs:
        bound = heads(COMPANY.scene(), frozenset({action}))["do_assign_position"]
        assert "_reason" in required(bound) and bound.annotations["reason"] == "required"
        assert "Required for this action." in props(bound)["_reason"]["description"]
    unrelated = heads(COMPANY.scene(), frozenset({"write.retract"}))["do_assign_position"]
    assert unrelated.input_schema == plain.input_schema
    assert dt.reason_required(
        next(a for a in COMPANY.actions if a.name == "assign_position"), frozenset(needs)
    )


# ---- annotations, status and descriptions ----------------------------------------------------


def test_annotations_say_what_a_tool_does() -> None:
    names = heads(COMPANY.scene())
    assert names["run_members_of_unit"].annotations == {
        "read_only": True,
        "dry_run": False,
        "reason": None,
        "paging": "offset",
    }
    assert names["do_assign_position"].annotations == {
        "read_only": False,
        "dry_run": True,
        "reason": "optional",
        "paging": None,
    }
    assert names["view_unit_overview"].annotations["paging"] is None
    assert names["view_members_report"].annotations["paging"] == "cursor"
    assert names["view_members_report"].annotations["read_only"] is True


def test_a_deprecated_entry_is_a_tool_with_a_line_naming_its_replacement() -> None:
    head = heads(COMPANY.scene())["view_legacy_unit_overview"]
    assert head.status == "deprecated" and head.replaced_by == "view_unit_overview"
    assert "Deprecated; use view_unit_overview." in head.description


def test_a_replacement_the_caller_may_not_use_is_not_named() -> None:
    scene = COMPANY.scene()
    alone = replace(scene, views=tuple(v for v in scene.views if v.name != "unit_overview"))
    head = heads(alone)["view_legacy_unit_overview"]
    assert head.replaced_by is None and "Deprecated." in head.description
    assert "use" not in head.description.split("Deprecated")[1].split("\n")[0]


def test_a_description_has_the_summary_use_when_and_the_examples() -> None:
    head = heads(COMPANY.scene())["run_training_completions"]
    text = head.description.split("\n\n")
    assert text[0] == "Who completed which of the given trainings since a date."
    assert text[1].startswith("Use when: ")
    assert text[2].startswith("Examples:\n- Who completed the safety or the security training")
    assert 'Arguments: {"since": "2026-01-01", "titles": ["Safety", "Security"]}' in text[2]


def test_an_action_example_is_its_request_text() -> None:
    head = heads(COMPANY.scene())["do_offboard_employee"]
    assert "Examples:\n- A colleague from the sales team left" in head.description
    assert "Arguments:" not in head.description


def test_an_example_without_parameters_is_its_question_only() -> None:
    assert dt.description_text("S", "U", ["- Q?"], deprecated=False, replaced_by=None) == (
        "S\n\nUse when: U\n\nExamples:\n- Q?"
    )
    assert (
        dt.description_text("S", "U", [], deprecated=False, replaced_by=None) == "S\n\nUse when: U"
    )


def test_the_heads_carry_what_the_manifest_lists_of_an_entry() -> None:
    head = heads(COMPANY.scene())["run_members_of_unit"]
    entry = next(f for f in COMPANY.functions if f.name == "members_of_unit")
    assert (head.entry, head.kind, head.module, head.entry_hash) == (
        "members_of_unit",
        "function",
        entry.module,
        entry.hash,
    )
    assert [(c.name, c.summary) for c in head.columns] == [
        (r.column, r.summary) for r in entry.defn.returns
    ]
    assert all(c.type == ColumnType("string") for c in head.columns)
    assert head.featured is entry.defn.featured
    assert heads(COMPANY.scene())["view_unit_overview"].featured is False


# ---- every example is valid input, every valid input binds -------------------------------


def input_validator(head: dt.ToolHead) -> Draft202012Validator:
    return Draft202012Validator(head.input_schema, format_checker=FORMAT_CHECKER)


def _is_reference(schema: dict[str, Any]) -> bool:
    inner = schema.get("items", schema)
    return bool(inner.get(dt.EXTENSION, {}).get("class"))


def _with_references(head: dt.ToolHead, given: dict[str, Any]) -> dict[str, Any]:
    """``given`` and a reference for every required reference property it leaves out: an example
    never gives one."""
    wanted = {
        n: _sample(props(head)[n])
        for n in required(head)
        if n not in given and _is_reference(props(head)[n])
    }
    return {**given, **wanted}


def test_every_example_of_a_function_and_a_view_is_valid_input_once_its_references_are_added() -> (
    None
):
    checked = 0
    for catalog in (COMPANY, INVENTORY):
        scene = catalog.scene()
        names = heads(scene)
        for entry in (*scene.functions, *scene.views):
            kind = "function" if entry in scene.functions else "view"
            head = names[dt.tool_name(kind, entry.name)]
            for example in entry.defn.examples:
                given = _with_references(head, dict(example.params))
                errors = list(input_validator(head).iter_errors(given))
                assert errors == [], (head.name, given, [e.message for e in errors])
                checked += 1
    assert checked >= 8


def test_an_example_line_gives_its_arguments_in_the_form_of_the_input() -> None:
    params = (
        ParamDef("amount", "decimal", "An amount."),
        ParamDef("when", "date", "A day.", default="2026-01-01"),
    )
    line = dt._example_line("How much?", {"amount": 5, "when": "2026-02-03"}, params)
    assert line == '- How much? Arguments: {"amount": "5", "when": "2026-02-03"}'
    many = (ParamDef("amounts", "decimal", "Amounts.", many=True),)
    assert dt._example_line("Q?", {"amounts": [1, "2.5"]}, many).endswith(
        '{"amounts": ["1", "2.5"]}'
    )


def test_an_example_that_leaves_out_a_reference_says_to_supply_it() -> None:
    ref = ParamDef("unit", "ref", "A unit.", of_class="org:OrgUnit")
    optional_ref = ParamDef("other", "ref", "Another.", of_class="org:OrgUnit", optional=True)
    assert dt._example_line("Q?", {}, (ref,)) == "- Q? " + dt.REFERENCES_NOTE
    assert dt._example_line("Q?", {"unit": "x"}, (ref,)) == '- Q? Arguments: {"unit": "x"}'
    assert dt.REFERENCES_NOTE not in dt._example_line("Q?", {}, (optional_ref,))
    assert dt.REFERENCES_NOTE in dt._example_line("Q?", {}, (), entity=True)


def test_the_example_of_an_entity_view_and_of_a_function_over_a_reference_say_so() -> None:
    names = heads(COMPANY.scene())
    assert dt.REFERENCES_NOTE in names["view_unit_overview"].description
    assert '"status": "active"' in names["view_unit_overview"].description
    assert dt.REFERENCES_NOTE in names["run_members_of_unit"].description
    assert dt.REFERENCES_NOTE not in names["run_training_completions"].description


def test_a_defaulted_parameter_validates_against_its_own_schema() -> None:
    for head in all_heads():
        for name, schema in props(head).items():
            if "default" in schema:
                found = list(
                    input_validator(head).iter_errors({**_minimal(head), name: schema["default"]})
                )
                assert found == [], (head.name, name)


def _sample(schema: dict[str, Any]) -> Any:
    """A value the schema accepts."""
    if "anyOf" in schema:
        return _sample(schema["anyOf"][0])
    if schema["type"] == "array":
        return [_sample(schema["items"])]
    if "enum" in schema:
        return schema["enum"][0]
    if schema["type"] == "integer":
        return 3
    if schema["type"] == "boolean":
        return True
    if schema.get("format") == "date":
        return "2026-11-25"
    if schema.get("format") == "date-time":
        return "2026-11-25T10:00:00.250+02:00"
    if "pattern" in schema and schema.get(dt.EXTENSION, {}).get("class"):
        return "ORG-0042"
    if "pattern" in schema:
        return "12.50"
    return "text"


def _minimal(head: dt.ToolHead) -> dict[str, Any]:
    return {name: _sample(props(head)[name]) for name in required(head)}


def test_a_value_the_schema_accepts_binds_with_the_declared_types() -> None:
    bound = 0
    for catalog in (COMPANY, INVENTORY):
        scene = catalog.scene()
        names = heads(scene)
        for kind, entries in (
            ("function", scene.functions),
            ("action", scene.actions),
            ("view", scene.views),
        ):
            for entry in entries:
                head = names[dt.tool_name(kind, entry.name)]
                given = {n: _sample(s) for n, s in props(head).items() if not n.startswith("_")}
                assert list(input_validator(head).iter_errors({**given, **_controls(head)})) == []
                got = bind_json(kind, entry.name, entry.defn.params, given, scene.model)
                assert set(got) == set(given)
                bound += 1
    assert bound >= 15


def _controls(head: dt.ToolHead) -> dict[str, Any]:
    """A value of each control the tool requires."""
    return {n: _sample(props(head)[n]) for n in required(head) if n.startswith("_")}


# ---- what a caller who sees less is given -------------------------------------------------------


def names_of(scene: Scene) -> list[str]:
    return sorted(heads(scene))


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_of_items_gets_no_tool_that_names_a_hidden_type() -> None:
    full = names_of(INVENTORY.scene())
    limited = names_of(INVENTORY.reader(ITEMS))
    assert set(limited) < set(full)
    for hidden in ("run_items_in_location", "do_move_item"):
        assert hidden in full and hidden not in limited
    assert "do_retire_item" in limited and "run_items_by_condition" in limited
    text = repr([h.input_schema for h in heads(INVENTORY.reader(ITEMS)).values()])
    for word in ("Location", "Category", "located_in", "in_category"):
        assert word not in text and word not in " ".join(limited)


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_an_enum_lists_the_values_of_the_model_the_caller_sees() -> None:
    scene = INVENTORY.reader(ITEMS)
    condition = props(heads(scene)["run_items_by_condition"])["condition"]
    assert condition["enum"] == ["new", "good", "worn", "damaged"]
    shrunk = replace(
        scene.model,
        enums={
            **scene.model.enums,
            "inv:Condition": replace(
                scene.model.enums["inv:Condition"],
                values=scene.model.enums["inv:Condition"].values[:2],
            ),
        },
    )
    again = props(heads(replace(scene, model=shrunk))["run_items_by_condition"])["condition"]
    assert again["enum"] == ["new", "good"]


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_without_markings_gets_no_tool_over_a_marked_attribute() -> None:
    everything = names_of(STAFF.scene())
    personal = names_of(STAFF.reader(held=PERSONAL))
    nothing = names_of(STAFF.reader(held=NOTHING))
    assert set(nothing) < set(personal) < set(everything)
    assert "do_set_phone" in nothing and "do_set_email" not in nothing
    assert "do_set_email" in personal and "do_set_salary" not in personal
    text = repr([h.input_schema for h in heads(STAFF.reader(held=NOTHING)).values()])
    for word in ("salary", "badge_code", "PayGrade", "pay_grade", "email"):
        assert word not in text


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_enum_of_a_marked_attribute_is_missing_for_a_reader_who_cannot_see_it() -> None:
    """``by_grade`` takes an enum only a marked attribute uses: with the marking it lists the
    values, without it the tool is gone together with the enum."""
    held = heads(STAFF.reader(held=PERSONAL))
    assert "run_by_grade" in held
    assert props(held["run_by_grade"])["grade"].get("enum")
    assert "run_by_grade" not in heads(STAFF.reader(held=NOTHING))
    assert props(heads(STAFF.reader(held=PERSONAL))["do_set_grade"])["value"].get("enum")
