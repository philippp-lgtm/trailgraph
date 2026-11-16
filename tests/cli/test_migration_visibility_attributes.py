"""S: the migration surfaces of a read view that hides attributes (field visibility plan R3, R4,
R5): ``history``'s ``migration`` item, ``names_mapped``, revisions whose names the map cannot
vouch for, ``--as-of`` reads of a state written before a rename, the ``--as-of`` warning and
the rename hints.

``example_graph`` starts on ``staff`` 1.0.0; release 2.0.0 renames the hidden ``email`` to
``private_email`` and the visible ``phone`` to ``work_phone``, retypes badges to access cards
(the hidden label ``holder_name`` becomes ``card_holder``), fills the hidden ``weekly_hours``,
maps the hidden ``pay_grade`` value ``junior`` to ``entry`` and drops the hidden
``birth_date``. Without an installation policy the local app caller holds no marking; ``HOLDER``
grants them all.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.markings import HOLDER, STAFF, recomputed
from support.read_limits import install

GRAPH = {"GRAPH_CLI_GRAPH": "example_graph"}
E1, E2, E3 = "EMP-0001", "EMP-0002", "EMP-0003"
B1 = "BDG-0001"
K1 = "CON-0001"
HOLDER_ONE_GRAPH = HOLDER.replace("  - {entrypoint: app, graphs: [twin_graph], level: write}\n", "")
BUILD = {
    "reason": "build",
    "ops": [
        {
            "op": "create",
            "type": "stf:Badge",
            "local": "b1",
            "attrs": {"holder_name": "Ada Lovelace", "number": 1},
        },
        {
            "op": "create",
            "type": "stf:Employee",
            "local": "e1",
            "attrs": {
                "name": "Ada",
                "phone": "+1 555 0101",
                "email": "ada@private.example",
                "birth_date": "1990-01-01",
                "pay_grade": "junior",
                "weekly_hours": 30,
            },
            "edges": [{"rel": "stf:holds_badge", "target": "@b1"}],
        },
        {"op": "create", "type": "stf:Employee", "local": "e2", "attrs": {"name": "Bo"}},
        {
            "op": "create",
            "type": "stf:Employee",
            "local": "e3",
            "attrs": {"name": "Cy", "email": "cy@private.example", "weekly_hours": 20},
        },
        {
            "op": "create",
            "type": "stf:Contractor",
            "local": "k1",
            "attrs": {"name": "Dee", "email": "dee@agency.example", "agency": "Temps"},
        },
    ],
}
SECRETS = (
    "ada@private.example",
    "cy@private.example",
    "1990-01-01",
    "junior",
    "Ada Lovelace",
    "private_email",
    "card_holder",
    "holder_name",
    "birth_date",
    "pay_grade",
    "weekly_hours",
)
PHONE = """      phone:
        summary: Work phone number.
        type: string
"""
EMAIL = """      email:
        summary: Private e-mail address.
        type: string
        markings: [personal]
"""
BIRTH_DATE = """      birth_date:
        summary: Date of birth.
        type: date
        markings: [personal]
"""
BADGE = """  Badge:
    summary: An access badge.
    display_prefix: BDG
    attributes:
      holder_name:
"""
EMPLOYEE_RULE = """    scope: Employee
    if:
      - {attr: email, value_presence: PRESENT}
    then:
      - {attr: phone, value_presence: PRESENT}
"""
FULL = {
    PHONE: PHONE.replace("phone:", "work_phone:") + "        renamed_from: phone\n",
    EMAIL: EMAIL.replace("email:", "private_email:") + "        renamed_from: email\n",
    BIRTH_DATE: "",
    "      junior: {summary: Junior.}": "      entry: {summary: Entry level.}",
    BADGE: BADGE.replace("Badge:", "AccessCard:").replace("holder_name", "card_holder"),
    "    range: Badge\n": "    range: AccessCard\n",
    EMPLOYEE_RULE: EMPLOYEE_RULE.replace("attr: email", "attr: private_email").replace(
        "attr: phone", "attr: work_phone"
    ),
}
FULL_STEPS = """\
  - retype: {from: stf:Badge, to: stf:AccessCard, attributes: {holder_name: card_holder}}
  - fill: {type: stf:Employee, attribute: weekly_hours, value: "38"}
  - map_values: {type: stf:Employee, attribute: pay_grade, values: {junior: entry}}
  - drop: {type: stf:Employee, attribute: birth_date}
"""
# Only the hidden e-mail is renamed.
HIDDEN_RENAME = {
    EMAIL: FULL[EMAIL],
    EMPLOYEE_RULE: EMPLOYEE_RULE.replace("attr: email", "attr: private_email"),
}


def version(
    tmp_path: Path, number: str, edits: dict[str, str], steps: str | None, base: Path | None = None
) -> Path:
    """The staff module (``base``: 1.0.0) as ``number`` with ``edits`` applied, and a migration
    from the version before with ``steps``."""
    source = base or STAFF / "1.0.0"
    text = (source / "staff.yaml").read_text()
    previous = text.split("  version: ")[1].split("\n")[0]
    text = text.replace(f"version: {previous}", f"version: {number}")
    for old, new in edits.items():
        assert old in text, old
        text = text.replace(old, new)
    target = tmp_path / number
    target.mkdir()
    (target / "staff.yaml").write_text(text)
    if steps is not None:
        (target / "migration.yaml").write_text(
            f"format: graph_cli.migration/1\nfrom: {previous}\nsummary: Test.\nsteps:\n{steps}"
        )
    return target


def release(run_cli: CliRunner, source: Path) -> None:
    outcome = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert outcome.code == 0, outcome.envelope


def ask(run_cli: CliRunner, *args: str) -> Outcome:
    return app(run_cli, *args, env=GRAPH)


def ok(run_cli: CliRunner, *args: str) -> Outcome:
    outcome = ask(run_cli, *args)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def clean(outcome: Outcome, *allowed: str) -> None:
    text = json.dumps(outcome.envelope)
    found = [s for s in SECRETS if s in text and s not in allowed]
    assert found == [], (found, outcome.envelope)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    write_documents(root, "example_graph", [BUILD])
    return root


@pytest.fixture
def migrated(run_cli: CliRunner, root: Path, tmp_path: Path) -> Path:
    release(run_cli, version(tmp_path, "2.0.0", FULL, FULL_STEPS))
    return tmp_path


def migration_of(outcome: Outcome) -> dict[str, Any]:
    items = [i for i in outcome.data["items"] if "migration" in i]
    assert len(items) == 1, outcome.data
    migration: dict[str, Any] = items[0]["migration"]
    return migration


# ---- history ---------------------------------------------------------------------------


def test_the_migration_item_shows_what_happened_to_visible_attributes_only(
    run_cli: CliRunner, migrated: Path
) -> None:
    ada = ask(run_cli, "history", E1)
    clean(ada)
    item = migration_of(ada)
    assert item["renames"] == [
        {"kind": "rename_attribute", "class": "stf:Colleague", "from": "phone", "to": "work_phone"}
    ]
    assert item["steps"] == []  # pay_grade mapped, birth_date dropped: both hidden
    bo = migration_of(ask(run_cli, "history", E2))
    assert bo["renames"] == [] and bo["steps"] == []  # weekly_hours filled: hidden
    badge = ask(run_cli, "history", B1)
    clean(badge)
    assert migration_of(badge)["steps"] == [
        {
            "kind": "retype",
            "from": "stf:Badge",
            "to": "stf:AccessCard",
            "attributes": {},
            "index": 0,
        }
    ]
    # the holder sees every effect
    install(run_cli, migrated / "policies", HOLDER_ONE_GRAPH)
    ada = migration_of(ok(run_cli, "history", E1))
    assert [(r["from"], r["to"]) for r in ada["renames"]] == [
        ("phone", "work_phone"),
        ("email", "private_email"),
    ]
    assert [s["kind"] for s in ada["steps"]] == ["map_values", "drop"]
    bo = migration_of(ok(run_cli, "history", E2))
    assert [(s["kind"], s.get("value")) for s in bo["steps"]] == [("fill", "38")]
    badge = migration_of(ok(run_cli, "history", B1))
    assert badge["steps"][0]["attributes"] == {"holder_name": "card_holder"}


def test_changed_names_come_from_stripped_revisions(run_cli: CliRunner, migrated: Path) -> None:
    ada = ok(run_cli, "history", E1)
    changed = [i["changed"] for i in ada.data["items"]]
    assert changed[0] is not None and changed[0]["attrs"] == []
    assert changed[1] is None  # the first revision
    clean(ada)


# ---- old revisions -----------------------------------------------------------------------


def test_names_mapped_is_set_only_when_mapping_changed_what_the_caller_sees(
    run_cli: CliRunner, migrated: Path
) -> None:
    ada = ok(run_cli, "get", E1, "--rev", "1", "--full").data["entity"]
    assert ada["names_mapped"] is True  # phone became work_phone
    assert ada["attrs"] == {"name": "Ada", "stage": "started", "work_phone": "+1 555 0101"}
    assert ada["content_hash"] == recomputed(ada)
    # Cy only had hidden attributes renamed: nothing the caller sees was mapped
    cy = ok(run_cli, "get", E3, "--rev", "1", "--full").data["entity"]
    assert "names_mapped" not in cy and cy["attrs"] == {"name": "Cy", "stage": "started"}
    bo = ok(run_cli, "get", E2, "--rev", "1", "--full").data["entity"]
    assert "names_mapped" not in bo
    badge = ok(run_cli, "get", B1, "--rev", "1", "--full").data["entity"]
    assert badge["names_mapped"] is True and badge["type"] == "stf:AccessCard"
    assert badge["attrs"] == {"number": 1}
    for ref in (E1, E3, B1):
        clean(ask(run_cli, "get", ref, "--rev", "1", "--full"))
    install(run_cli, migrated / "policies", HOLDER_ONE_GRAPH)
    cy = ok(run_cli, "get", E3, "--rev", "1", "--full").data["entity"]
    assert cy["names_mapped"] is True and cy["attrs"]["private_email"] == "cy@private.example"


def test_revisions_behind_a_hop_whose_release_files_are_gone_keep_no_attribute(
    run_cli: CliRunner, root: Path, migrated: Path
) -> None:
    (graph,) = (root / "graphs").iterdir()
    shutil.rmtree(graph / "ontology" / "1.0.0")
    # the name map cannot vouch for the names of 1.0.0 revisions: no attribute at all
    ada = ok(run_cli, "get", E1, "--rev", "1", "--full").data["entity"]
    assert ada["attrs"] == {} and ada["content_hash"] == recomputed(ada)
    assert ada["type"] == "stf:Employee"
    # the contractor was not migrated: its first revision is the current one, which fits the pin
    contractor = ok(run_cli, "get", K1, "--rev", "1", "--full").data["entity"]
    assert contractor["attrs"]["email"] == "dee@agency.example"
    history = ask(run_cli, "history", E1)
    clean(history)
    assert migration_of(history)["renames"] == [] and migration_of(history)["steps"] == []
    # the current revisions fit the pin: shown as usual
    assert ok(run_cli, "get", E1).data["entity"]["attrs"]["work_phone"] == "+1 555 0101"
    # an earlier state read as of then: its revisions keep no attribute either
    found = ok(run_cli, "find", "--as-of", "tx:1", "--where", "email:exists=true")
    assert found.data["items"] == []
    state = ok(run_cli, "find", "--as-of", "tx:1", "--limit", "100").data["items"]
    assert state and all(item["attrs"] == {} for item in state)
    # a holder reads them as stored
    install(run_cli, migrated / "policies", HOLDER_ONE_GRAPH)
    raw = ok(run_cli, "get", E1, "--rev", "1").data["entity"]["attrs"]
    assert raw["email"] == "ada@private.example"


# ---- earlier states --------------------------------------------------------------------


def test_as_of_filters_and_sorts_hidden_attributes_renamed_in_the_hop(
    run_cli: CliRunner, migrated: Path
) -> None:
    # the old name is the contractors' visible e-mail: an employee's mapped e-mail never matches
    old = ok(run_cli, "find", "--as-of", "tx:1", "--where", "email:exists=true")
    assert [i["alias"] for i in old.data["items"]] == [K1]
    clean(old)
    # the current name is hidden: unknown, as a name no type declares
    for args in (
        ("--where", "private_email:exists=true"),
        ("--sort", "private_email"),
    ):
        hidden = ask(run_cli, "find", "--as-of", "tx:1", "--type", "stf:Employee", *args)
        other = [a.replace("private_email", "no_such_field") for a in args]
        missing = ask(run_cli, "find", "--as-of", "tx:1", "--type", "stf:Employee", *other)
        assert hidden.error["code"] == missing.error["code"] == "term.unknown"
        hidden.error["details"].pop("did_you_mean", None)
        missing.error["details"].pop("did_you_mean", None)
        assert (
            json.loads(json.dumps(hidden.error).replace("private_email", "no_such_field"))
            == missing.error
        )
    sorted_ = ok(run_cli, "find", "--as-of", "tx:1", "--type", "stf:Employee", "--sort", "name")
    clean(sorted_)
    install(run_cli, migrated / "policies", HOLDER_ONE_GRAPH)
    holder = ok(
        run_cli,
        "find",
        "--as-of",
        "tx:1",
        "--where",
        "private_email:exists=true",
        "--sort",
        "private_email:desc",
    )
    assert [i["alias"] for i in holder.data["items"]] == [E3, E1]


def warning_of(outcome: Outcome) -> dict[str, Any]:
    found = [w for w in outcome.warnings if w["code"] == "temporal.as_of_read"]
    assert len(found) == 1, outcome.warnings
    return found[0]


def test_the_as_of_warning_speaks_of_renames_the_caller_sees(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    release(run_cli, version(tmp_path, "2.0.0", HIDDEN_RENAME, "  []\n"))
    limited = warning_of(ok(run_cli, "get", E1, "--as-of", "tx:1"))
    assert "names_mapped" not in limited["details"] and "mapped" not in limited["message"]
    assert limited["details"]["ontology_version"] == "1.0.0"
    install(run_cli, tmp_path / "policies", HOLDER_ONE_GRAPH)
    holder = warning_of(ok(run_cli, "get", E1, "--as-of", "tx:1"))
    assert holder["details"]["names_mapped"] is True and "mapped" in holder["message"]


def test_the_as_of_warning_of_a_visible_rename(run_cli: CliRunner, migrated: Path) -> None:
    limited = warning_of(ok(run_cli, "get", E1, "--as-of", "tx:1"))
    assert limited["details"]["names_mapped"] is True


# ---- rename hints ------------------------------------------------------------------------


def hint_free(run_cli: CliRunner, *args: str, asked: str) -> Outcome:
    """The answer with ``asked`` equals the answer with a name nobody ever used."""
    hidden = ask(run_cli, *args)
    missing = ask(run_cli, *(a.replace(asked, "never_used") for a in args))
    assert hidden.error["code"] == missing.error["code"] == "term.unknown", hidden.envelope
    for error in (hidden.error, missing.error):
        error["details"].pop("did_you_mean", None)
    assert "renamed" not in hidden.error["details"], hidden.error
    assert json.loads(json.dumps(hidden.error).replace(asked, "never_used")) == missing.error
    return hidden


def test_rename_hints_never_name_a_hidden_attribute(
    run_cli: CliRunner, migrated: Path, tmp_path: Path
) -> None:
    release(
        run_cli,
        version(
            tmp_path,
            "3.0.0",
            {
                "      private_email:": "      personal_email:",
                "renamed_from: email": "renamed_from: private_email",
                "attr: private_email": "attr: personal_email",
            },
            "  []\n",
            base=migrated / "2.0.0",
        ),
    )
    where = ("find", "--type", "stf:Employee", "--where")
    # directly (the old name of a hidden attribute) and through a hidden intermediate name
    hint_free(run_cli, *where, "email=x", asked="email")
    hint_free(run_cli, *where, "private_email=x", asked="private_email")
    hint_free(run_cli, "get", E1, "--fields", "holder_name", asked="holder_name")
    # a visible rename keeps its hint
    phone = ask(run_cli, *where, "phone=x")
    assert phone.error["details"]["renamed"][0]["to"] == "work_phone"
    install(run_cli, tmp_path / "policies", HOLDER_ONE_GRAPH)
    holder = ask(run_cli, *where, "email=x")
    assert holder.error["details"]["renamed"][0]["to"] == "personal_email"
    holder = ask(run_cli, *where, "private_email=x")
    assert holder.error["details"]["renamed"][0]["to"] == "personal_email"
