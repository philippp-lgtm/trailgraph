"""The function file format ``graph_cli.function/1``: shape, names, problems."""

from support.functions import VALID, valid_sources

from graph_cli.kinetic.format import FunctionDef, ParamDef, function_name, load_function

TEXT = (VALID / "members_of_unit.yaml").read_text()
FILE = "members_of_unit.yaml"


def codes(file_name: str, text: str) -> list[str]:
    defn, problems = load_function(file_name, text)
    assert defn is None
    return [p.code for p in problems]


def test_a_valid_file_is_read_in_full() -> None:
    defn, problems = load_function(FILE, TEXT)
    assert problems == []
    assert defn is not None
    assert (defn.name, defn.module, defn.featured, defn.status) == (
        "members_of_unit",
        "organization",
        True,
        "active",
    )
    assert defn.params == (ParamDef("unit", "ref", "The unit to list.", of_class="org:OrgUnit"),)
    assert [r.column for r in defn.returns] == ["person", "position", "unit"]
    assert defn.query.startswith("MATCH (p:Person)")
    assert not defn.query.endswith("\n")


def test_every_valid_fixture_loads() -> None:
    for file_name, text in valid_sources().items():
        defn, problems = load_function(file_name, text)
        assert problems == [] and defn is not None and defn.name == function_name(file_name)


def test_defaults_many_and_examples_are_read() -> None:
    text = (VALID / "training_completions.yaml").read_text()
    defn, _ = load_function("training_completions.yaml", text)
    assert defn is not None
    titles, since = defn.params
    assert (titles.many, titles.required) == (True, True)
    assert (since.default, since.required) == ("2000-01-01", False)
    assert defn.examples[0].params == {"titles": ["Safety", "Security"], "since": "2026-01-01"}


def test_the_declaration_reads_back_from_its_json_form() -> None:
    defn, _ = load_function(FILE, TEXT)
    assert defn is not None
    assert FunctionDef.from_json(defn.to_json(), defn.query) == defn


def test_a_problem_names_function_path_and_line() -> None:
    _, problems = load_function(FILE, TEXT.replace("featured: true", "featured: maybe"))
    (problem,) = problems
    assert (problem.function, problem.code, problem.path) == (
        "members_of_unit",
        "function.invalid_value",
        "featured",
    )
    assert problem.line == len(TEXT.splitlines())
    assert problem.to_json()["function"] == "members_of_unit"


def test_yaml_that_is_not_strict_is_refused() -> None:
    assert codes(FILE, "name: a\nname: b\n") == ["function.invalid_yaml"]
    assert codes(FILE, "") == ["function.invalid_yaml"]
    assert codes(FILE, "- a\n- b\n") == ["function.invalid_value"]


def test_schema_unknown_and_missing_keys() -> None:
    assert "function.unsupported_schema" in codes(FILE, TEXT.replace("function/1", "function/2"))
    assert codes(FILE, TEXT + "owner: someone\n") == ["function.unknown_key"]
    assert codes(FILE, TEXT.replace("query: |", "other: |")) == [
        "function.unknown_key",
        "function.missing_key",
    ]


def test_name_rules_and_the_file_name() -> None:
    assert codes(FILE, TEXT.replace("name: members_of_unit", "name: Members")) == [
        "function.invalid_value"
    ]
    assert codes("other.yaml", TEXT) == ["function.name_mismatch"]
    assert codes("members_of_unit.yml", TEXT) == ["function.name_mismatch"]


def test_length_limits() -> None:
    long_summary = "x" * 101
    assert codes(
        FILE,
        TEXT.replace("People holding a position in a unit or in any unit below it.", long_summary),
    ) == ["function.invalid_value"]
    assert codes(FILE, TEXT.replace("Who works in a department", "x" * 600 + " who")) == [
        "function.invalid_value"
    ]


def test_parameter_declarations() -> None:
    assert codes(FILE, TEXT.replace("    class: org:OrgUnit\n", "")) == ["function.missing_key"]
    assert codes(FILE, TEXT.replace("type: ref", "type: ref\n    enum: org:Kind")) == [
        "function.invalid_value"
    ]
    assert codes(FILE, TEXT.replace("type: ref", "type: float")) == ["function.invalid_value"]
    assert codes(FILE, TEXT.replace("  unit:\n", "  Unit:\n")) == ["function.invalid_value"]
    assert codes(FILE, TEXT.replace("    summary: The unit to list.\n", "")) == [
        "function.missing_key"
    ]


def test_returns_and_examples_need_entries() -> None:
    assert codes(FILE, TEXT.replace("returns:", "returns: []\nold:")) == [
        "function.unknown_key",
        "function.invalid_value",
    ]
    assert "function.invalid_value" in codes(
        FILE, TEXT.replace("examples:\n  - question:", "examples: []\nold:\n  - question:")
    )


def test_replaced_by_needs_deprecated_and_another_name() -> None:
    assert codes(FILE, TEXT + "replaced_by: other_one\n") == ["function.invalid_value"]
    both = TEXT + "status: deprecated\nreplaced_by: members_of_unit\n"
    assert codes(FILE, both) == ["function.invalid_value"]
    ok, problems = load_function(FILE, TEXT + "status: deprecated\nreplaced_by: other_one\n")
    assert problems == [] and ok is not None and ok.replaced_by == "other_one"
