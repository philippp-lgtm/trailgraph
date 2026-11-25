"""Markings in the ontology (field visibility plan F1, F2, step 1): grammar, validation,
compiled schema ``/2`` with ``requires``, the reader's attribute key allow list, diff kinds,
describe and the JSON Schema export."""

import copy
from typing import Any

import pytest
from support.fixtures import MARKED_ONTOLOGIES, compiled_model, ontology_versions
from support.skills import graph_entry

from graph_cli.kernel.canonical import JsonObject, hash_value, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import CompileOutcome, compile_bundle
from graph_cli.ontology_layer.diff import classify, module_bumps
from graph_cli.ontology_layer.exporters.jsonschema import MARKINGS_KEYWORD, export_jsonschema
from graph_cli.ontology_layer.model import CompiledModel, load_compiled, model_from_json
from graph_cli.services.describe import describe_fields

HEADER = """\
format: graph_cli.ir/1
module: {{id: sample, prefix: smp, version: {v}, summary: Sample.{imports}}}
"""

BASE = """\
markings:
  personal: {summary: Personal data.}
  financial: {summary: Money matters., description: Salaries and bank details.}
classes:
  Person:
    summary: A person.
    attributes:
      name: {summary: Name., type: string, required: true, role: label, markings: [personal]}
      email: {summary: E-mail., type: string, unique: true, markings: [smp:personal]}
      salary: {summary: Salary., type: integer, required: true, default: 0,
               markings: [financial, personal]}
      team: {summary: Team., type: string}
  Employee:
    summary: A person employed.
    is_a: Person
    attributes:
      staff_number: {summary: Staff number., type: string}
"""


def compile_text(
    body: str, version: str = "1.0.0", extra: dict[str, str] | None = None
) -> CompileOutcome:
    imports = ""
    files = {}
    for module_id, text in (extra or {}).items():
        files[module_id] = text.encode()
        imports = f", imports: [{{module: {module_id}, version: '^1.0.0'}}]"
    text = HEADER.format(v=version, imports=imports) + body
    files["sample"] = text.encode()
    return compile_bundle("sample", LayeredSource(MappingSource(files)))


def model(body: str, version: str = "1.0.0", extra: dict[str, str] | None = None) -> CompiledModel:
    outcome = compile_text(body, version, extra)
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def findings(body: str, extra: dict[str, str] | None = None) -> list[tuple[str, str]]:
    return [(f.code, f.target["path"]) for f in compile_text(body, extra=extra).findings]


# ---- grammar and validation ---------------------------------------------------------------


def test_markings_compile_sorted_and_inherited() -> None:
    compiled = model(BASE)
    person = compiled.classes["smp:Person"].attributes
    assert person["name"].markings == ("smp:personal",)  # a local name resolves to the CURIE
    assert person["email"].markings == ("smp:personal",)
    assert person["salary"].markings == ("smp:financial", "smp:personal")  # sorted
    assert person["team"].markings == ()
    # a property of the declaration: every subclass inherits it
    assert compiled.classes["smp:Employee"].attributes["email"].markings == ("smp:personal",)
    assert list(compiled.markings) == ["smp:financial", "smp:personal"]
    assert compiled.markings["smp:financial"].description == "Salaries and bank details."
    assert {"smp:personal", "smp:financial"} <= compiled.term_curies()
    assert compiled.features() == ("markings",)


def test_marking_of_an_imported_module() -> None:
    shared = (
        "format: graph_cli.ir/1\n"
        "module: {id: shared, prefix: shr, version: 1.0.0, summary: Shared.}\n"
        "markings:\n  personal: {summary: Personal data.}\n"
    )
    body = (
        "classes:\n  Person:\n    summary: A person.\n    attributes:\n"
        "      email: {summary: E-mail., type: string, markings: [shr:personal]}\n"
    )
    compiled = model(body, extra={"shared": shared})
    assert compiled.classes["smp:Person"].attributes["email"].markings == ("shr:personal",)
    assert compiled.markings["shr:personal"].summary == "Personal data."


@pytest.mark.parametrize(
    ("markings", "path"),
    [
        ("[]", "classes.Person.attributes.email.markings"),
        ("personal", "classes.Person.attributes.email.markings"),
        ("[personal, personal]", "classes.Person.attributes.email.markings[1]"),
        ("[personal, smp:personal]", "classes.Person.attributes.email.markings[1]"),
        ("[Personal]", "classes.Person.attributes.email.markings[0]"),
    ],
)
def test_markings_list_form(markings: str, path: str) -> None:
    body = (
        "markings:\n  personal: {summary: Personal data.}\n"
        "classes:\n  Person:\n    summary: A person.\n    attributes:\n"
        f"      email: {{summary: E-mail., type: string, markings: {markings}}}\n"
    )
    assert findings(body) == [("model.structural.invalid_value", path)]


def test_unknown_and_unimported_markings() -> None:
    body = (
        "classes:\n  Person:\n    summary: A person.\n    attributes:\n"
        "      email: {summary: E-mail., type: string, markings: [personl, other:personal]}\n"
        "markings:\n  personal: {summary: Personal data.}\n"
    )
    outcome = compile_text(body)
    found = [(f.code, f.target["path"], f.expected) for f in outcome.findings]
    assert found == [
        (
            "model.referential.unknown_marking",
            "classes.Person.attributes.email.markings[0]",
            ["personal"],
        ),
        # a prefix that is not imported: the same name of the own module is suggested
        (
            "model.referential.unknown_marking",
            "classes.Person.attributes.email.markings[1]",
            ["personal"],
        ),
    ]


def test_marking_shares_the_name_space_of_relations() -> None:
    body = (
        "markings:\n  knows: {summary: Who knows whom.}\n"
        "classes:\n  Person: {summary: A person.}\n"
        "relations:\n  knows: {summary: Knows., domain: Person, range: Person}\n"
    )
    assert findings(body) == [("model.structural.duplicate_term", "markings.knows")]


def test_marking_declaration_shape() -> None:
    body = "markings:\n  personal: {description: No summary., deprecated: true}\n"
    assert findings(body) == [
        ("model.structural.missing_summary", "markings.personal"),
        ("model.structural.unknown_key", "markings.personal.deprecated"),
    ]
    assert findings("markings:\n  Personal: {summary: Upper case.}\n") == [
        ("model.structural.invalid_value", "markings.Personal")
    ]


# ---- compiled schema /2 -------------------------------------------------------------------


def test_a_model_with_markings_is_schema_2_with_requires() -> None:
    document = model(BASE).to_json()
    assert document["schema"] == "graph_cli.compiled_ontology/2"
    assert document["requires"] == ["markings"]
    assert sorted(document["markings"]) == ["smp:financial", "smp:personal"]  # type: ignore[arg-type]


def test_declared_but_unused_markings_still_need_schema_2() -> None:
    document = model("markings:\n  personal: {summary: Personal data.}\n").to_json()
    assert document["schema"] == "graph_cli.compiled_ontology/2"
    assert model_from_json(document).markings.keys() == {"smp:personal"}


def test_a_model_without_markings_keeps_schema_1_and_its_bytes() -> None:
    plain = BASE.replace(", markings: [personal]", "").replace(", markings: [smp:personal]", "")
    plain = plain.replace(",\n               markings: [financial, personal]", "")
    plain = plain.split("classes:", 1)[1]
    document = model("classes:" + plain).to_json()
    assert document["schema"] == "graph_cli.compiled_ontology/1"
    assert "requires" not in document and "markings" not in document
    attributes = document["classes"]["smp:Person"]["attributes"]  # type: ignore[index,call-overload]
    assert all("markings" not in a for a in attributes.values())


@pytest.mark.parametrize("name", MARKED_ONTOLOGIES)
def test_marked_fixture_round_trips(name: str) -> None:
    for version in ontology_versions(name):
        compiled = compiled_model(name, version)
        data = pretty_bytes(compiled.to_json())
        assert load_compiled(data, expected_hash=compiled.artifact_hash) == compiled


def reader_022(document: JsonObject) -> None:
    """The schema check every program before 0.23.0 has (``model_from_json`` of 0.21.x and
    0.22.x): the only key of a compiled document such a program does not ignore."""
    if document["schema"] != "graph_cli.compiled_ontology/1":
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported compiled ontology schema {document['schema']!r}",
        )


@pytest.mark.doc_claim("security_model.markings")
def test_a_022_reader_refuses_a_model_with_markings() -> None:
    document = compiled_model("personnel").to_json()
    with pytest.raises(GraphCliError) as info:
        reader_022(document)
    assert info.value.code == "format.unsupported_version"
    reader_022(compiled_model("inventory").to_json())  # a model without markings still loads


def load(document: dict[str, Any]) -> CompiledModel:
    return load_compiled(pretty_bytes(document), expected_hash=hash_value(document))


def refusal(document: dict[str, Any]) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        load(document)
    return info.value


def marked() -> dict[str, Any]:
    return copy.deepcopy(compiled_model("personnel").to_json())  # type: ignore[arg-type]


def plain() -> dict[str, Any]:
    return copy.deepcopy(compiled_model("inventory").to_json())  # type: ignore[arg-type]


def test_unknown_feature_is_refused() -> None:
    document = marked()
    document["requires"] = ["markings", "row_rules"]
    error = refusal(document)
    assert error.code == "format.unsupported_version"
    assert error.details == {"feature": "row_rules"}
    assert "update graph" in error.message


@pytest.mark.parametrize("where", ["top", "attribute", "requires"])
def test_schema_1_with_markings_is_refused(where: str) -> None:
    document = marked() if where != "requires" else plain()
    document["schema"] = "graph_cli.compiled_ontology/1"
    if where == "attribute":
        del document["markings"], document["requires"]
    elif where == "requires":
        document["requires"] = ["markings"]
    error = refusal(document)
    assert error.code == "format.unsupported_version"
    key = {"top": "requires", "attribute": "markings", "requires": "requires"}[where]
    assert error.details["unknown"] == f"the key {key!r}"


@pytest.mark.parametrize(
    "change",
    ["no_requires", "empty_markings", "unsorted", "undeclared", "plain_as_2"],
)
def test_schema_2_must_name_and_use_markings(change: str) -> None:
    document = plain() if change == "plain_as_2" else marked()
    person = document["classes"]["ex:Person"]["attributes"] if change != "plain_as_2" else {}
    if change == "no_requires":
        document["requires"] = []
    elif change == "empty_markings":
        document["markings"] = {}
    elif change == "unsorted":
        person["email"]["markings"] = ["ex:personal", "ex:personal"]
    elif change == "undeclared":
        person["email"]["markings"] = ["ex:secret"]
    else:
        document["schema"] = "graph_cli.compiled_ontology/2"
        document["requires"] = ["markings"]
    error = refusal(document)
    assert error.code == "internal.error" and "bad shape" in error.message
    with pytest.raises(JsonShapeError):
        model_from_json(document)


@pytest.mark.parametrize("make", [plain, marked])
def test_unknown_attribute_key_is_refused(make: Any) -> None:
    document = make()
    first = next(iter(document["classes"]))
    attribute = next(iter(document["classes"][first]["attributes"]))
    document["classes"][first]["attributes"][attribute]["write_only"] = True
    error = refusal(document)
    assert error.code == "format.unsupported_version"
    assert error.details == {
        "owner": f"attribute {first}.{attribute}",
        "unknown": "the key 'write_only'",
    }


def test_attribute_allow_list_reads_a_model_written_before_label_roles() -> None:
    document = plain()
    for cls in document["classes"].values():
        for attribute in cls["attributes"].values():
            del attribute["role"]
    loaded = load(document)
    assert all(a.role is None for c in loaded.classes.values() for a in c.attributes.values())


# ---- diff ---------------------------------------------------------------------------------


def kinds(old: CompiledModel, new: CompiledModel) -> list[tuple[str, str, str, Any]]:
    return [(c.path, c.kind, c.level, c.detail.get("marking")) for c in classify(old, new).changes]


def test_renames_compare_markings_through() -> None:
    old = model(BASE)
    new = model(
        BASE.replace(
            "email: {summary: E-mail., type: string, unique: true, markings: [smp:personal]}",
            "mail: {summary: E-mail., type: string, unique: true, renamed_from: email}",
        ),
        "2.0.0",
    )
    assert kinds(old, new) == [
        ("classes.smp:Person.attributes.email", "remove_marking", "minor", "smp:personal"),
        ("classes.smp:Person.attributes.email", "rename", "major", None),
    ]


def test_new_marked_attribute_is_an_ordinary_add() -> None:
    old = model(BASE)
    new = model(
        BASE.replace(
            "      team: {summary: Team., type: string}\n",
            "      team: {summary: Team., type: string}\n"
            "      phone: {summary: Phone., type: string, markings: [personal]}\n",
        ),
        "1.1.0",
    )
    assert kinds(old, new) == [
        ("classes.smp:Person.attributes.phone", "add_attribute", "minor", None)
    ]


def test_marking_changes_belong_to_their_module() -> None:
    shared = (
        "format: graph_cli.ir/1\n"
        "module: {{id: shared, prefix: shr, version: {v}, summary: Shared.}}\n"
        "markings:\n  personal: {{summary: Personal data.}}\n{more}"
    )
    body = "classes:\n  Person: {summary: A person.}\n"
    old = model(body, extra={"shared": shared.format(v="1.0.0", more="")})
    more = "  financial: {summary: Money.}\n"
    new = model(body, "1.1.0", extra={"shared": shared.format(v="1.0.0", more=more)})
    report = classify(old, new)
    assert [(c.path, c.kind, c.level) for c in report.changes] == [
        ("markings.shr:financial", "add_marking_term", "minor")
    ]
    bumps = module_bumps(old, new, report)
    assert [(b.module, b.level, b.check) for b in bumps] == [("shared", "minor", "insufficient")]


# ---- describe and the JSON Schema export --------------------------------------------------


def test_describe_fields_shows_markings_only_where_set() -> None:
    compiled = model(BASE)
    fields = describe_fields(graph_entry(compiled), compiled, "smp:Person")["fields"]
    by_name = {f["name"]: f for f in fields}  # type: ignore[index,union-attr]
    assert by_name["salary"]["markings"] == ["smp:financial", "smp:personal"]
    assert "markings" not in by_name["team"]


def test_jsonschema_export_annotates_marked_attributes() -> None:
    files = export_jsonschema(model(BASE))
    properties = files["smp.Person.json"]["properties"]
    assert properties["email"][MARKINGS_KEYWORD] == ["smp:personal"]  # type: ignore[index,call-overload]
    assert MARKINGS_KEYWORD not in properties["team"]  # type: ignore[operator,index,call-overload]
