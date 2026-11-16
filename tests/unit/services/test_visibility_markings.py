"""``Visibility`` computes the attribute half of the read view (field visibility plan F4, step 3).

Markings and types are those of the ``personnel`` series; the stage 3 behaviour of the type half
is covered by ``test_visibility_cache.py``.
"""

import dataclasses
from collections.abc import Callable

import pytest
from support.fixtures import compiled_model
from support.txbuilder import actor as plain_actor

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.read_view import UNLIMITED
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services import visibility as visibility_module
from graph_cli.services.visibility import Visibility, attribute_mask

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
MARKED = compiled_model("personnel", "1.0.0")
PLAIN = compiled_model("notes_and_sources", "1.1.0")
SOURCE = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of people"}
  reporting: {description: "Reporting: people without personal data"}
  people: {description: "People only, without personal data"}
grants:
  - {entrypoint: server, principal: {role: hr}, graphs: [example_graph], level: write,
     markings: [ex:personal]}
  - {entrypoint: server, principal: {role: reporting}, graphs: [example_graph], level: read}
  - {entrypoint: server, principal: {role: people}, graphs: [example_graph], level: read,
     reads: [ex:Person]}
no_project: {"*": read}
"""
POLICY = policy_set(
    compile_policy(SOURCE, file="installation.yaml", graphs={"example_graph": EXAMPLE}).document
)
# The same without the ``reads`` grant: the graph is not read limited.
UNLIMITED_TYPES = policy_set(
    compile_policy(
        SOURCE.split(b"  - {entrypoint: server, principal: {role: people}")[0]
        + b'no_project: {"*": read}\n',
        file="installation.yaml",
        graphs={"example_graph": EXAMPLE},
    ).document
)


def entry(artifact: str = "sha256:a", bundle: str = "personnel") -> GraphEntry:
    return GraphEntry(
        EXAMPLE, "example_graph", "active", OntologyPin(bundle, "1.0.0", artifact), {}, ()
    )


def token(*roles: str) -> ActorContext:
    return dataclasses.replace(
        plain_actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


def counting(model: CompiledModel) -> tuple[Callable[[], CompiledModel], list[int]]:
    calls: list[int] = []

    def load() -> CompiledModel:
        calls.append(1)
        return model

    return load, calls


def test_a_caller_without_the_marking_gets_an_allow_list_per_concrete_type() -> None:
    view = Visibility().of(token("reporting"), entry(), POLICY, MARKED)
    assert view.types is None and view.attributes is not None
    assert view.keeps("ex:Person") == {"name", "team"}
    assert view.keeps("ex:Team") == set(MARKED.classes["ex:Team"].attributes)
    concrete = sorted(n for n, c in MARKED.classes.items() if not c.abstract)
    assert [t for t, _ in view.attributes] == concrete  # every concrete type, sorted
    assert view.limited and not view.types_limited


def test_a_caller_holding_every_used_marking_has_no_attribute_half() -> None:
    assert Visibility().of(token("hr"), entry(), POLICY, MARKED) == UNLIMITED
    assert Visibility().of(token("hr", "reporting"), entry(), POLICY, MARKED) == UNLIMITED


def test_both_halves_together() -> None:
    view = Visibility().of(token("people"), entry(), POLICY, MARKED)
    assert view.types == {"ex:Person"}
    assert view.keeps("ex:Person") == {"name", "team"}


def test_a_pin_without_markings_computes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_probe(*args: object) -> frozenset[str]:
        raise AssertionError("no probe for a pin without markings")

    monkeypatch.setattr(visibility_module, "held_markings", no_probe)
    monkeypatch.setattr(visibility_module, "visibility", no_probe)
    cache = Visibility()
    load, calls = counting(PLAIN)
    for roles in (("reporting",), ("hr",), ()):
        plain = entry("sha256:p", "notes_and_sources")
        assert cache.lazy(token(*roles), plain, UNLIMITED_TYPES, load, frozenset) == UNLIMITED
        assert cache.lazy(token(*roles), plain, BUILTIN_POLICY_SET, load, frozenset) == UNLIMITED
    assert calls == []  # the markings come from the raw bytes: the model is never needed
    assert cache._visible == {}  # nothing cached per actor either


def test_the_attribute_half_is_cached_per_policy_pin_and_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    real = visibility_module.held_markings

    def held(*args: object) -> frozenset[str]:
        probes.append("probe")
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(visibility_module, "held_markings", held)
    cache = Visibility()
    load, calls = counting(MARKED)
    used = MARKED.used_markings
    first = cache.lazy(token("reporting"), entry(), POLICY, load, used)
    assert cache.lazy(token("reporting"), entry(), POLICY, load, used) is first
    assert len(probes) == 1
    cache.lazy(token("hr"), entry(), POLICY, load, used)  # another actor
    cache.lazy(token("reporting"), entry("sha256:b"), POLICY, load, used)  # another pin
    cache.lazy(token("reporting"), entry(), BUILTIN_POLICY_SET, load, used)  # another policy
    assert len(probes) == 4 and len(cache._visible) == 4
    assert len(calls) == 4  # once per computed view


def test_the_markings_are_asked_on_every_call_and_key_the_cached_view() -> None:
    """An answer never outlives the artifact it came from: the bytes of a pin that showed no
    marking (an artifact that did not match its pin, say) and show some now give the
    attribute half at once."""
    cache = Visibility()
    asked: list[frozenset[str]] = []
    answers = iter([frozenset(), MARKED.used_markings(), MARKED.used_markings()])

    def markings() -> frozenset[str]:
        asked.append(found := next(answers))
        return found

    actor = token("people")  # read limited, so a view is cached for the empty markings too
    before = cache.lazy(actor, entry(), POLICY, lambda: MARKED, markings)
    assert before.types == {"ex:Person"} and before.attributes is None
    after = cache.lazy(actor, entry(), POLICY, lambda: MARKED, markings)
    assert after.attributes is not None and after.keeps("ex:Person") == {"name", "team"}
    assert cache.lazy(actor, entry(), POLICY, lambda: MARKED, markings) is after
    assert len(asked) == 3


def test_the_builtin_policy_holds_no_marking() -> None:
    view = Visibility().of(token(), entry(), BUILTIN_POLICY_SET, MARKED)
    assert view.types is None and view.keeps("ex:Person") == {"name", "team"}


def test_attribute_mask_is_none_when_every_used_marking_is_held() -> None:
    assert attribute_mask(MARKED, {"ex:personal"}) is None
    assert attribute_mask(PLAIN, set()) is None  # nothing is marked
    mask = attribute_mask(MARKED, set())
    assert mask is not None and dict(mask)["ex:Person"] == {"name", "team"}


def test_conjunctive_markings_need_every_one() -> None:
    model = compiled_model("personnel", "1.1.0")
    person = model.classes["ex:Person"]
    both = ("ex:personal", "ex:team_internal")
    email = dataclasses.replace(person.attributes["email"], markings=both)
    attributes = {**person.attributes, "email": email}
    classes = {**model.classes, "ex:Person": dataclasses.replace(person, attributes=attributes)}
    model = dataclasses.replace(model, classes=classes)
    assert model.used_markings() == set(both)
    for held in ((), ("ex:personal",), ("ex:team_internal",)):
        mask = attribute_mask(model, set(held))
        assert mask is not None and "email" not in dict(mask)["ex:Person"], held
    assert attribute_mask(model, set(both)) is None
