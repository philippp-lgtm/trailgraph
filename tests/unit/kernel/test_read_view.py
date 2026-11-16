"""``ReadView``: the read view key of one caller on one graph (field visibility plan F4, R3)."""

import pytest

from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.read_view import UNLIMITED, ReadView

TYPES = frozenset({"ex:Person", "ex:Team"})
ATTRIBUTES = (("ex:Person", frozenset({"name", "team"})), ("ex:Team", frozenset({"name"})))


def test_without_any_limit_nothing_is_limited_and_there_is_no_digest() -> None:
    assert ReadView(None, None) == UNLIMITED
    assert not UNLIMITED.limited and not UNLIMITED.types_limited
    assert not UNLIMITED.attributes_limited
    assert UNLIMITED.digest is None
    assert UNLIMITED.keeps("ex:Person") is None and UNLIMITED.shows_type("ex:Anything")


def test_the_digest_of_a_type_limited_view_is_the_stage_3_digest() -> None:
    view = ReadView(TYPES)
    assert view.digest == hash_value(sorted(TYPES))  # installed claims stay fresh
    assert ReadView(frozenset()).digest == hash_value([])
    assert view.limited and view.types_limited and not view.attributes_limited


def test_the_attribute_half_changes_the_digest_and_both_halves_count() -> None:
    attributes_only = ReadView(None, ATTRIBUTES)
    both = ReadView(TYPES, ATTRIBUTES)
    digests = {ReadView(TYPES).digest, attributes_only.digest, both.digest}
    assert None not in digests and len(digests) == 3
    assert attributes_only.limited and not attributes_only.types_limited
    assert attributes_only.attributes_limited
    narrower = ReadView(TYPES, (("ex:Person", frozenset({"name"})), ATTRIBUTES[1]))
    assert narrower.digest != both.digest


def test_the_digest_counts_the_attributes_of_the_visible_types_only() -> None:
    seen, other = frozenset({"ex:Person"}), "ex:Team"

    def view(person: set[str], team: set[str]) -> ReadView:
        return ReadView.of(seen, {"ex:Person": person, other: team})

    base = view({"name"}, {"name"})
    assert view({"name"}, {"name", "extra"}).digest == base.digest  # a type it never sees
    assert view({"name"}, set()).digest == base.digest
    assert view({"name", "extra"}, {"name"}).digest != base.digest
    assert base.digest != ReadView.of(seen).digest  # the attribute half still counts
    assert base.digest != ReadView.of(None, {"ex:Person": {"name"}, other: {"name"}}).digest


def test_the_view_is_hashable_and_ignores_the_order_of_its_attribute_half() -> None:
    reordered = ReadView(TYPES, tuple(reversed(ATTRIBUTES)))
    same = ReadView(TYPES, ATTRIBUTES)
    assert reordered == same and hash(reordered) == hash(same)
    assert reordered.attributes == ATTRIBUTES  # sorted by type
    assert reordered.digest == same.digest
    assert {reordered: 1}[same] == 1
    assert ReadView.of(TYPES, {"ex:Team": ["name"], "ex:Person": ["team", "name"]}) == same
    assert ReadView.of() == UNLIMITED


def test_keeps_is_an_allow_list_and_a_type_without_an_entry_keeps_nothing() -> None:
    view = ReadView(None, ATTRIBUTES)
    assert view.keeps("ex:Person") == {"name", "team"}
    assert view.keeps("ex:Other") == frozenset()  # fail closed
    assert view.shows_type("ex:Other")  # the type half does not hide it
    assert ReadView(TYPES).shows_type("ex:Team") and not ReadView(TYPES).shows_type("ex:X")


def test_types_only_drops_the_attribute_half() -> None:
    assert ReadView(TYPES, ATTRIBUTES).types_only() == ReadView(TYPES)
    assert ReadView(None, ATTRIBUTES).types_only() == UNLIMITED
    plain = ReadView(TYPES)
    assert plain.types_only() is plain


def test_a_type_named_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        ReadView(None, (("ex:Person", frozenset()), ("ex:Person", frozenset({"name"}))))
