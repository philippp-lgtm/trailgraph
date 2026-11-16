"""Reused names of marked attributes, decided by the name map (field visibility plan R2):
the pure judgement over a pin chain of compiled models."""

from collections.abc import Sequence

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.semver import Version, bump_level
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin.marking_names import (
    Downgrade,
    Judgement,
    Returned,
    Reuse,
    judge,
)

HEADER = """\
format: graph_cli.ir/1
module: {{id: sample, prefix: ex, version: {v}, summary: Sample.}}
markings:
  personal: {{summary: Personal data.}}
  financial: {{summary: Money matters.}}
"""
P = ("ex:personal",)


def model(version: str, classes: str) -> CompiledModel:
    text = HEADER.format(v=version) + "classes:\n" + classes
    outcome = compile_bundle("sample", LayeredSource(MappingSource({"sample": text.encode()})))
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def person(attributes: str, name: str = "Person", extra: str = "") -> str:
    return f"  {name}:\n    summary: A class.{extra}\n    attributes:\n{attributes}"


def attr(name: str, *markings: str, extra: str = "") -> str:
    marks = f", markings: [{', '.join(markings)}]" if markings else ""
    return f"      {name}: {{summary: An attribute., type: string{marks}{extra}}}\n"


def run(versions: Sequence[tuple[str, str]], migrations: dict[int, str] | None = None) -> Judgement:
    """The judgement of a chain of ``(version, classes)``; ``migrations[i]`` is the migration
    file of the step from ``versions[i]`` to ``versions[i + 1]``."""
    chain = [v for v, _ in versions]
    models = [model(v, classes) for v, classes in versions]
    hops = {}
    for index in range(len(chain) - 1):
        old, new = models[index], models[index + 1]
        if bump_level(Version.parse(chain[index]), Version.parse(chain[index + 1])) != "major":
            continue
        text = (migrations or {}).get(index)
        doc = None if text is None else parse_migration(text.encode()).doc
        assert text is None or doc is not None
        hops[index] = Hop(chain[index], chain[index + 1], old, new, derive_renames(old, new), doc)
    return judge(chain, models, hops)


def migration(from_version: str, steps: str) -> str:
    return f"format: graph_cli.migration/1\nfrom: {from_version}\nsummary: Test.\nsteps:\n{steps}"


def test_unchanged_marking_is_fine() -> None:
    v1 = person(attr("name") + attr("email", "personal"))
    assert run([("1.0.0", v1), ("1.1.0", v1)]) == Judgement()


def test_marking_removed_in_place_warns() -> None:
    v1 = person(attr("name") + attr("email", "personal", "financial"))
    v2 = person(attr("name") + attr("email", "financial"))
    judgement = run([("1.0.0", v1), ("1.1.0", v2)])
    assert judgement == Judgement(
        downgrades=(Downgrade("ex:Person", "email", "in_place", P, ("ex:financial",)),)
    )


def test_downgrade_warns_once_not_at_every_later_release() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("email"))
    v3 = person(attr("email") + attr("phone"))
    assert run([("1.0.0", v1), ("1.1.0", v2), ("1.2.0", v3)]) == Judgement()


def test_drop_then_reuse_is_refused() -> None:
    v1 = person(attr("name") + attr("email", "personal"))
    v2 = person(attr("name"))
    v3 = person(attr("name") + attr("email"))
    judgement = run([("1.0.0", v1), ("2.0.0", v2), ("2.1.0", v3)])
    assert judgement.reuses == (Reuse("ex:Person", "email", "1.0.0", P, "ex:Person", "email", ()),)


def test_reuse_with_the_markings_is_fine() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("name"))
    v3 = person(attr("email", "personal", "financial"))
    assert run([("1.0.0", v1), ("2.0.0", v2), ("2.1.0", v3)]) == Judgement()


def test_rename_then_reuse_only_returns() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("mail", "personal", extra=", renamed_from: email"))
    v3 = person(attr("mail", "personal") + attr("email"))
    judgement = run([("1.0.0", v1), ("2.0.0", v2), ("2.1.0", v3)])
    assert judgement == Judgement(
        returned=(Returned("ex:Person", "email", "1.0.0", P, "ex:Person.mail"),)
    )
    # one release later the name is back already: no warning again
    v4 = person(attr("mail", "personal") + attr("email") + attr("phone"))
    assert run([("1.0.0", v1), ("2.0.0", v2), ("2.1.0", v3), ("2.2.0", v4)]) == Judgement()


def test_rename_that_drops_the_marking_warns() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("mail", extra=", renamed_from: email"))
    judgement = run([("1.0.0", v1), ("2.0.0", v2)])
    assert judgement.downgrades == (Downgrade("ex:Person", "mail", "rename", P, ()),)
    assert judgement.reuses == ()


def test_implicit_retype_carry_is_refused() -> None:
    v1 = person(attr("email", "personal")) + person(attr("email"), name="Contact")
    v2 = person(attr("email"), name="Contact")
    steps = "  - retype: {from: ex:Person, to: ex:Contact}\n"
    judgement = run([("1.0.0", v1), ("2.0.0", v2)], {0: migration("1.0.0", steps)})
    assert judgement.reuses == (Reuse("ex:Person", "email", "1.0.0", P, "ex:Contact", "email", ()),)


def test_explicit_retype_map_warns() -> None:
    v1 = person(attr("email", "personal")) + person(attr("mail"), name="Contact")
    v2 = person(attr("mail"), name="Contact")
    steps = "  - retype: {from: ex:Person, to: ex:Contact, attributes: {email: mail}}\n"
    judgement = run([("1.0.0", v1), ("2.0.0", v2)], {0: migration("1.0.0", steps)})
    assert judgement == Judgement(
        downgrades=(Downgrade("ex:Contact", "mail", "retype_map", P, ()),)
    )


def test_retype_into_a_shared_parent_declaration_is_the_same_declaration() -> None:
    parent = person(attr("email", "personal"), name="Party", extra="\n    abstract: true")
    child = "\n    is_a: Party"
    v1 = parent + person("", name="Person", extra=child) + person("", name="Contact", extra=child)
    v2 = parent.replace(", markings: [personal]", "") + person("", name="Contact", extra=child)
    steps = "  - retype: {from: ex:Person, to: ex:Contact}\n"
    judgement = run([("1.0.0", v1), ("2.0.0", v2)], {0: migration("1.0.0", steps)})
    assert judgement.reuses == ()
    assert judgement.downgrades == (Downgrade("ex:Party", "email", "in_place", P, ()),)


def test_parent_attribute_redeclared_unmarked_lower_is_refused() -> None:
    child = "\n    is_a: Party"
    v1 = person(attr("email", "personal"), name="Party") + person("", name="Person", extra=child)
    v2 = person(attr("name"), name="Party") + person(attr("email"), name="Person", extra=child)
    judgement = run([("1.0.0", v1), ("2.0.0", v2)])
    # the values of Party.email stay undeclared on Party; those an entity of Person stored
    # under the inherited attribute reach the new, unmarked Person.email
    assert judgement.reuses == (Reuse("ex:Person", "email", "1.0.0", P, "ex:Person", "email", ()),)


def test_class_rename_keeps_the_declaration() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("email"), name="Human", extra="\n    renamed_from: Person")
    judgement = run([("1.0.0", v1), ("2.0.0", v2)])
    assert judgement == Judgement(downgrades=(Downgrade("ex:Human", "email", "in_place", P, ()),))


def test_a_value_carried_into_a_marked_declaration_follows_it() -> None:
    # 1.0.0 -> 2.0.0 carries into an equally marked Contact.email (fine); removing that
    # marking later is a downgrade of Contact.email, not a reuse
    v1 = person(attr("email", "personal")) + person(attr("email", "personal"), name="Contact")
    v2 = person(attr("email", "personal"), name="Contact")
    v3 = person(attr("email"), name="Contact")
    steps = "  - retype: {from: ex:Person, to: ex:Contact}\n"
    judgement = run([("1.0.0", v1), ("2.0.0", v2), ("2.1.0", v3)], {0: migration("1.0.0", steps)})
    assert judgement == Judgement(downgrades=(Downgrade("ex:Contact", "email", "in_place", P, ()),))


def test_a_marking_moved_to_another_name_counts_as_removed() -> None:
    v1 = person(attr("email", "personal"))
    v2 = person(attr("email", "financial"))
    judgement = run([("1.0.0", v1), ("2.0.0", v2)])
    assert judgement.downgrades == (
        Downgrade("ex:Person", "email", "in_place", P, ("ex:financial",)),
    )
