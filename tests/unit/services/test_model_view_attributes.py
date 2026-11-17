"""The model view of a read view with an attribute half (field visibility plan F5, R7): the
model as if the classes did not declare the hidden attributes.

The oracle is the ``staff`` twin (``support.markings``): the module as a caller holding no
marking must read it, authored by hand. The rest are the cases the twin cannot show: an abstract
class above concrete classes that keep different attributes, a partial holder, a rule whose
text happens to contain a marking's CURIE, a module whose only terms are markings, and enums
no attribute uses.
"""

from pathlib import Path

from support.fixtures import assert_same_but_unused_standard_terms, compiled_model
from support.markings import CONTRACTOR, EMPLOYEE, STAFF, twin_model

from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.bundle import DirectorySource, LayeredSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.term_model import concrete_types
from graph_cli.services.visibility import Visibility, attribute_mask, filter_model

STAFF_MODEL = compiled_model("staff")
EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))


def view_of(model: CompiledModel, held: frozenset[str]) -> CompiledModel:
    return filter_model(model, None, attribute_mask(model, held))


def compile_dir(main: str, directory: Path) -> CompiledModel:
    outcome = compile_bundle(main, LayeredSource(DirectorySource(directory)))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def test_the_view_of_a_caller_without_markings_is_the_twin() -> None:
    assert view_of(STAFF_MODEL, frozenset()).to_json() == twin_model().to_json()


def test_a_view_without_an_attribute_half_keeps_every_attribute() -> None:
    types = concrete_types(STAFF_MODEL.classes)
    kept = filter_model(STAFF_MODEL, types)
    assert_same_but_unused_standard_terms(kept, STAFF_MODEL)  # every type visible: no change
    assert filter_model(STAFF_MODEL, None).to_json() == STAFF_MODEL.to_json()
    # a holder of every marking has no attribute half at all
    assert attribute_mask(STAFF_MODEL, {"stf:personal", "stf:pay"}) is None


def test_a_partial_holder_keeps_what_it_holds_and_only_those_markings() -> None:
    view = view_of(STAFF_MODEL, frozenset({"stf:personal"}))
    employee = view.classes[EMPLOYEE].attributes
    assert "salary" not in employee  # needs stf:personal and stf:pay
    assert {"email", "birth_date", "pay_grade", "badge_code", "weekly_hours"} <= set(employee)
    assert employee["email"].markings == ("stf:personal",)
    assert set(view.markings) == {"stf:personal"}
    assert {r.id for r in view.rules} == {r.id for r in STAFF_MODEL.rules}
    assert "stf:PayGrade" in view.enums and "stf:BadgeCode" in view.datatypes
    # the view of a caller who holds only stf:pay: stf:pay marks no attribute it sees
    assert set(view_of(STAFF_MODEL, frozenset({"stf:pay"})).markings) == set()


def test_an_abstract_class_keeps_what_a_visible_concrete_subclass_keeps() -> None:
    mask = ReadView.of(
        attributes={
            EMPLOYEE: {"name", "phone", "stage"},
            CONTRACTOR: {"name", "agency"},
        }
    ).attributes
    both = filter_model(STAFF_MODEL, None, mask)
    assert set(both.classes["stf:Colleague"].attributes) == {"name", "phone"}
    assert set(both.classes[CONTRACTOR].attributes) == {"name", "agency"}
    assert both.classes[EMPLOYEE].attributes["phone"].declared_in == "stf:Colleague"
    # with only contractors visible, the abstract class keeps what contractors keep
    contractors = filter_model(STAFF_MODEL, frozenset({CONTRACTOR, "stf:Crew"}), mask)
    assert set(contractors.classes["stf:Colleague"].attributes) == {"name"}
    # a class without an entry in the mask keeps nothing (fail closed)
    assert filter_model(STAFF_MODEL, None, mask).classes["stf:Crew"].attributes == {}


def test_rules_and_guards_that_read_a_hidden_attribute_are_dropped() -> None:
    view = view_of(STAFF_MODEL, frozenset())
    assert {r.id for r in view.rules} == {"stf.contractor_contact"}  # the sibling's e-mail
    flow = view.lifecycles["stf:OnboardingFlow"]
    assert [[g.id for g in t.guards] for t in flow.transitions] == [[], ["stf.shift_known"]]


def test_a_removed_marking_is_no_name_an_expression_uses(tmp_path: Path) -> None:
    """A rule whose text contains the CURIE of a marking the caller lacks stays: the substring
    test of removed terms is not fed markings (it would over-drop rules, R7)."""
    text = (STAFF / "1.0.0" / "staff.yaml").read_text()
    text = text.replace(
        "      - {attr: agency, value_presence: PRESENT}\n",
        "      - {attr: agency, value_presence: PRESENT}\n"
        "      - none_of:\n"
        "          - {attr: agency, equals_string: 'stf:personal'}\n",
    )
    (tmp_path / "staff.yaml").write_text(text)
    model = compile_dir("staff", tmp_path)
    assert "stf.contractor_contact" in {r.id for r in view_of(model, frozenset()).rules}


SHARED = """\
format: graph_cli.ir/1
module:
  id: shared
  prefix: shr
  version: 1.0.0
  summary: Markings several modules use.
markings:
  personal:
    summary: Personal data of a natural person.
"""
MAIN = """\
format: graph_cli.ir/1
module:
  id: main
  prefix: mn
  version: 1.0.0
  summary: People.
  imports:
    - {module: shared, version: "^1.0"}
classes:
  Person:
    summary: A person.
    attributes:
      name: {summary: Name., type: string, role: label}
      email: {summary: E-mail., type: string, markings: [shr:personal]}
"""


def test_a_module_whose_only_terms_are_markings_the_caller_lacks_loses_its_prefix(
    tmp_path: Path,
) -> None:
    (tmp_path / "shared.yaml").write_text(SHARED)
    (tmp_path / "main.yaml").write_text(MAIN)
    model = compile_dir("main", tmp_path)
    assert "shr" in model.prefixes
    lacking = view_of(model, frozenset())
    assert "shr" not in lacking.prefixes and "shared" not in [m.id for m in lacking.modules]
    assert lacking.markings == {}
    held = filter_model(model, None, attribute_mask(model, {"shr:personal"}))
    assert "shr" in held.prefixes and set(held.markings) == {"shr:personal"}


CODES = """\
format: graph_cli.ir/1
module:
  id: codes
  prefix: cd
  version: 1.0.0
  summary: Codes no attribute uses.
enums:
  Colour:
    summary: A colour.
    values:
      red: {summary: Red.}
      blue: {summary: Blue.}
"""
LEVELS = """\
format: graph_cli.ir/1
module:
  id: levels
  prefix: lv
  version: 1.0.0
  summary: People with a marked tier.
  imports:
    - {module: codes, version: "^1.0"}
markings:
  personal:
    summary: Personal data of a natural person.
enums:
  Level:
    summary: A level no attribute uses.
    values:
      low: {summary: Low.}
      high: {summary: High.}
  Tier:
    summary: A tier only the marked attribute uses.
    values:
      basic: {summary: Basic.}
      gold: {summary: Gold.}
classes:
  Person:
    summary: A person.
    attributes:
      name: {summary: Name., type: string, role: label}
      tier: {summary: Tier., type: Tier, markings: [personal]}
  Place:
    summary: A place.
    attributes:
      name: {summary: Name., type: string, role: label}
"""


def test_the_attribute_half_drops_only_the_enums_its_hidden_attributes_alone_use(
    tmp_path: Path,
) -> None:
    (tmp_path / "codes.yaml").write_text(CODES)
    (tmp_path / "levels.yaml").write_text(LEVELS)
    model = compile_dir("levels", tmp_path)
    lacking = view_of(model, frozenset())
    assert "lv:Tier" not in lacking.enums  # every attribute that uses it is hidden
    assert {"lv:Level", "cd:Colour"} <= set(lacking.enums)  # no attribute uses them
    # a module whose only term is an enum no attribute uses keeps its prefix
    assert "cd" in lacking.prefixes and "codes" in [m.id for m in lacking.modules]
    # the type half alone still drops what no attribute of a kept class uses (stage 3)
    places = filter_model(model, frozenset({"lv:Place"}))
    assert set(places.enums) == set() and "cd" not in places.prefixes
    both = filter_model(model, frozenset({"lv:Place"}), attribute_mask(model, frozenset()))
    assert set(both.enums) == set() and set(both.prefixes) == set(places.prefixes)


# ---- the cache ---------------------------------------------------------------------------


def entry() -> GraphEntry:
    return GraphEntry(
        EXAMPLE, "example_graph", "active", OntologyPin("staff", "1.0.0", "sha256:s"), {}, ()
    )


def test_the_model_view_of_an_attribute_half_is_filtered_and_cached() -> None:
    visibility = Visibility()
    assert visibility.model(entry(), STAFF_MODEL, UNLIMITED) is STAFF_MODEL
    view = ReadView(None, attribute_mask(STAFF_MODEL, frozenset()))
    first = visibility.model(entry(), STAFF_MODEL, view)
    assert first.to_json() == twin_model().to_json()
    assert visibility.model(entry(), STAFF_MODEL, view) is first
