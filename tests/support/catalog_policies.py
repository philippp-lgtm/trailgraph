"""Policies and a published catalog for the tests of what a caller may use (graph start plan).

``company_env`` is the company example on the memory backend with its example functions, actions
and views published; ``Policy`` builds a compiled policy from the few knobs the tests vary (deny
rules on ``read.function`` and ``read.find``, read-only and hidden types, a grant, obligations).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.ports.catalog import (
    FunctionsSlot,
    KineticSlot,
    functions_hash,
    sources_hash,
)
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.services.read import ReadService
from support import views as view_fixtures
from support.actions import publish_actions, valid_sources
from support.services import ServiceEnv

GRAPH = "example_graph"


class Fixed:
    """A policy source with one fixed policy set."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


@dataclass(frozen=True)
class Policy:
    """A caller's policy, as the matrix varies it."""

    grant: str = "level: write"
    deny: tuple[str, ...] = ()  # read.function, read.find
    types: tuple[tuple[str, str], ...] = ()  # graph layer: type -> read | none
    obligations: bool = False

    def compiled(self, env: ServiceEnv) -> PolicySet:
        graphs = {GRAPH: env.entry.id}
        deny = "".join(f"  - {{id: no_{a.replace('.', '_')}, action: [{a}]}}\n" for a in self.deny)
        installation = (
            "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
            f"  - {{entrypoint: app, graphs: [{GRAPH}], {self.grant}}}\n"
            'no_project: {"*": write}\n' + (f"deny:\n{deny}" if deny else "")
        )
        layers = [compile_policy(installation.encode(), file="i.yaml", graphs=graphs).document]
        if self.types or self.obligations:
            graph = "schema: graph_cli.policy/1\nid: graph\n"
            if self.types:
                graph += "types:\n" + "".join(f'  "{t}": {m}\n' for t, m in self.types)
            if self.obligations:
                graph += (
                    "obligations:\n  - {action: [write.*], require_reason: true, max_batch: 1}\n"
                )
            layers.append(
                compile_policy(
                    graph.encode(),
                    file="g.yaml",
                    graphs=graphs,
                    graph=env.entry.id,
                    roles=frozenset(),
                ).document
            )
        return policy_set(*layers)  # type: ignore[arg-type]


def publish_functions(env: ServiceEnv, sources: dict[str, str]) -> None:
    compiled, problems = compile_catalog(sources, env.model)
    assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    env.catalog.functions[env.entry.id] = FunctionsSlot(  # type: ignore[union-attr]
        functions_hash(files), hash_value(compiled), env.entry.pin, compiled, {}
    )


def publish_views(env: ServiceEnv) -> None:
    function_entries = view_fixtures.function_entries(env.model)
    sources = view_fixtures.valid_sources()
    compiled, problems = compile_views(sources, env.model, function_entries)
    assert [p.to_json() for p in problems] == []
    files = {name: text.encode() for name, text in sources.items()}
    env.catalog.views[env.entry.id] = KineticSlot(  # type: ignore[union-attr]
        sources_hash(files), hash_value(compiled), env.entry.pin, compiled, {}, kind="views"
    )


EMPLOYEE_LINKS = "level: read, writes: {ppl:Employee: {link: [leg:employed_by]}}"

LIMITS: tuple[tuple[tuple[str, str], ...], ...] = (
    (),
    (("ops:Task", "none"),),
    (("org:Position", "none"),),
    (("org:Department", "none"),),
    (("ppl:Employee", "none"), ("ppl:External", "none")),
    (("ops:Training", "none"), ("leg:LegalEntity", "none")),
)
RIGHTS: tuple[dict[str, Any], ...] = (
    {},
    {"deny": ("read.function",)},
    {"deny": ("read.find",)},
    {"deny": ("read.function", "read.find")},
    {"grant": "level: read"},
    {"grant": EMPLOYEE_LINKS},
    {"obligations": True},
    {"grant": "level: read", "deny": ("read.function", "read.find"), "obligations": True},
)


def company_env(root: Path) -> ServiceEnv:
    found = ServiceEnv("memory", "company", root, example_version="1.1.0")
    publish_functions(found, view_fixtures.function_sources())
    publish_actions(found, valid_sources())
    publish_views(found)
    return found


def reader(env: ServiceEnv, policy: Policy | None = None) -> ReadService:
    return env.make_reader(None if policy is None else Fixed(policy.compiled(env)))
