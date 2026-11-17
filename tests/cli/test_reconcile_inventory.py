"""S: the acceptance patterns of reconcile by key (plan section 11), replayed through ``graph
apply`` on a throwaway data root with the toy ontology of ``support.reconcile_toy``.

A pipeline scans hosts: packages (shared vocabulary, partial scope), installations per
collector (a complete scope each, edges to the package by value and to the host), and folders (a
complete scope, expected folders that are not present stay as items). Each case is a run of the
pipeline's document; every run is one tx, or none when nothing changed.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app
from support.reconcile_toy import _MODULE, doc

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
HOST = {"type": "ex:Host", "attr": "hostname", "value": "host-a"}
INSTALLATION = "ex:Installation"
FOLDER = "ex:Folder"


# ---- the pipeline: what it hands over ----------------------------------------------------------


def packages(*names: str) -> dict[str, Any]:
    """The shared vocabulary: a partial scope, so a package no run lists stays."""
    return {
        "type": "ex:Package",
        "key": "source_key",
        "under": "pkg/",
        "fields": ["name"],
        "items": [{"key": f"pkg/{n}", "attrs": {"name": n}} for n in names],
    }


def installs(manager: str, versions: dict[str, str], **extra: Any) -> dict[str, Any]:
    """The installations one collector saw on the host: a complete scope of its own."""
    under = f"host_a/{manager}/"
    items = [
        {
            "key": f"{under}{name}",
            "attrs": {"package": name, "version": version},
            "edges": [
                {
                    "rel": "ex:installation_of",
                    "target": {
                        "type": "ex:Package",
                        "attr": "source_key",
                        "value": f"inventory/pkg/{name}",
                    },
                },
                {"rel": "ex:installed_on", "target": HOST},
            ],
        }
        for name, version in versions.items()
    ]
    return {
        "type": INSTALLATION,
        "key": "source_key",
        "under": under,
        "complete": True,
        "fields": ["package", "version"],
        "edges": ["ex:installation_of", "ex:installed_on"],
        "items": items,
        **extra,
    }


def folders(seen: list[str], expected: list[str], **extra: Any) -> dict[str, Any]:
    """Folders of a scan root: observed ones are present, expected ones that are missing stay
    as items with ``present: false`` instead of being retired."""
    items = [{"key": f"host_a/{p}", "attrs": {"path": p, "present": True}} for p in seen]
    items += [
        {"key": f"host_a/{p}", "attrs": {"path": p, "present": False}}
        for p in expected
        if p not in seen
    ]
    return {
        "type": FOLDER,
        "key": "source_key",
        "under": "host_a/",
        "complete": True,
        "fields": ["path", "present"],
        "items": items,
        **extra,
    }


def versions(manager: str, count: int, version: str = "1") -> dict[str, str]:
    return {f"{manager}{n:02d}": version for n in range(count)}


def run(document: dict[str, Any]) -> str:
    return json.dumps(document)


# ---- the toy graph -----------------------------------------------------------------------------


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    ontology = tmp_path / "ontology"
    ontology.mkdir()
    (ontology / "example_module.yaml").write_text(_MODULE)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(ontology), "--language", "en"
    )
    assert created.code == 0, created.envelope
    host = app(run_cli, "create", "ex:Host", "--set-json", 'hostname="host-a"', env=ENV)
    assert host.code == 0, host.envelope  # hosts are curated by people, not by the pipeline
    return root


def apply(run_cli: CliRunner, document: dict[str, Any], *flags: str) -> Outcome:
    return app(run_cli, "apply", "-", *flags, env=ENV, input=run(document))


HOST_TX = 1  # the host is created by hand before the first run: the runs count from 1 after it


def written(outcome: Outcome) -> int | None:
    """The number of the tx the run wrote, counted from the host's own (tx 1); ``None`` when it
    wrote nothing."""
    assert outcome.code == 0, outcome.envelope
    seq: int | None = outcome.data["results"][0]["result"]["seq"]
    return None if seq is None else seq - HOST_TX


def counts(outcome: Outcome, index: int = 0) -> dict[str, int]:
    block = outcome.data["results"][0]["result"]["reconcile"]["scopes"][index]
    return {k: v for k, v in block.items() if isinstance(v, int) and not isinstance(v, bool)}


def entity(run_cli: CliRunner, type_curie: str, key: str) -> dict[str, Any] | None:
    found = app(
        run_cli, "find", "--type", type_curie, "--where", f"source_key=inventory/{key}", env=ENV
    )
    assert found.code == 0, found.envelope
    items: list[dict[str, Any]] = found.data["items"]
    return items[0] if items else None


def keys(run_cli: CliRunner, type_curie: str) -> list[str]:
    found = app(run_cli, "find", "--type", type_curie, "--limit", "500", env=ENV)
    assert found.code == 0, found.envelope
    return sorted(
        str(i["attrs"]["source_key"]).removeprefix("inventory/") for i in found.data["items"]
    )


def scan(**scopes: Any) -> dict[str, Any]:
    """The whole document of a scan: the vocabulary, two collectors and the folders."""
    return doc(
        packages("curl", "jq", "zlib", "bash", "git", "tmux"),
        installs("apt", scopes.get("apt", {"curl": "8", "jq": "1", "zlib": "1"})),
        installs("pip", scopes.get("pip", {"bash": "5", "git": "2"})),
        folders(scopes.get("seen", ["etc", "var"]), ["etc", "var", "srv"]),
        reason="scan of host-a",
    )


# ---- the patterns of the estate sync, as runs --------------------------------------------------


def test_a_scan_is_one_tx_and_the_same_scan_again_writes_none(
    run_cli: CliRunner, root: Path
) -> None:
    first = apply(run_cli, scan())
    assert written(first) == 1  # packages, installations and folders in one tx
    assert [counts(first, i)["created"] for i in range(4)] == [6, 3, 2, 3]
    assert len(keys(run_cli, INSTALLATION)) == 5 and len(keys(run_cli, FOLDER)) == 3
    again = apply(run_cli, scan())
    assert written(again) is None and again.envelope["graph"].get("seq") is None
    assert [counts(again, i)["unchanged"] for i in range(4)] == [6, 3, 2, 3]
    assert all(counts(again, i)["created"] == 0 for i in range(4))
    # the edges were written from the values: an installation is linked to its package and host
    curl = entity(run_cli, INSTALLATION, "host_a/apt/curl")
    assert curl is not None
    neighbors = app(run_cli, "neighbors", curl["id"], env=ENV)
    assert neighbors.code == 0, neighbors.envelope
    rels = {i["edge"]["rel"] for i in neighbors.data["items"]}
    assert rels == {"ex:installation_of", "ex:installed_on"}


def test_a_change_a_drop_and_an_addition_are_one_tx_each_run(
    run_cli: CliRunner, root: Path
) -> None:
    assert written(apply(run_cli, scan())) == 1
    jq = entity(run_cli, INSTALLATION, "host_a/apt/jq")
    assert jq is not None
    changed = apply(run_cli, scan(apt={"curl": "9", "jq": "1", "tmux": "3"}))  # zlib dropped
    assert written(changed) == 2
    assert counts(changed, 1) == {
        "created": 1,
        "updated": 1,
        "unchanged": 1,
        "moved": 0,
        "adopted": 0,
        "kept": 0,
        "retired": 1,
        "revived": 0,
    }
    assert entity(run_cli, INSTALLATION, "host_a/apt/zlib") is None
    assert entity(run_cli, INSTALLATION, "host_a/apt/jq")["id"] == jq["id"]  # type: ignore[index]
    curl = entity(run_cli, INSTALLATION, "host_a/apt/curl")
    assert curl is not None and curl["attrs"]["version"] == "9"


def test_a_collector_that_failed_sends_no_scope_and_its_entities_stay(
    run_cli: CliRunner, root: Path
) -> None:
    assert written(apply(run_cli, scan())) == 1
    before = keys(run_cli, INSTALLATION)
    partial = doc(
        packages("curl", "jq", "zlib", "bash", "git", "tmux"),
        installs("apt", {"curl": "9", "jq": "1", "zlib": "1"}),  # pip failed: no entry
        reason="scan of host-a, pip failed",
    )
    outcome = apply(run_cli, partial)
    assert (
        written(outcome) == 2
        and len(outcome.data["results"][0]["result"]["reconcile"]["scopes"]) == 2
    )
    assert keys(run_cli, INSTALLATION) == before  # the pip installations were not touched
    # an empty answer of a collector is not the same as no answer: it is refused
    emptied = apply(run_cli, doc(installs("pip", {})))
    assert emptied.code == 1 and emptied.error["code"] == "reconcile.source_empty"
    assert keys(run_cli, INSTALLATION) == before
    assert apply(run_cli, doc(installs("pip", {}, allow_empty=True))).code == 0
    assert len(keys(run_cli, INSTALLATION)) == 3  # only a deliberate wipe retires them


def test_a_rename_by_moves_keeps_the_entity_and_its_edges(run_cli: CliRunner, root: Path) -> None:
    assert written(apply(run_cli, scan())) == 1
    old = entity(run_cli, INSTALLATION, "host_a/apt/jq")
    assert old is not None
    renamed_scope = installs("apt", {"curl": "8", "jq2": "1", "zlib": "1"})
    renamed_scope["moves"] = [{"from": "host_a/apt/jq", "to": "host_a/apt/jq2"}]
    document = doc(packages("curl", "jq", "jq2", "zlib", "bash", "git"), renamed_scope)
    moved = apply(run_cli, document)
    assert written(moved) == 2 and counts(moved, 1)["moved"] == 1
    assert counts(moved, 1)["retired"] == 0 and counts(moved, 1)["created"] == 0
    new = entity(run_cli, INSTALLATION, "host_a/apt/jq2")
    assert new is not None and new["id"] == old["id"]  # the same entity under the new key
    assert entity(run_cli, INSTALLATION, "host_a/apt/jq") is None
    # the run is repeatable: the old key is gone and the new one held, a no-op
    assert written(apply(run_cli, document)) is None


def test_an_adoption_run_keys_the_entities_written_before_there_were_keys(
    run_cli: CliRunner, root: Path
) -> None:
    ids = {}
    for name in ("curl", "jq"):
        made = app(
            run_cli,
            "create",
            INSTALLATION,
            "--set-json",
            f'package="{name}"',
            "--set-json",
            'version="1"',
            env=ENV,
        )
        assert made.code == 0, made.envelope
        ids[name] = made.data["id"]
    under = "host_a/apt/"
    scope = installs("apt", {"curl": "1", "jq": "1"})
    scope["moves"] = [{"id": i, "to": f"{under}{n}"} for n, i in ids.items()]
    adopt = doc(packages("curl", "jq"), scope, reason="adopt the entities of the first scan")
    done = apply(run_cli, adopt)
    assert written(done) == 3  # two creates before it, this tx after
    assert counts(done, 1)["adopted"] == 2 and counts(done, 1)["created"] == 0
    for name, entity_id in ids.items():
        found = entity(run_cli, INSTALLATION, f"{under}{name}")
        assert found is not None and found["id"] == entity_id  # no duplicate, same identity
    assert len(keys(run_cli, INSTALLATION)) == 2
    assert written(apply(run_cli, adopt)) is None  # the second run has nothing to adopt


def test_the_mass_retire_refusal_and_its_override(run_cli: CliRunner, root: Path) -> None:
    names = versions("p", 20)
    assert written(apply(run_cli, doc(packages(*names), installs("apt", names)))) == 1
    shrunk = versions("p", 12)  # 8 of 20 would retire
    refused = apply(run_cli, doc(packages(*names), installs("apt", shrunk)))
    assert refused.code == 1 and refused.error["code"] == "reconcile.retire_limit"
    assert (refused.error["details"]["count"], refused.error["details"]["limit"]) == (8, 5)
    assert len(keys(run_cli, INSTALLATION)) == 20  # nothing was written
    smaller = apply(run_cli, doc(installs("apt", shrunk, allow_retire=7)))
    assert smaller.code == 1 and smaller.error["code"] == "reconcile.retire_limit"
    allowed = apply(run_cli, doc(packages(*names), installs("apt", shrunk, allow_retire=8)))
    assert written(allowed) == 2 and counts(allowed, 1)["retired"] == 8
    assert len(keys(run_cli, INSTALLATION)) == 12
    # the mistake a renamed key scheme makes is the same refusal, not a silent loss
    scheme = {f"new_{n}": v for n, v in shrunk.items()}
    lost = apply(run_cli, doc(packages(*names, *scheme), installs("apt", scheme)))
    assert lost.code == 1 and lost.error["code"] == "reconcile.retire_limit"


def test_keep_leaves_what_the_source_did_not_observe(run_cli: CliRunner, root: Path) -> None:
    assert written(apply(run_cli, scan())) == 1
    hidden = "host_a/apt/zlib"  # a sub-tree the collector could not read this time
    outcome = apply(
        run_cli,
        doc(installs("apt", {"curl": "9"}, keep=[hidden], allow_retire=5)),
    )
    assert written(outcome) == 2
    assert counts(outcome) == {
        "created": 0,
        "updated": 1,
        "unchanged": 0,
        "moved": 0,
        "adopted": 0,
        "kept": 1,
        "retired": 1,
        "revived": 0,
    }
    kept = entity(run_cli, INSTALLATION, hidden)
    assert kept is not None and kept["attrs"]["version"] == "1"  # not updated, not retired
    assert entity(run_cli, INSTALLATION, "host_a/apt/jq") is None  # the one nobody kept


def test_a_lifecycle_retire_keeps_the_entity_and_a_return_revives_it(
    run_cli: CliRunner, root: Path
) -> None:
    retire = {"transition": "removed", "revive": "present"}

    def scan_apt(names: dict[str, str]) -> dict[str, Any]:
        return doc(packages("curl", "jq", "zlib"), installs("apt", names, retire=retire))

    assert written(apply(run_cli, scan_apt({"curl": "8", "jq": "1", "zlib": "1"}))) == 1
    jq = entity(run_cli, INSTALLATION, "host_a/apt/jq")
    assert jq is not None and jq["attrs"]["state"] == "present"
    gone = apply(run_cli, scan_apt({"curl": "8", "zlib": "1"}))
    assert written(gone) == 2 and counts(gone, 1)["retired"] == 1
    held = entity(run_cli, INSTALLATION, "host_a/apt/jq")
    assert held is not None and held["id"] == jq["id"] and held["attrs"]["state"] == "removed"
    assert (
        written(apply(run_cli, scan_apt({"curl": "8", "zlib": "1"}))) is None
    )  # not retired twice
    back = apply(run_cli, scan_apt({"curl": "8", "jq": "1", "zlib": "1"}))
    assert written(back) == 3 and counts(back, 1)["revived"] == 1
    revived = entity(run_cli, INSTALLATION, "host_a/apt/jq")
    assert revived is not None and revived["id"] == jq["id"]  # the same entity came back
    assert revived["attrs"]["state"] == "present"
    history = app(run_cli, "history", jq["id"], env=ENV)
    assert [i["source"]["name"] for i in history.data["items"]] == ["inventory"] * 3


def test_expected_folders_that_are_missing_stay_and_deep_ones_are_kept(
    run_cli: CliRunner, root: Path
) -> None:
    first = apply(run_cli, doc(folders(["etc", "var", "srv"], ["etc", "var", "srv"])))
    assert written(first) == 1 and counts(first)["created"] == 3
    srv = entity(run_cli, FOLDER, "host_a/srv")
    assert srv is not None and srv["attrs"]["present"] is True
    # srv is gone from disk but expected: it stays, marked not present, and tmp was never seen
    second = apply(run_cli, doc(folders(["etc", "var"], ["etc", "var", "srv"])))
    assert (
        written(second) == 2 and counts(second)["updated"] == 1 and counts(second)["retired"] == 0
    )
    still = entity(run_cli, FOLDER, "host_a/srv")
    assert still is not None and still["id"] == srv["id"] and still["attrs"]["present"] is False
    # a folder below the scan depth is not observed at all: it is kept, and nothing changes
    deep = apply(run_cli, doc(folders(["etc"], ["etc"], keep=["host_a/var", "host_a/srv"])))
    assert written(deep) is None and counts(deep)["kept"] == 2 and counts(deep)["retired"] == 0
    assert keys(run_cli, FOLDER) == ["host_a/etc", "host_a/srv", "host_a/var"]
    # a folder nobody expects or keeps retires, as a complete scope says
    last = apply(run_cli, doc(folders(["etc"], ["etc"])))
    assert counts(last)["retired"] == 2 and written(last) == 3


def test_every_run_is_one_transaction_with_its_source_in_the_audit(
    run_cli: CliRunner, root: Path
) -> None:
    runs = [scan(), scan(apt={"curl": "9", "jq": "1", "zlib": "1"}), scan(seen=["etc"])]
    seqs = [written(apply(run_cli, document)) for document in runs]
    assert seqs == [1, 2, 3]
    changes = app(run_cli, "changes", "--since", "tx:2", env=ENV)
    assert [i["tx_seq"] for i in changes.data["items"]] == [3, 4]
    assert all(i["source"]["name"] == "inventory" for i in changes.data["items"])
    assert admin(run_cli, "audit", "verify").code == 0
