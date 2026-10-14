"""Semantic versions and import ranges."""

import pytest

from graph_cli.ontology_layer.semver import (
    Version,
    VersionRange,
    bump_level,
    is_version,
    level_at_least,
    max_level,
)


def test_parse_and_order() -> None:
    assert str(Version.parse("1.10.0")) == "1.10.0"
    assert Version.parse("1.10.0") > Version.parse("1.9.9")
    for bad in ("1.0", "01.0.0", "1.0.0-rc1", "v1.0.0"):
        assert not is_version(bad)
        with pytest.raises(ValueError):
            Version.parse(bad)


@pytest.mark.parametrize(
    ("old", "new", "level"),
    [
        ("1.0.0", "1.0.0", "none"),
        ("1.0.0", "1.0.1", "patch"),
        ("1.0.1", "1.1.0", "minor"),
        ("1.1.0", "2.0.0", "major"),
        ("2.0.0", "1.9.0", "downgrade"),
    ],
)
def test_bump_level(old: str, new: str, level: str) -> None:
    assert bump_level(Version.parse(old), Version.parse(new)) == level


def test_levels() -> None:
    assert max_level(["patch", "major", "minor"]) == "major"
    assert max_level([]) == "none"
    assert level_at_least("major", "minor")
    assert not level_at_least("patch", "minor")


@pytest.mark.parametrize(
    ("spec", "inside", "outside"),
    [
        ("^1.0", ["1.0.0", "1.9.3"], ["0.9.0", "2.0.0"]),
        ("^1.2.3", ["1.2.3", "1.3.0"], ["1.2.2", "2.0.0"]),
        ("^0.2.3", ["0.2.3", "0.2.9"], ["0.3.0"]),
        ("^0.0.3", ["0.0.3"], ["0.0.4"]),
        ("~1.2", ["1.2.0", "1.2.9"], ["1.3.0"]),
        ("~1", ["1.0.0", "1.9.0"], ["2.0.0"]),
        ("1.2.3", ["1.2.3"], ["1.2.4"]),
    ],
)
def test_ranges(spec: str, inside: list[str], outside: list[str]) -> None:
    version_range = VersionRange.parse(spec)
    assert all(version_range.matches(Version.parse(v)) for v in inside)
    assert not any(version_range.matches(Version.parse(v)) for v in outside)


@pytest.mark.parametrize("spec", ["1.2", ">=1.0", "^x", ""])
def test_invalid_ranges(spec: str) -> None:
    with pytest.raises(ValueError):
        VersionRange.parse(spec)
