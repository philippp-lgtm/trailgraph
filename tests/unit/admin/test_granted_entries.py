"""The warnings of a publish for the entries a catalog grant names (catalog callers plan, step 4):
``granted_entries.warnings`` over the installed policy, the published slot and the one a publish
would write, and ``terms.entry_grants``, which reads the grants back from the compiled policy.

The policies are compiled from their authored form; the slots hold only what the comparison
reads (the names and hashes of the compiled entries and what a removed entry names as its
replacement).
"""

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.access_control.terms import EntryGrant, entry_grants, named_entries
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SlotKind
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import OntologyPin
from graph_cli_admin.services_admin import granted_entries
from graph_cli_admin.services_admin.catalog_follow import Followed, Republish
from graph_cli_admin.services_admin.release import republish_warnings

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
PIN = OntologyPin("sample", "1.0.0", "sha256:" + "0" * 64)
ENTRY = GraphEntry(EXAMPLE, "example_graph", "active", PIN, {}, ())
SOURCE = """\
schema: graph_cli.policy/1
id: installation
roles:
  support: {description: "Support agents"}
  triage: {description: "Triage"}
grants:
  - entrypoint: server
    principal: {role: support}
    graphs: [example_graph]
    level: catalog
    catalog:
      functions: [open_tickets, escalations]
      actions: [close_ticket]
      views: [ticket_overview]
    reads: ["ex:Ticket"]
  - entrypoint: server
    principal: {role: triage, project: shop}
    graphs: [example_graph, other_graph]
    level: catalog
    catalog: {functions: [open_tickets]}
    reads: ["ex:Ticket"]
  - entrypoint: server
    principal: {role: triage}
    graphs: [other_graph]
    level: read
    catalog: {actions: [other_action]}
no_project: {"*": write}
"""


def installed(source: str = SOURCE) -> PolicySet:
    document = compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS).document
    return policy_set(document)


def slot(kind: SlotKind, **entries: str | tuple[str, str | None]) -> KineticSlot:
    """A catalog: ``name=hash`` or ``name=(hash, replaced_by)``."""
    items: list[JsonValue] = []
    for name, value in entries.items():
        digest, successor = value if isinstance(value, tuple) else (value, None)
        items.append({"name": name, "hash": digest, "replaced_by": successor})
    key = {"functions": "functions", "actions": "actions", "views": "views"}[kind]
    return KineticSlot("s", "c", PIN, {key: items}, kind=kind)


def codes(found: tuple[object, ...]) -> list[tuple[str, str]]:
    return [(w.code, w.details["name"]) for w in found]  # type: ignore[attr-defined]


def test_grants_are_read_back_per_kind_and_name_with_their_principal() -> None:
    found = entry_grants(installed().documents, EXAMPLE)
    support = EntryGrant("grant.1", (("role", "support"),))
    triage = EntryGrant("grant.2", (("project", "shop"), ("role", "triage")))
    assert found == {
        "function": {"escalations": (support,), "open_tickets": (support, triage)},
        "action": {"close_ticket": (support,)},
        "view": {"ticket_overview": (support,)},
    }
    # the other graph has its own entries; a read grant's catalog.actions counts too
    other = entry_grants(installed().documents, OTHER)
    assert sorted(other["function"]) == ["open_tickets"] and sorted(other["action"]) == [
        "other_action"
    ]
    assert other["action"]["other_action"] == (EntryGrant("grant.3", (("role", "triage"),)),)
    # named_entries is the names of the same reading
    assert named_entries(installed().documents, EXAMPLE) == {
        "function": ("escalations", "open_tickets"),
        "action": ("close_ticket",),
        "view": ("ticket_overview",),
    }


def test_a_policy_without_catalog_grants_names_no_entry() -> None:
    assert entry_grants(BUILTIN_POLICY_SET.documents, EXAMPLE) == {
        "function": {},
        "action": {},
        "view": {},
    }


def test_a_removed_granted_entry_warns_once_per_name_with_its_grants_and_replacement() -> None:
    before = slot("functions", open_tickets=("h1", "escalations"), escalations="h2", other="h3")
    after = slot("functions", escalations="h2")
    found = granted_entries.warnings(installed(), ENTRY, "functions", before, after)
    assert codes(found) == [("catalog.granted_entry_removed", "open_tickets")]
    details = found[0].details
    assert details["replaced_by"] == "escalations" and details["kind"] == "function"
    assert [g["grant"] for g in details["grants"]] == ["grant.1", "grant.2"]  # type: ignore[union-attr, index]
    assert "escalations" in found[0].message and "does not cover" in found[0].message
    # an entry that names no replacement says what the callers lose instead
    plain = granted_entries.warnings(
        installed(), ENTRY, "functions", slot("functions", escalations="h2"), slot("functions")
    )
    assert plain[0].details["replaced_by"] is None and "lose" in plain[0].message


def test_a_changed_granted_entry_warns_with_both_hashes_and_an_equal_one_does_not() -> None:
    before = slot("functions", open_tickets="h1", escalations="h2", other="h3")
    after = slot("functions", open_tickets="h1x", escalations="h2", other="h3x")
    found = granted_entries.warnings(installed(), ENTRY, "functions", before, after)
    assert codes(found) == [("catalog.granted_entry_changed", "open_tickets")]
    assert (found[0].details["hash_before"], found[0].details["hash_after"]) == ("h1", "h1x")
    same = granted_entries.warnings(installed(), ENTRY, "functions", before, before)
    assert same == ()


def test_the_kinds_are_judged_by_their_own_names() -> None:
    """A function and an action of one name are different entries; a name granted as an action
    is not warned about for a function slot."""
    grants = installed()
    gone = granted_entries.warnings(
        grants, ENTRY, "actions", slot("actions", close_ticket="h1"), slot("actions")
    )
    assert codes(gone) == [("catalog.granted_entry_removed", "close_ticket")]
    assert (
        granted_entries.warnings(
            grants, ENTRY, "functions", slot("functions", close_ticket="h1"), slot("functions")
        )
        == ()
    )
    views = granted_entries.warnings(
        grants,
        ENTRY,
        "views",
        slot("views", ticket_overview="h1"),
        slot("views", ticket_overview="h2"),
    )
    assert codes(views) == [("catalog.granted_entry_changed", "ticket_overview")]


def test_only_the_grants_of_the_graph_count() -> None:
    other = GraphEntry(OTHER, "other_graph", "active", PIN, {}, ())
    before, after = slot("actions", close_ticket="h1", other_action="h2"), slot("actions")
    assert codes(granted_entries.warnings(installed(), other, "actions", before, after)) == [
        ("catalog.granted_entry_removed", "other_action")
    ]
    assert codes(granted_entries.warnings(installed(), ENTRY, "actions", before, after)) == [
        ("catalog.granted_entry_removed", "close_ticket")
    ]


def test_an_entry_the_catalog_did_not_hold_is_no_removal_and_no_change() -> None:
    grants = installed()
    nothing = granted_entries.warnings(
        grants, ENTRY, "functions", None, slot("functions", open_tickets="h1")
    )
    appeared = granted_entries.warnings(
        grants,
        ENTRY,
        "functions",
        slot("functions", other="h0"),
        slot("functions", open_tickets="h1"),
    )
    assert nothing == () and appeared == ()
    # nothing published before, nothing after
    assert granted_entries.warnings(grants, ENTRY, "functions", None, None) == ()


def test_unpublishing_a_whole_catalog_warns_for_every_granted_name() -> None:
    before = slot("functions", open_tickets="h1", escalations="h2")
    found = granted_entries.warnings(installed(), ENTRY, "functions", before, None)
    assert codes(found) == [
        ("catalog.granted_entry_removed", "escalations"),
        ("catalog.granted_entry_removed", "open_tickets"),
    ]


def test_without_catalog_grants_nothing_is_judged() -> None:
    before = slot("functions", open_tickets="h1")
    assert (
        granted_entries.warnings(BUILTIN_POLICY_SET, ENTRY, "functions", before, slot("functions"))
        == ()
    )


def test_the_codes_are_registered_warnings() -> None:
    from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code

    for code in ("catalog.granted_entry_removed", "catalog.granted_entry_changed"):
        spec = lookup_error_code(code)
        assert spec is not None and spec.category is ErrorCategory.WARNING


def test_a_release_judges_each_catalog_it_compiles_again_by_the_same_rules() -> None:
    """``release.republish_warnings`` hands the published and the recompiled slot of each
    republished catalog to ``warnings``: a changed hash warns, so does an entry the recompiled
    catalog would lack (not expected of a recompile, judged all the same); a catalog the
    release leaves alone is not looked at."""
    functions = Republish(
        slot("functions", open_tickets="a", escalations="b"),
        slot("functions", open_tickets="a2", escalations="b"),
        ("open_tickets",),
    )
    views = Republish(slot("views", ticket_overview="v"), slot("views"), ("ticket_overview",))
    found = republish_warnings(
        installed(),
        ENTRY,
        (Followed(functions, []), Followed(None, []), Followed(views, [])),
    )
    assert [(w.code, w.details["kind"], w.details["name"]) for w in found] == [
        ("catalog.granted_entry_changed", "function", "open_tickets"),
        ("catalog.granted_entry_removed", "view", "ticket_overview"),
    ]
    assert republish_warnings(installed(), ENTRY, (Followed(None, []),) * 3) == ()
    assert republish_warnings(BUILTIN_POLICY_SET, ENTRY, (Followed(functions, []),)) == ()
