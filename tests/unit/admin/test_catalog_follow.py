"""``catalog_follow``: a published catalog that no longer resolves is compiled again from its
sources when the release allows it, else its problems stand (migration plan M8)."""

import json
from pathlib import Path

from support.fixtures import FIXTURES, compiled_model

from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.ports.catalog import KineticSlot, sources_hash
from graph_cli.kernel.records import OntologyPin
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.view_compile import compile_views
from graph_cli_admin.services_admin.catalog_follow import follow

MODEL = compiled_model("inventory", "1.1.0")
PIN = OntologyPin("inventory", "1.1.0", MODEL.artifact_hash)
OLD_PIN = OntologyPin("inventory", "1.0.0", "sha256:" + "0" * 64)


def sources(kind: str) -> dict[str, bytes]:
    folder: Path = FIXTURES / kind / "inventory" / "valid"
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.yaml"))}


def slot_of(kind: str, files: dict[str, bytes], *, stale: bool) -> KineticSlot:
    """A published catalog; ``stale`` leaves a stored form that no longer resolves although the
    sources compile (what a rename leaves behind)."""
    if kind == "functions":
        compiled, found = compile_catalog(files, MODEL)
        marker = "located_in"
    else:
        compiled, found = compile_actions(files, MODEL)
        marker = "inv:located_in"
    assert not found
    document = json.loads(json.dumps(compiled))
    if stale:
        field = "query" if kind == "functions" else "ops"
        entry = next(e for e in document[kind] if marker in json.dumps(e[field]))
        entry[field] = json.loads(json.dumps(entry[field]).replace(marker, "gone_x", 1))
    return KineticSlot(sources_hash(files), hash_value(document), OLD_PIN, document, files, kind)  # type: ignore[arg-type]


def test_a_catalog_that_resolves_is_left_alone() -> None:
    for kind in ("functions", "actions"):
        slot = slot_of(kind, sources(kind), stale=False)
        found = follow(slot, MODEL, PIN, recompile=True)  # type: ignore[arg-type]
        assert found.republish is None and found.problems == []


def test_a_stale_catalog_is_compiled_again_from_its_sources() -> None:
    for kind in ("functions", "actions"):
        slot = slot_of(kind, sources(kind), stale=True)
        found = follow(slot, MODEL, PIN, recompile=True)  # type: ignore[arg-type]
        assert found.problems == [], kind
        assert found.republish is not None
        again = found.republish.slot
        assert again.hash == slot.hash and again.files == slot.files
        assert again.compiled_hash != slot.compiled_hash and again.published_against == PIN
        assert again.kind == kind and again.count == slot.count
        assert found.republish.names  # the entries that no longer resolved
        # the result is what publishing the same sources against the model stores
        fresh = (
            compile_catalog(slot.files, MODEL)[0]
            if kind == "functions"
            else compile_actions(slot.files, MODEL)[0]
        )
        assert again.compiled == fresh
        # and it resolves: a second look finds nothing to do
        assert follow(again, MODEL, PIN, recompile=True).republish is None


def test_without_a_rename_the_problems_stand() -> None:
    slot = slot_of("functions", sources("functions"), stale=True)
    found = follow(slot, MODEL, PIN, recompile=False)
    assert found.republish is None and found.problems  # the same as the plain check


def test_sources_that_do_not_compile_block() -> None:
    files = sources("functions")
    files["items_in_location.yaml"] = files["items_in_location.yaml"].replace(
        b":located_in", b":gone"
    )
    slot = slot_of("functions", sources("functions"), stale=True)
    slot = KineticSlot(sources_hash(files), slot.compiled_hash, OLD_PIN, slot.compiled, files)
    found = follow(slot, MODEL, PIN, recompile=True)
    assert found.republish is None
    assert "term.unknown" in {p.code for p in found.problems}


def view_slot(*, stale: bool) -> tuple[KineticSlot, tuple[FunctionEntry, ...]]:
    """A published view catalog with the function entries it runs; ``stale`` leaves a stored
    relation that is gone although the sources compile."""
    files = sources("views")
    functions = load_entries(compile_catalog(sources("functions"), MODEL)[0])
    compiled, found = compile_views(files, MODEL, functions)
    assert not found
    document = json.loads(json.dumps(compiled))
    if stale:
        entry = next(e for e in document["views"] if "inv:located_in" in json.dumps(e["sections"]))
        entry["sections"] = json.loads(
            json.dumps(entry["sections"]).replace("inv:located_in", "inv:gone_x")
        )
    slot = KineticSlot(
        sources_hash(files),
        hash_value(document),
        OLD_PIN,
        document,
        files,
        "views",
    )  # type: ignore[arg-type]
    return slot, functions


def test_a_view_catalog_that_holds_is_left_alone() -> None:
    slot, functions = view_slot(stale=False)
    found = follow(slot, MODEL, PIN, recompile=True, functions=functions)
    assert found.republish is None and found.problems == []


def test_a_stale_view_catalog_is_compiled_again_with_the_functions() -> None:
    slot, functions = view_slot(stale=True)
    found = follow(slot, MODEL, PIN, recompile=True, functions=functions)
    assert found.problems == [] and found.republish is not None
    again = found.republish.slot
    assert again.kind == "views" and again.hash == slot.hash and again.files == slot.files
    assert again.compiled_hash != slot.compiled_hash and again.published_against == PIN
    assert again.compiled == compile_views(slot.files, MODEL, functions)[0]
    assert found.republish.names == ("location_overview",)
    assert follow(again, MODEL, PIN, recompile=True, functions=functions).republish is None


def test_a_view_needs_the_functions_the_graph_will_have() -> None:
    slot, functions = view_slot(stale=False)
    gone = follow(slot, MODEL, PIN, recompile=True, functions=functions[:1])
    assert gone.republish is None
    assert {p.code for p in gone.problems} == {"view.unknown_function"}
    stale, _ = view_slot(stale=True)
    blocked = follow(stale, MODEL, PIN, recompile=True, functions=())  # sources do not compile
    assert blocked.republish is None and "view.unknown_function" in {
        p.code for p in blocked.problems
    }


def test_without_a_rename_the_problems_of_a_stale_view_catalog_stand() -> None:
    slot, functions = view_slot(stale=True)
    found = follow(slot, MODEL, PIN, recompile=False, functions=functions)
    assert found.republish is None and found.problems
