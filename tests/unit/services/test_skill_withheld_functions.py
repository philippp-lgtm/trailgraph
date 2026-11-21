"""What a generated skill shows of the functions to a reader without ``read.function`` (graph
start plan S4, review of 560bcdd): the read view decides first, the right second. A reader that
sees no function gets the same skill documents and the same digest with or without the right, so
the right cannot be used to find out that the graph has functions it does not see; a reader that
sees some gets a document marked ``withheld`` and no names.

Over the company example (the type limits and the grants of ``support.catalog_policies``) and
the ``staff`` toy (a reader that lacks a marking is limited by it).
"""

from dataclasses import replace
from pathlib import Path

import pytest
from support import markings as marked
from support.actions import publish_actions
from support.catalog_policies import GRAPH, Fixed, Policy, company_env, publish_functions, reader
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.canonical import JsonObject, hash_value
from graph_cli.kernel.records import RequestMeta
from graph_cli.kernel.skill_featured import kind_digest
from graph_cli.services.read import ReadService

REQUEST = RequestMeta("run", "sha256:" + "0" * 64)
NO_FUNCTIONS = ("read.function",)
# Every type the company functions name: a reader that loses all of them sees no function.
ALL_FUNCTION_TYPES = (
    "ppl:Employee",
    "ppl:External",
    "ppl:Person",
    "org:Position",
    "org:Department",
    "org:Role",
    "ops:Training",
    "ops:Policy",
    "leg:LegalEntity",
)
HIDE_ALL = tuple((t, "none") for t in ALL_FUNCTION_TYPES)


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    return company_env(tmp_path / "root")


def documents(env: ServiceEnv, read: ReadService) -> dict[str, object]:
    model, _ = read.skill_model(env.actor, env.catalog.resolve(env.name), REQUEST)
    return dict(model)


def functions_of(model: dict[str, object]) -> JsonObject | None:
    found = model.get("functions")
    assert found is None or isinstance(found, dict)
    return found


def test_a_reader_whose_view_hides_every_function_gets_the_same_skill_with_or_without_the_right(
    env: ServiceEnv,
) -> None:
    allowed = reader(env, Policy(types=HIDE_ALL))
    denied = reader(env, Policy(types=HIDE_ALL, deny=NO_FUNCTIONS))
    assert functions_of(documents(env, allowed)) is None
    assert documents(env, denied) == documents(env, allowed)
    entry = env.catalog.resolve(env.name)
    assert denied.skill_featured(env.actor, entry) == allowed.skill_featured(env.actor, entry)


def test_a_reader_that_sees_some_functions_without_the_right_is_told_they_are_withheld(
    env: ServiceEnv,
) -> None:
    some = (("org:Position", "none"),)
    allowed = functions_of(documents(env, reader(env, Policy(types=some))))
    assert allowed is not None and allowed["functions"] and "withheld" not in allowed
    denied = functions_of(documents(env, reader(env, Policy(types=some, deny=NO_FUNCTIONS))))
    assert denied is not None
    assert denied["functions"] == [] and denied["withheld"] is True
    # the right, not the view, is what changes the digest of a reader that sees some
    entry = env.catalog.resolve(env.name)
    one = reader(env, Policy(types=some)).skill_featured(env.actor, entry)
    two = reader(env, Policy(types=some, deny=NO_FUNCTIONS)).skill_featured(env.actor, entry)
    assert one != two


def test_a_reader_that_sees_only_deprecated_functions_gets_the_same_skill_with_or_without_the_right(
    env: ServiceEnv,
) -> None:
    """The render and the digest count active functions only, so a reader that sees no active
    function is not told "withheld": its documents and digest are those of the reader with the
    right (review of 56d20be)."""
    slot = env.catalog.functions[env.entry.id]  # type: ignore[union-attr]
    compiled = {
        **slot.compiled,
        "functions": [{**f, "status": "deprecated"} for f in slot.compiled["functions"]],  # type: ignore[union-attr]
    }
    env.catalog.functions[env.entry.id] = replace(  # type: ignore[union-attr]
        slot, compiled=compiled, compiled_hash=hash_value(compiled)
    )
    for types in ((), (("org:Position", "none"),)):
        allowed = reader(env, Policy(types=types))
        denied = reader(env, Policy(types=types, deny=NO_FUNCTIONS))
        shown = functions_of(documents(env, denied))
        assert "withheld" not in (shown or {})
        assert shown == functions_of(documents(env, allowed))  # views differ: they run functions
        assert kind_digest("functions", shown) == kind_digest(
            "functions", functions_of(documents(env, allowed))
        )


def test_a_reader_with_no_limit_and_no_right_is_withheld_too(env: ServiceEnv) -> None:
    denied = functions_of(documents(env, reader(env, Policy(deny=NO_FUNCTIONS))))
    assert denied is not None and denied["functions"] == [] and denied["withheld"] is True


def test_the_digest_of_a_limited_reader_moves_with_the_actions_its_write_rights_list(
    env: ServiceEnv,
) -> None:
    entry = env.catalog.resolve(env.name)
    limit = (("ops:Task", "none"),)
    writer = reader(env, Policy(types=limit)).skill_featured(env.actor, entry)
    reading = reader(env, Policy(grant="level: read", types=limit)).skill_featured(env.actor, entry)
    assert writer != reading


# ---- a reader limited only by a marking ---------------------------------------------------


@pytest.fixture
def staff(tmp_path: Path) -> ServiceEnv:
    found = ServiceEnv("memory", "staff", tmp_path / "root")
    publish_functions(found, marked.FUNCTION_SOURCES)
    publish_actions(found, marked.ACTION_SOURCES)
    return found


def staff_reader(env: ServiceEnv, grant: str, deny: str = "") -> ReadService:
    installation = (
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        f"  - {{entrypoint: app, graphs: [{GRAPH}], {grant}}}\n"
        'no_project: {"*": write}\n' + deny
    )
    document = compile_policy(
        installation.encode(), file="i.yaml", graphs={GRAPH: env.entry.id}
    ).document
    return env.make_reader(Fixed(policy_set(document)))  # type: ignore[arg-type]


def test_a_reader_limited_only_by_a_marking_gets_the_visible_functions_or_the_withheld_mark(
    staff: ServiceEnv,
) -> None:
    deny = "deny:\n  - {id: no_read_function, action: [read.function]}\n"
    names = lambda doc: {f["name"] for f in doc["functions"]}  # noqa: E731
    plain = functions_of(documents(staff, staff_reader(staff, "level: write")))
    assert plain is not None and "employee_mail" not in names(plain) and names(plain)
    full = functions_of(
        documents(staff, staff_reader(staff, "level: write, markings: [stf:personal, stf:pay]"))
    )
    assert full is not None and "employee_mail" in names(full)
    withheld = functions_of(documents(staff, staff_reader(staff, "level: write", deny)))
    assert withheld is not None and withheld["functions"] == [] and withheld["withheld"] is True
