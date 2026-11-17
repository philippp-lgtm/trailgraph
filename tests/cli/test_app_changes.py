"""S: ``graph changes``, the change feed (change feed plan CF1-CF14), over a graph from the
company example with published actions: what a page lists per tx, paging by entity items with a
cursor inside a large tx, positions (a seq, a time, above the head, an empty graph), usage
errors, the text form and its next-page hint, and the access log.

Visibility has its own tests: ``test_read_visibility.py`` (type read limits),
``test_read_visibility_attributes.py`` (markings), ``test_migration_changes.py`` (migrations)
and ``tests/server`` (tokens); the denial of ``read.history`` is in ``test_policy.py``.
"""

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.actions import VALID
from support.admin import Outcome, admin, init
from support.app import app
from support.fixtures import EXAMPLES, example_model
from support.services import OpsFactory

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
# A fixed session, so ``own_session`` does not depend on the environment the tests run in.
ENV = {"GRAPH_CLI_GRAPH": "company", "GRAPH_CLI_SESSION_ID": "feed-session"}
FACTORY = OpsFactory(example_model("company", "1.1.0"))


def create(run_cli: CliRunner, cls: str, local: str) -> str:
    """One entity of the class (and what it requires), through ``apply``."""
    applied = app(
        run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": FACTORY.create(cls, local)})
    )
    assert applied.code == 0, applied.envelope
    entities = applied.data["results"][0]["result"]["entities"]
    return str(next(e["id"] for e in entities if e["local"] == local))


def changes(run_cli: CliRunner, *args: str) -> Outcome:
    return app(run_cli, "changes", *args, env=ENV)


def feed(run_cli: CliRunner, since: str = "tx:0", limit: int = 500) -> dict[str, Any]:
    outcome = changes(run_cli, "--since", since, "--limit", str(limit))
    assert outcome.code == 0, outcome.envelope
    data: dict[str, Any] = outcome.data
    return data


@pytest.fixture
def empty(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "company"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    published = admin(run_cli, "graph", "actions", "publish", "company", str(VALID))
    assert published.code == 0, published.envelope
    return root


def test_the_feed_of_an_empty_graph_is_empty(run_cli: CliRunner, empty: Path) -> None:
    data = feed(run_cli)
    assert data == {
        "since": "tx:0",
        "since_tx_id": None,
        "head": 0,
        "next": "tx:0",
        "next_tx_id": None,
        "more": False,
        "view": None,
        "items": [],
    }
    # ``tx:0`` is the only position of an empty graph; a time is the position before any tx
    assert feed(run_cli, "2030-01-01T00:00:00Z")["since"] == "tx:0"
    refused = changes(run_cli, "--since", "tx:1")
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")
    assert refused.error["details"] == {"argument": "--since", "actual": "tx:1", "head_seq": 0}
    text = run_cli("graph", "changes", "--since", "tx:0", env=ENV)
    assert (text.returncode, text.stdout, text.stderr) == (0, "No changes after tx:0.\n", "")


def test_a_page_lists_each_tx_with_its_command_reason_action_and_entities(
    run_cli: CliRunner, empty: Path
) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "q")
    attr = "name"
    updated = app(
        run_cli,
        *("update", person, "--set", f"{attr}=changed value", "--reason", "fix a typo"),
        env=ENV,
    )
    assert updated.code == 0, updated.envelope
    done = app(
        run_cli,
        *(
            "do",
            "assign_position",
            "--param",
            f"person={person}",
            "--param",
            f"position={position}",
        ),
        *("--param", "since=2026-03-01", "--reason", "new recruiter"),
        env=ENV,
    )
    assert done.code == 0, done.envelope
    data = feed(run_cli)
    assert (data["head"], data["next"], data["more"], data["view"]) == (4, "tx:4", False, None)
    assert list(data) == [
        *("since", "since_tx_id", "head", "next", "next_tx_id", "more", "view", "items")
    ]
    first, second, third, fourth = data["items"]
    assert [e["tx_seq"] for e in data["items"]] == [1, 2, 3, 4]
    assert data["next_tx_id"] == fourth["tx_id"] and data["since_tx_id"] is None
    for entry in data["items"]:
        assert list(entry)[:6] == [
            *("tx_seq", "tx_id", "recorded_at", "command", "reason", "own_session")
        ]
        assert entry["own_session"] is True and entry["recorded_at"].endswith("Z")
        assert "partial" not in entry and "migration" not in entry
        for item in entry["entities"]:
            assert list(item) == [*("id", "type", "rev", "record_status", "changed", "targets")]
    assert [(i["id"], i["type"], i["rev"]) for i in first["entities"]] == [
        (person, "ppl:Employee", 1)
    ]
    assert first["entities"][0]["changed"] is None and first["entities"][0]["targets"] == []
    assert (first["command"], first["reason"]) == ("apply", None)
    assert position in [i["id"] for i in second["entities"]]  # created with its department
    assert [i["type"] for i in second["entities"]].count("org:Position") == 1
    [edit] = third["entities"]
    assert (edit["id"], edit["rev"], edit["changed"]["attrs"]) == (person, 2, [attr])
    assert (third["command"], third["reason"]) == ("update", "fix a typo")
    assert "action" not in third
    # a write by ``do``: its action, and the link that changed the person, with its target
    assert (fourth["command"], fourth["reason"]) == ("do", "new recruiter")
    assert fourth["action"]["name"] == "assign_position"
    assert fourth["action"]["hash"].startswith("sha256:")
    [linked] = [i for i in fourth["entities"] if i["id"] == person]
    assert linked["rev"] == 3 and linked["changed"]["edges"]["added"]
    assert linked["targets"] == [position]
    # the same lines as ``history`` shows of the entity
    for item in app(run_cli, "history", person, env=ENV).data["items"]:
        entry = data["items"][item["tx_seq"] - 1]
        mine = next(i for i in entry["entities"] if i["id"] == person)
        assert (mine["rev"], mine["changed"]) == (item["rev"], item["changed"])
        assert (entry["command"], entry["reason"]) == (item["command"], item["reason"])
        assert entry.get("action") == item.get("action")
    # since a position: only what follows
    assert [e["tx_seq"] for e in feed(run_cli, "tx:2")["items"]] == [3, 4]
    assert feed(run_cli, "tx:4")["items"] == []


def test_a_time_is_the_position_after_the_last_tx_recorded_at_or_before_it(
    run_cli: CliRunner, empty: Path
) -> None:
    for local in ("a", "b", "c"):
        create(run_cli, "org:Position", local)
    whole = feed(run_cli)
    second = whole["items"][1]
    by_time = feed(run_cli, second["recorded_at"])
    assert by_time["since"] == "tx:2" and by_time["since_tx_id"] == second["tx_id"]
    assert [e["tx_seq"] for e in by_time["items"]] == [3]
    assert feed(run_cli, "1999-12-31T23:59:59+00:00")["items"] == whole["items"]
    assert feed(run_cli, "2999-01-01T00:00:00Z")["items"] == []
    far = feed(run_cli, "2999-01-01T00:00:00Z")
    assert (far["since"], far["head"], far["more"]) == ("tx:3", 3, False)


def test_a_tx_larger_than_a_page_is_split_and_resumed(run_cli: CliRunner, empty: Path) -> None:
    ops = [op for local in "abcde" for op in FACTORY.create("org:Department", local)]
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    create(run_cli, "org:Department", "z")  # a second tx after it
    whole = feed(run_cli)
    every = [i["id"] for e in whole["items"] for i in e["entities"]]
    assert [e["tx_seq"] for e in whole["items"]] == [1, 2] and len(every) == 6
    assert every[:5] == sorted(every[:5])  # the entities of a tx ascend by id
    got: list[str] = []
    since, pages = "tx:0", []
    while True:
        page = feed(run_cli, since, limit=2)
        pages.append(page)
        got += [i["id"] for e in page["items"] for i in e["entities"]]
        assert page["since"] == since
        since = page["next"]
        if not page["more"]:
            break
    assert got == every
    # the big tx (five entities) is cut twice at two items, each cut says so; the rest of it
    # and the next tx fill the last page
    cut = [(p["next"], [e.get("partial") for e in p["items"]]) for p in pages]
    assert cut == [
        (f"tx:1/{every[1]}", [True]),
        (f"tx:1/{every[3]}", [True]),
        ("tx:2", [None, None]),
    ]
    assert [[len(e["entities"]) for e in p["items"]] for p in pages] == [[2], [2], [1, 1]]
    # every page names the tx the one before ended at
    for before, after in itertools.pairwise(pages):
        assert after["since_tx_id"] == before["next_tx_id"]
        assert after["since"] == before["next"]
    # the cursor of a split tx is a ``tx:<seq>/<id>`` the next call takes as it is
    assert pages[0]["next"] == f"tx:1/{every[1]}" and pages[0]["more"] is True
    resumed = feed(run_cli, pages[0]["next"], limit=500)
    assert [i["id"] for e in resumed["items"] for i in e["entities"]] == every[2:]
    assert resumed["items"][0]["tx_seq"] == 1 and "partial" not in resumed["items"][0]
    # a position at the end of a tx, with and without its last entity
    assert feed(run_cli, f"tx:1/{every[4]}")["items"][0]["tx_seq"] == 2


def test_the_text_form_and_the_next_page_hint(run_cli: CliRunner, empty: Path) -> None:
    person, position = create(run_cli, "ppl:Employee", "p"), create(run_cli, "org:Position", "q")
    done = app(
        run_cli,
        *(
            "do",
            "assign_position",
            "--param",
            f"person={person}",
            "--param",
            f"position={position}",
        ),
        *("--param", "since=2026-03-01", "--reason", "new\nrecruiter"),
        env=ENV,
    )
    assert done.code == 0, done.envelope
    text = run_cli("graph", "changes", "--since", "tx:0", env=ENV)
    assert text.returncode == 0 and text.stderr == "", text.stderr
    lines = text.stdout.splitlines()
    assert lines[0].startswith("tx:1 ") and lines[0].endswith(" apply (own session)")
    assert lines[1] == f"  {person} ppl:Employee rev 1 created"
    do_head = next(line for line in lines if line.startswith("tx:3 "))
    assert do_head.endswith(" do assign_position (own session)")
    index = lines.index(do_head)
    assert lines[index + 1] == "  reason: new recruiter"  # one line, whitespace collapsed
    assert lines[index + 2].startswith(f"  {person} ppl:Employee rev 2 edges added: ")
    # a page that ends early says how to go on, on stderr, and the line works as it is
    short = run_cli("graph", "changes", "--since", "tx:0", "--limit", "1", env=ENV)
    assert short.returncode == 0
    command = "graph changes --graph company --since tx:1 --limit 1"
    assert short.stderr == f"hint: More changes available: {command}\n"
    assert short.stdout.splitlines()[0].startswith("tx:1 ")
    hint = app(run_cli, "changes", "--since", "tx:0", "--limit", "1", env=ENV).hints
    assert hint == [{"text": "More changes available", "command": command}]


def test_the_default_page_size_is_not_repeated_in_the_hint(run_cli: CliRunner, empty: Path) -> None:
    ops = [op for n in range(101) for op in FACTORY.create("org:Position", f"n{n}")]
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    page = app(run_cli, "changes", "--since", "tx:0", env=ENV)  # the default page: 100 items
    assert page.code == 0, page.envelope
    assert page.data["more"] is True and page.data["items"][0]["partial"] is True
    assert sum(len(e["entities"]) for e in page.data["items"]) == 100
    assert page.hints == [
        {
            "text": "More changes available",
            "command": f"graph changes --graph company --since {page.data['next']}",
        }
    ]


def test_usage_errors(run_cli: CliRunner, empty: Path) -> None:
    create(run_cli, "org:Position", "p")
    for since, why in (
        ("garbage", "malformed"),
        ("tx:", "malformed"),
        ("tx:-1", "negative"),
        ("tx:1x", "not a number"),
        ("tx:0/0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", "no entity in tx 0"),
        ("tx:1/not-an-id", "not an id"),
        ("2030-01-01T00:00:00", "a time without an offset"),
        ("", "empty"),
    ):
        refused = changes(run_cli, "--since", since)
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), why
        assert refused.error["details"]["argument"] == "--since", why
        assert refused.error["message"].startswith("--since: "), why
    for limit in ("0", "501", "-3"):
        refused = changes(run_cli, "--since", "tx:0", "--limit", limit)
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), limit
        assert refused.error["details"]["argument"] == "--limit", limit
    missing = changes(run_cli)
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")
    assert "--since" in missing.error["message"]
    assert changes(run_cli, "--since", "tx:0", "--as-of", "tx:1").code == 2  # no such option


def enable_logs(env: dict[str, str]) -> Path:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    return Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"


def test_the_access_log_names_the_command_and_no_entity(
    run_cli: CliRunner, empty: Path, isolated_env: dict[str, str]
) -> None:
    log = enable_logs(isolated_env)
    person = create(run_cli, "ppl:Employee", "p")
    assert feed(run_cli)["items"]
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    mine = [line for line in lines if line.get("command") == "changes"]
    assert len(mine) == 1, lines
    text = json.dumps(mine[0])
    assert person not in text and "ppl:Employee" not in text
