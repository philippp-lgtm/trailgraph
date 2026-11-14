"""Alias prefixes seeded at graph create (B1 open point) and merged by a release: seeds,
renames (also across modules) and retypes move a prefix with the class, never free it."""

from support.fixtures import ONTOLOGIES
from support.modules import compile_modules, header, single

from graph_cli.kernel.ids import TermRef
from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.migration import Retype
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin.graphs import alias_prefixes, merged_alias_prefixes
from graph_cli_admin.services_admin.ontology import Compiled, compile_source


def test_classes_of_imported_modules_seed_their_prefix() -> None:
    prefixes = alias_prefixes(compile_source(ONTOLOGIES / "notes_and_sources" / "1.1.0"))
    assert prefixes[TermRef("files:File")] == "FILE"
    assert prefixes[TermRef("nas:Scan")] == "SCAN"
    assert prefixes[TermRef("nas:Note")] == "NOTE"


def cls(name: str, prefix: str | None = None, *, renamed_from: str | None = None) -> str:
    lines = [f"  {name}:", "    summary: A class."]
    if prefix is not None:
        lines.append(f"    display_prefix: {prefix}")
    if renamed_from is not None:
        lines.append(f"    renamed_from: {renamed_from}")
    return "\n".join(lines) + "\n"


def model(version: str, *classes: str) -> CompiledModel:
    return single(version, "classes:\n" + "".join(classes))


def merged(
    current: dict[str, str],
    new: CompiledModel,
    *,
    old: CompiledModel | None = None,
    retypes: tuple[Retype, ...] = (),
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    renames = derive_renames(old, new) if old is not None else Renames({}, {}, {})
    result = merged_alias_prefixes(
        {TermRef(k): v for k, v in current.items()},
        Compiled(new, (), {}),
        renames=renames,
        retypes=retypes,
    )
    return (
        {str(k): v for k, v in result.merged.items()},
        {str(k): v for k, v in result.skipped.items()},
        {str(k): v for k, v in result.added.items()},
        {str(k): v for k, v in result.moved.items()},
    )


def test_a_release_without_renames_only_seeds_new_classes() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"))
    new = model("1.1.0", cls("Alpha", "ALP"), cls("Beta", "BET"), cls("Gamma"))
    assert merged({"ex:Alpha": "ALP"}, new, old=old) == (
        {"ex:Alpha": "ALP", "ex:Beta": "BET"},
        {},
        {"ex:Beta": "BET"},
        {},
    )


def test_a_local_rename_replaces_the_entry_of_the_old_class() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"))
    new = model("2.0.0", cls("Gamma", "ALP", renamed_from="Alpha"))
    result = merged({"ex:Alpha": "ALP"}, new, old=old)
    assert result == (
        {"ex:Gamma": "ALP"},
        {},
        {},
        {"ex:Gamma": {"from": "ex:Alpha", "prefix": "ALP"}},
    )


def test_a_rename_keeps_the_prefix_of_the_old_class_whatever_the_new_seed() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"))
    new = model("2.0.0", cls("Gamma", "GAM", renamed_from="Alpha"))
    assert merged({"ex:Alpha": "ALP"}, new, old=old)[0] == {"ex:Gamma": "ALP"}


def test_a_curie_rename_across_modules_moves_the_prefix() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"), cls("Beta", "BET"))
    area = (
        header("area_a", "aa", "1.0.0")
        + "classes:\n"
        + cls("Alpha", "ALP", renamed_from="ex:Alpha")
    )
    main = header("example", "ex", "2.0.0", ("area_a",)) + "classes:\n" + cls("Beta", "BET")
    new = compile_modules("example", {"example": main, "area_a": area})
    manifest = {"ex:Alpha": "ALP", "ex:Beta": "BET"}
    assert merged(manifest, new, old=old) == (
        {"aa:Alpha": "ALP", "ex:Beta": "BET"},
        {},
        {},
        {"aa:Alpha": {"from": "ex:Alpha", "prefix": "ALP"}},
    )


def test_a_removed_class_keeps_its_prefix_reserved() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"), cls("Beta", "BET"))
    new = model("2.0.0", cls("Beta", "BET"), cls("Gamma", "ALP"))
    assert merged({"ex:Alpha": "ALP", "ex:Beta": "BET"}, new, old=old) == (
        {"ex:Alpha": "ALP", "ex:Beta": "BET"},
        {"ex:Gamma": "ALP"},
        {},
        {},
    )


def retype(source: str, target: str) -> Retype:
    return Retype(source, target, {})


def test_a_retype_carries_the_prefix_of_a_removed_class_to_the_target() -> None:
    new = model("2.0.0", cls("Beta"), cls("Gamma"))
    result = merged({"ex:Alpha": "ALP"}, new, retypes=(retype("ex:Alpha", "ex:Gamma"),))
    assert result == (
        {"ex:Gamma": "ALP"},
        {},
        {},
        {"ex:Gamma": {"from": "ex:Alpha", "prefix": "ALP"}},
    )


def test_a_retype_target_that_declares_the_same_prefix_takes_it_over() -> None:
    new = model("2.0.0", cls("Gamma", "ALP"))
    result = merged({"ex:Alpha": "ALP"}, new, retypes=(retype("ex:Alpha", "ex:Gamma"),))
    assert result[0] == {"ex:Gamma": "ALP"}
    assert result[1] == {}


def test_a_retype_target_with_its_own_prefix_in_the_manifest_keeps_it() -> None:
    new = model("2.0.0", cls("Gamma", "GAM"))
    manifest = {"ex:Alpha": "ALP", "ex:Gamma": "GAM"}
    result = merged(manifest, new, retypes=(retype("ex:Alpha", "ex:Gamma"),))
    assert result == (manifest, {}, {}, {})


def test_a_retype_target_with_another_display_prefix_gets_its_own() -> None:
    new = model("2.0.0", cls("Gamma", "GAM"))
    result = merged({"ex:Alpha": "ALP"}, new, retypes=(retype("ex:Alpha", "ex:Gamma"),))
    assert result == ({"ex:Alpha": "ALP", "ex:Gamma": "GAM"}, {}, {"ex:Gamma": "GAM"}, {})


def test_a_retype_from_class_that_still_exists_carries_nothing() -> None:
    new = model("2.0.0", cls("Alpha", "ALP"), cls("Gamma"))
    result = merged({"ex:Alpha": "ALP"}, new, retypes=(retype("ex:Alpha", "ex:Gamma"),))
    assert result == ({"ex:Alpha": "ALP"}, {}, {}, {})


def test_two_retypes_into_one_class_carry_the_first_prefix_only() -> None:
    new = model("2.0.0", cls("Gamma"))
    manifest = {"ex:Alpha": "ALP", "ex:Beta": "BET"}
    retypes = (retype("ex:Alpha", "ex:Gamma"), retype("ex:Beta", "ex:Gamma"))
    merged_map, _, _, moved = merged(manifest, new, retypes=retypes)
    assert merged_map == {"ex:Gamma": "ALP", "ex:Beta": "BET"}
    assert list(moved) == ["ex:Gamma"]


def test_an_explicit_rename_wins_over_a_retype_into_the_same_class() -> None:
    old = model("1.0.0", cls("Alpha", "ALP"), cls("Beta", "BET"))
    new = model("2.0.0", cls("Gamma", renamed_from="Alpha"))
    result = merged(
        {"ex:Alpha": "ALP", "ex:Beta": "BET"},
        new,
        old=old,
        retypes=(retype("ex:Beta", "ex:Gamma"),),
    )
    assert result[0] == {"ex:Gamma": "ALP", "ex:Beta": "BET"}


def test_the_prefix_changes_have_the_shape_of_the_pin_intent() -> None:
    new = model("2.0.0", cls("Gamma"), cls("Delta", "DEL"))
    result = merged_alias_prefixes(
        {TermRef("ex:Alpha"): "ALP"},
        Compiled(new, (), {}),
        renames=Renames({}, {}, {}),
        retypes=(retype("ex:Alpha", "ex:Gamma"),),
    )
    assert result.to_json() == {
        "added": {"ex:Delta": "DEL"},
        "moved": {"ex:Gamma": {"from": "ex:Alpha", "prefix": "ALP"}},
    }
