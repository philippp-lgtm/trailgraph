"""The duplicate-name warning through the write service (F47): it never refuses a
write, and a dry run names new entities by ``local``, never by a placeholder id."""

from pathlib import Path
from typing import Any

from support.services import ServiceEnv

from graph_cli.kernel.records import RequestMeta
from graph_cli.services.write import WriteRequest, WriteResult

REQUEST = RequestMeta.of("apply", {})


def topic(local: str, label: str) -> dict[str, Any]:
    return {"op": "create", "type": "nas:Topic", "local": local, "attrs": {"label": label}}


def apply(env: ServiceEnv, *ops: dict[str, Any], dry_run: bool) -> WriteResult:
    changeset = env.service.parse({"ops": list(ops)})
    return env.service.apply(WriteRequest(env.actor, env.name, changeset, dry_run, REQUEST))


def test_dry_run_names_new_candidates_by_local(tmp_path: Path) -> None:
    env = ServiceEnv("memory", "notes_and_sources", tmp_path)
    result = apply(env, topic("a", "Optics"), topic("b", "optics"), dry_run=True)
    found = [f.to_json() for f in result.findings]
    assert [(f["code"], f["target"]["local"]) for f in found] == [
        ("data.semantic.possible_duplicate", "a"),
        ("data.semantic.possible_duplicate", "b"),
    ]
    assert [f["actual"][0]["id"] for f in found] == ["@b", "@a"]
    assert all("@" in f["message"] for f in found)


def test_a_duplicate_name_commits_with_the_warning(tmp_path: Path) -> None:
    env = ServiceEnv("memory", "notes_and_sources", tmp_path)
    first = apply(env, topic("a", "Optics"), dry_run=False)
    second = apply(env, topic("b", "OPTICS"), dry_run=False)
    assert second.seq == (first.seq or 0) + 1
    (finding,) = second.findings
    assert finding.code == "data.semantic.possible_duplicate"
    assert finding.actual[0]["id"] == str(first.entities[0].id)  # type: ignore[index]
