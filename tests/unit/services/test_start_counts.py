"""What ``graph start`` counts of a graph (graph start plan S2): the numbers of ``describe`` for the
same caller, nothing of what its read view hides, and the same totals whatever rights it holds
(the rights only move entries between ``may use`` and ``may not use``)."""

from pathlib import Path

import pytest
from support.catalog_leaks import assert_hidden_absent
from support.catalog_policies import LIMITS, RIGHTS, Policy, company_env, reader
from support.services import ServiceEnv

KINDS = ("functions", "actions", "views")


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    return company_env(tmp_path / "root")


def item(env: ServiceEnv, policy: Policy) -> dict[str, object]:
    return reader(env, policy).start_graphs(env.actor, [env.entry])[0]


def totals(found: dict[str, object]) -> dict[str, int]:
    counts = found["counts"]
    refused = found.get("not_allowed", {})
    assert isinstance(counts, dict) and isinstance(refused, dict)
    return {kind: counts[kind] + refused.get(kind, 0) for kind in KINDS}


def test_a_graph_with_everything_allowed_has_no_not_allowed_count(env: ServiceEnv) -> None:
    found = item(env, Policy())
    assert found["name"] == "example_graph" and found["level"] == "write"
    assert found["counts"] == {
        "modules": found["counts"]["modules"],  # type: ignore[index]
        "types": found["counts"]["types"],  # type: ignore[index]
        "functions": 9,
        "actions": 4,
        "views": 5,  # the deprecated legacy view is not counted, as in describe views
    }
    assert "not_allowed" not in found


def test_the_totals_depend_on_the_read_view_alone_and_hidden_names_are_absent(
    env: ServiceEnv,
) -> None:
    every = {
        name
        for names in (
            {f.name for f in reader(env).functions(env.actor, env.entry) or ()},
            {a.name for a in reader(env).actions(env.actor, env.entry) or ()},
            {v.name for v in reader(env).views(env.actor, env.entry) or ()},
        )
        for name in names
    }
    hidden_somewhere = 0
    for limit in LIMITS:
        oracle = totals(item(env, Policy(types=limit)))
        seen = {
            *(
                f.name
                for f in reader(env, Policy(types=limit)).functions(env.actor, env.entry) or ()
            ),
            *(a.name for a in reader(env, Policy(types=limit)).actions(env.actor, env.entry) or ()),
            *(v.name for v in reader(env, Policy(types=limit)).views(env.actor, env.entry) or ()),
        }
        hidden = every - seen
        hidden_somewhere += len(hidden)
        for rights in RIGHTS:
            policy = Policy(types=limit, **rights)
            found = item(env, policy)
            assert totals(found) == oracle, (limit, rights)
            modules = reader(env, policy).start_modules(env.actor, env.entry)
            assert_hidden_absent([found, modules], hidden - {found["name"]})
    assert hidden_somewhere > 0


def test_a_caller_without_read_describe_gets_no_counts(env: ServiceEnv) -> None:
    from support.catalog_policies import Fixed

    from graph_cli.access_control.compiler import compile_policy
    from graph_cli.access_control.policy import policy_set

    text = (
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        "  - {entrypoint: app, graphs: [example_graph], level: write}\n"
        'no_project: {"*": write}\n'
        "deny:\n  - {id: no_describe, action: [read.describe]}\n"
    )
    document = compile_policy(
        text.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    read = env.make_reader(Fixed(policy_set(document)))  # type: ignore[arg-type]
    found = read.start_graphs(env.actor, [env.entry])[0]
    assert found["counts"] is None and found["skill"] is None
    assert read.start_modules(env.actor, env.entry) == []


def test_the_modules_of_one_graph_carry_the_counts_of_describe_modules(env: ServiceEnv) -> None:
    modules = {m["id"]: m for m in reader(env).start_modules(env.actor, env.entry)}  # type: ignore[index]
    assert modules["organization"]["functions"] == 3
    assert "not_allowed" not in modules["organization"]
    refused = {
        m["id"]: m  # type: ignore[index]
        for m in reader(env, Policy(deny=("read.function",))).start_modules(env.actor, env.entry)
    }
    assert refused["organization"]["functions"] == 0
    assert refused["organization"]["not_allowed"]["functions"] == 3  # type: ignore[index]
