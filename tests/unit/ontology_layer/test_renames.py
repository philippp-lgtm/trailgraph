"""``renamed_from`` across modules, rename checks R1-R6 and the derived renames (B7.12)."""

from itertools import pairwise

import pytest
from support.fixtures import (
    MODULAR_ONTOLOGIES,
    TOY_ONTOLOGIES,
    compiled_model,
    ontology_versions,
)
from support.modules import compile_modules, header, single

from graph_cli.ontology_layer.diff import (
    DiffReport,
    RenameProblem,
    Renames,
    check_renames,
    classify,
    derive_renames,
)
from graph_cli.ontology_layer.model import CompiledModel

ATTRIBUTES = """      title: {summary: Title., type: string}
      size: {summary: Size., type: string}
"""


def body(
    *,
    alpha: str = "Alpha",
    alpha_from: str | None = None,
    beta: str = "Beta",
    beta_from: str | None = None,
    attributes: str = ATTRIBUTES,
    link: str = "link",
    link_from: str | None = None,
    more: str = "",
) -> str:
    """Two classes and a relation between them; ``more`` adds classes or relations."""

    def origin(name: str | None) -> str:
        return "" if name is None else f"    renamed_from: {name}\n"

    return (
        "classes:\n"
        f"  {alpha}:\n    summary: First class.\n{origin(alpha_from)}"
        f"    attributes:\n{attributes}"
        f"  {beta}:\n    summary: Second class.\n{origin(beta_from)}"
        f"{more}"
        "relations:\n"
        f"  {link}:\n    summary: Link.\n    domain: {alpha}\n    range: {beta}\n"
        f"{origin(link_from)}"
    )


def old_model() -> CompiledModel:
    return single("1.0.0", body())


def new_model(**kwargs: str | None) -> CompiledModel:
    return single("2.0.0", body(**kwargs))


def problems(old: CompiledModel, new: CompiledModel) -> list[tuple[str, str, str]]:
    return [(p.rule, p.severity, p.path) for p in check_renames(old, new)]


def kinds(report: DiffReport) -> list[tuple[str, str]]:
    return [(c.path, c.kind) for c in report.changes]


# ---- renamed_from naming a term of another module ------------------------------------------

MOVED_MAIN = header("example", "ex", "2.0.0", ("area_a",))
MOVED_AREA = header("area_a", "aa", "1.0.0") + body(
    alpha_from="ex:Alpha", beta_from="ex:Beta", link_from='"ex:link"'
)


def moved_model() -> CompiledModel:
    return compile_modules("example", {"example": MOVED_MAIN, "area_a": MOVED_AREA})


def test_a_curie_renamed_from_is_a_rename_across_modules() -> None:
    old, new = old_model(), moved_model()
    report = classify(old, new)
    assert kinds(report) == [
        ("classes.ex:Alpha", "rename"),
        ("classes.ex:Beta", "rename"),
        ("modules.area_a", "add_import"),
        ("relations.ex:link", "rename"),
        # the relation's domain and range name the moved classes: compared through the rename
    ]
    assert report.level == "major"
    assert check_renames(old, new) == ()
    assert derive_renames(old, new) == Renames(
        {"ex:Alpha": "aa:Alpha", "ex:Beta": "aa:Beta"}, {"ex:link": "aa:link"}, {}
    )


def test_a_pure_move_shows_only_renames() -> None:
    """Domain, range and ``extends`` naming a renamed class follow it: no change report."""
    sub = "  Delta:\n    summary: Third.\n    is_a: Alpha\n"
    old = single("1.0.0", body(more=sub))
    moved_sub = "  Delta:\n    summary: Third.\n    is_a: Alpha\n    renamed_from: ex:Delta\n"
    area = header("area_a", "aa", "1.0.0") + body(
        alpha_from="ex:Alpha", beta_from="ex:Beta", link_from='"ex:link"', more=moved_sub
    )
    new = compile_modules("example", {"example": MOVED_MAIN, "area_a": area})
    assert new.classes["aa:Delta"].extends == "aa:Alpha"
    assert kinds(classify(old, new)) == [
        ("classes.ex:Alpha", "rename"),
        ("classes.ex:Beta", "rename"),
        ("classes.ex:Delta", "rename"),
        ("modules.area_a", "add_import"),
        ("relations.ex:link", "rename"),
    ]


def test_a_real_change_next_to_a_rename_still_shows() -> None:
    """The comparison is through the rename, not blind to the references."""
    sub = "  Delta:\n    summary: Third.\n    is_a: Alpha\n"
    old = single("1.0.0", body(more=sub))
    moved = single(
        "2.0.0",
        body(
            alpha="Gamma",
            alpha_from="Alpha",
            link="link",
            more="  Delta:\n    summary: Third.\n    is_a: Beta\n",
        ),
    )
    changes = {(c.path, c.kind): c.detail for c in classify(old, moved).changes}
    assert ("classes.ex:Alpha", "rename") in changes
    assert changes[("classes.ex:Delta", "change_extends")] == {
        "from": "ex:Gamma",
        "to": "ex:Beta",
    }
    assert ("relations.ex:link.domain", "change_domain") not in changes


def test_a_rename_without_other_changes_keeps_references_quiet() -> None:
    old = old_model()
    new = new_model(alpha="Gamma", alpha_from="Alpha", beta="Delta", beta_from="Beta")
    assert kinds(classify(old, new)) == [
        ("classes.ex:Alpha", "rename"),
        ("classes.ex:Beta", "rename"),
    ]


def test_the_curie_is_stored_verbatim_and_a_local_name_stays_local() -> None:
    moved = moved_model()
    assert moved.classes["aa:Alpha"].renamed_from == "ex:Alpha"
    assert moved.relations["aa:link"].renamed_from == "ex:link"
    local = new_model(beta="Gamma", beta_from="Beta")
    assert local.classes["ex:Gamma"].renamed_from == "Beta"


def test_a_local_name_means_the_prefix_of_the_new_term() -> None:
    old, new = old_model(), new_model(beta="Gamma", beta_from="Beta")
    assert dict(derive_renames(old, new).classes) == {"ex:Beta": "ex:Gamma"}
    assert check_renames(old, new) == ()


def test_the_old_module_need_not_be_imported() -> None:
    """A CURIE of a prefix the new model does not know is judged against the old model only."""
    new = new_model(beta="Gamma", beta_from="gone:Beta")
    assert problems(old_model(), new) == [("dangling", "error", "classes.ex:Gamma")]


# ---- classes and relations: R1-R6 -------------------------------------------------------------


def test_a_valid_rename_has_no_problem() -> None:
    assert check_renames(old_model(), new_model(beta="Gamma", beta_from="Beta")) == ()


def test_r6_a_core_or_built_in_source_is_refused() -> None:
    new = new_model(beta="Doc", beta_from="files:File", link_from="core:supersedes")
    assert problems(old_model(), new) == [
        ("builtin_source", "error", "classes.ex:Doc"),
        ("builtin_source", "error", "relations.ex:link"),
    ]


def test_r5_two_terms_cannot_share_a_source() -> None:
    more = "  Delta:\n    summary: Fourth.\n    renamed_from: Beta\n"
    new = new_model(beta="Gamma", beta_from="Beta", more=more)
    assert problems(old_model(), new) == [
        ("duplicate_source", "error", "classes.ex:Delta"),
        ("duplicate_source", "error", "classes.ex:Gamma"),
    ]


def test_r2_a_source_that_exists_nowhere_dangles() -> None:
    new = new_model(beta="Gamma", beta_from="Missing")
    assert problems(old_model(), new) == [("dangling", "error", "classes.ex:Gamma")]
    # classify ignores it: the old class is removed and the new one added
    assert ("classes.ex:Beta", "remove_class") in kinds(classify(old_model(), new))


def test_r3_the_source_must_be_gone() -> None:
    more = "  Gamma:\n    summary: Third.\n    renamed_from: Beta\n"
    assert problems(old_model(), new_model(more=more)) == [
        ("source_remains", "error", "classes.ex:Gamma")
    ]


def test_r4_the_target_must_be_new() -> None:
    old = single("1.0.0", body(more="  Gamma:\n    summary: Third.\n"))
    new = new_model(beta="Gamma", beta_from="Beta")
    assert problems(old, new) == [("target_exists", "error", "classes.ex:Gamma")]


def test_r3_and_r4_can_both_hold() -> None:
    old = single("1.0.0", body(more="  Gamma:\n    summary: Third.\n"))
    new = new_model(more="  Gamma:\n    summary: Third.\n    renamed_from: Beta\n")
    assert [p[0] for p in problems(old, new)] == ["source_remains", "target_exists"]


def test_r1_the_leftover_of_the_previous_release_is_a_warning() -> None:
    old = single("2.0.0", body(beta="Gamma", beta_from="Beta"))
    new = single("2.1.0", body(beta="Gamma", beta_from="Beta"))
    assert problems(old, new) == [("leftover", "warning", "classes.ex:Gamma")]
    assert classify(old, new).changes == ()


def test_a_leftover_with_another_source_is_dangling() -> None:
    old = single("2.0.0", body(beta="Gamma", beta_from="Other"))
    new = single("2.1.0", body(beta="Gamma", beta_from="Beta"))
    assert problems(old, new) == [("dangling", "error", "classes.ex:Gamma")]


def test_relations_are_judged_like_classes() -> None:
    old = old_model()
    assert check_renames(old, new_model(link="join", link_from="link")) == ()
    assert problems(old, new_model(link="join", link_from="nope")) == [
        ("dangling", "error", "relations.ex:join")
    ]
    extra = (
        "  join:\n    summary: Join.\n    domain: Alpha\n    range: Beta\n    renamed_from: link\n"
    )
    assert problems(old, single("2.0.0", body() + extra)) == [
        ("source_remains", "error", "relations.ex:join")
    ]
    both = single("1.0.0", body() + extra.replace("    renamed_from: link\n", ""))
    assert problems(both, new_model(link="join", link_from="link")) == [
        ("target_exists", "error", "relations.ex:join")
    ]


# ---- attributes -------------------------------------------------------------------------------


def attributes(*lines: str) -> str:
    return "".join(f"      {line}\n" for line in lines)


def test_an_attribute_rename_is_judged_within_the_class_pair() -> None:
    renamed = attributes(
        "title: {summary: Title., type: string}",
        "extent: {summary: Size., type: string, renamed_from: size}",
    )
    new = new_model(attributes=renamed)
    assert check_renames(old_model(), new) == ()
    assert dict(derive_renames(old_model(), new).attributes) == {("ex:Alpha", "size"): "extent"}


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            (
                "title: {summary: T., type: string}",
                "extent: {summary: S., type: string, renamed_from: nope}",
            ),
            [("dangling", "extent")],
        ),
        (
            (
                "title: {summary: T., type: string}",
                "size: {summary: S., type: string}",
                "extent: {summary: S., type: string, renamed_from: size}",
            ),
            [("source_remains", "extent")],
        ),
        (
            (
                "a: {summary: S., type: string, renamed_from: size}",
                "b: {summary: S., type: string, renamed_from: size}",
                "title: {summary: T., type: string}",
            ),
            [("duplicate_source", "a"), ("duplicate_source", "b")],
        ),
        (
            ("title: {summary: T., type: string, renamed_from: size}",),
            [("target_exists", "title")],
        ),
    ],
    ids=["dangling", "source_remains", "duplicate_source", "target_exists"],
)
def test_attribute_problems(lines: tuple[str, ...], expected: list[tuple[str, str]]) -> None:
    found = problems(old_model(), new_model(attributes=attributes(*lines)))
    assert found == [
        (rule, "error", f"classes.ex:Alpha.attributes.{name}") for rule, name in expected
    ]


def test_an_attribute_leftover_is_a_warning() -> None:
    renamed = attributes(
        "title: {summary: Title., type: string}",
        "extent: {summary: Size., type: string, renamed_from: size}",
    )
    old, new = single("2.0.0", body(attributes=renamed)), single("2.1.0", body(attributes=renamed))
    assert problems(old, new) == [("leftover", "warning", "classes.ex:Alpha.attributes.extent")]


def test_an_attribute_of_a_new_class_dangles() -> None:
    more = (
        "  Gamma:\n    summary: Third.\n    attributes:\n"
        "      x: {summary: X., type: string, renamed_from: size}\n"
    )
    assert problems(old_model(), new_model(more=more)) == [
        ("dangling", "error", "classes.ex:Gamma.attributes.x")
    ]


def test_attributes_follow_a_renamed_class() -> None:
    renamed = attributes(
        "title: {summary: Title., type: string}",
        "extent: {summary: Size., type: string, renamed_from: size}",
    )
    old, new = old_model(), new_model(alpha="Gamma", alpha_from="Alpha", attributes=renamed)
    assert check_renames(old, new) == ()
    renames = derive_renames(old, new)
    assert dict(renames.classes) == {"ex:Alpha": "ex:Gamma"}
    assert dict(renames.attributes) == {("ex:Gamma", "size"): "extent"}


def test_the_problems_serialize() -> None:
    problem = RenameProblem("dangling", "error", "class", "classes.ex:X", "ex:Y", "message")
    assert problem.to_json() == {
        "rule": "dangling",
        "severity": "error",
        "kind": "class",
        "path": "classes.ex:X",
        "source": "ex:Y",
        "message": "message",
    }


# ---- derive_renames agrees with classify --------------------------------------------------------


def reported(report: DiffReport) -> Renames:
    """The renames a diff reports, rebuilt from its ``rename`` changes."""
    classes: dict[str, str] = {}
    relations: dict[str, str] = {}
    pending: list[tuple[str, str, str]] = []
    for change in report.changes:
        if change.kind != "rename":
            continue
        section, _, rest = change.path.partition(".")
        target = str(change.detail["to"])
        if ".attributes." in rest:
            owner, _, name = rest.partition(".attributes.")
            pending.append((owner, name, target))
        elif section == "classes":
            classes[rest] = target
        else:
            relations[rest] = target
    attributes = {(classes.get(owner, owner), name): target for owner, name, target in pending}
    return Renames(classes, relations, attributes)


def pair_cases() -> list[tuple[str, CompiledModel, CompiledModel]]:
    cases = [
        (f"{name} {a}->{b}", compiled_model(name, a), compiled_model(name, b))
        for name in (*TOY_ONTOLOGIES, *MODULAR_ONTOLOGIES)
        for a, b in pairwise(ontology_versions(name))
    ]
    renamed = attributes(
        "title: {summary: Title., type: string}",
        "extent: {summary: Size., type: string, renamed_from: size}",
    )
    cases += [
        ("moved", old_model(), moved_model()),
        ("attribute", old_model(), new_model(attributes=renamed)),
        (
            "class and attribute",
            old_model(),
            new_model(alpha="Gamma", alpha_from="Alpha", attributes=renamed),
        ),
        ("invalid ones ignored", old_model(), new_model(beta="Gamma", beta_from="Missing")),
    ]
    return cases


@pytest.mark.parametrize(
    ("old", "new"), [pytest.param(o, n, id=label) for label, o, n in pair_cases()]
)
def test_derive_renames_agrees_with_classify(old: CompiledModel, new: CompiledModel) -> None:
    derived, expected = derive_renames(old, new), reported(classify(old, new))
    assert dict(derived.classes) == dict(expected.classes)
    assert dict(derived.relations) == dict(expected.relations)
    assert dict(derived.attributes) == dict(expected.attributes)


def test_the_inventory_rename_is_derived() -> None:
    old, new = compiled_model("inventory", "1.1.0"), compiled_model("inventory", "2.0.0")
    renames = derive_renames(old, new)
    assert dict(renames.attributes) == {("inv:Item", "name"): "label"}
    assert renames.to_json() == {
        "classes": {},
        "relations": {},
        "attributes": [{"class": "inv:Item", "from": "name", "to": "label"}],
    }


def test_the_inventory_series_has_one_leftover_and_no_error() -> None:
    found = {
        f"{a}->{b}": [
            (p.rule, p.path)
            for p in check_renames(compiled_model("inventory", a), compiled_model("inventory", b))
        ]
        for a, b in pairwise(ontology_versions("inventory"))
    }
    assert found == {
        "1.0.0->1.0.1": [],
        "1.0.1->1.1.0": [],
        "1.1.0->2.0.0": [],
        "2.0.0->2.1.0": [("leftover", "classes.inv:Item.attributes.label")],
    }
