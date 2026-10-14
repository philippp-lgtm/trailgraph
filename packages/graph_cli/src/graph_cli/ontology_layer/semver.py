"""Semantic versions and import ranges for ontology modules (contracts B7.1, B7.12).

v1 accepts plain ``MAJOR.MINOR.PATCH`` only: no pre-release or build
metadata. Ranges: exact ``1.2.3``, caret ``^1.2[.3]`` and tilde ``~1.2[.3]``
with npm semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

Level = Literal["none", "patch", "minor", "major"]
LEVELS: Final[tuple[Level, ...]] = ("none", "patch", "minor", "major")

_VERSION_RE: Final = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RANGE_RE: Final = re.compile(r"^([\^~]?)(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?$")


@dataclass(frozen=True, slots=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _VERSION_RE.match(text)
        if match is None:
            raise ValueError(f"not a version (MAJOR.MINOR.PATCH): {text!r}")
        return cls(int(match[1]), int(match[2]), int(match[3]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def is_version(text: str) -> bool:
    return _VERSION_RE.match(text) is not None


def max_level(levels: list[Level] | tuple[Level, ...]) -> Level:
    return max(levels, key=LEVELS.index, default="none")


def bump_level(old: Version, new: Version) -> Level | Literal["downgrade"]:
    """The semver level of the step from ``old`` to ``new``."""
    if new < old:
        return "downgrade"
    if new.major != old.major:
        return "major"
    if new.minor != old.minor:
        return "minor"
    if new.patch != old.patch:
        return "patch"
    return "none"


def level_at_least(declared: Level, required: Level) -> bool:
    return LEVELS.index(declared) >= LEVELS.index(required)


@dataclass(frozen=True, slots=True)
class VersionRange:
    text: str
    lower: Version
    upper: Version | None  # exclusive; None for an exact version

    @classmethod
    def parse(cls, text: str) -> VersionRange:
        match = _RANGE_RE.match(text)
        if match is None:
            raise ValueError(f"not a version range: {text!r}")
        op, major_s, minor_s, patch_s = match.groups()
        major, minor, patch = int(major_s), int(minor_s or 0), int(patch_s or 0)
        lower = Version(major, minor, patch)
        if op == "":
            if minor_s is None or patch_s is None:
                raise ValueError(f"an exact version needs MAJOR.MINOR.PATCH: {text!r}")
            return cls(text, lower, None)
        if op == "~":
            upper = Version(major + 1, 0, 0) if minor_s is None else Version(major, minor + 1, 0)
        elif major > 0 or minor_s is None:
            upper = Version(major + 1, 0, 0)
        elif minor > 0 or patch_s is None:
            upper = Version(0, minor + 1, 0)
        else:
            upper = Version(0, 0, patch + 1)
        return cls(text, lower, upper)

    def matches(self, version: Version) -> bool:
        if self.upper is None:
            return version == self.lower
        return self.lower <= version < self.upper
