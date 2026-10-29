"""The tracked function fixtures: a valid set and one broken file per rule."""

from pathlib import Path

from support.fixtures import FIXTURES

FUNCTIONS = FIXTURES / "functions" / "company"
VALID = FUNCTIONS / "valid"
BROKEN_FUNCTIONS = FUNCTIONS / "broken"


def read_dir(path: Path) -> dict[str, str]:
    """File name -> content of the function files in a folder."""
    return {p.name: p.read_text() for p in sorted(path.glob("*.yaml"))}


def valid_sources() -> dict[str, str]:
    return read_dir(VALID)


def edited(file_name: str, old: str, new: str) -> dict[str, str]:
    """The valid sources with one replacement in one file (``old`` must occur)."""
    sources = valid_sources()
    assert old in sources[file_name], old
    sources[file_name] = sources[file_name].replace(old, new)
    return sources
