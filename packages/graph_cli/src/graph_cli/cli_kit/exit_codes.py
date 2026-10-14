"""Exit codes by error category (contracts section 1.5.1, B9.4).

The exit code is a function of the category only. Warnings never change it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from graph_cli.kernel.error_codes import ErrorCategory

EXIT_OK: Final = 0

EXIT_BY_CATEGORY: Final[Mapping[ErrorCategory, int]] = MappingProxyType(
    {
        ErrorCategory.NOT_FOUND: 1,
        ErrorCategory.REFUSED: 1,
        ErrorCategory.INVALID: 1,
        ErrorCategory.USAGE: 2,
        ErrorCategory.DENIED: 3,
        ErrorCategory.CONFLICT: 4,
        ErrorCategory.UNAVAILABLE: 5,
        ErrorCategory.INTERNAL: 6,
    }
)


def exit_code_for(category: ErrorCategory) -> int:
    """Exit code of an error category; a warning category is a caller bug."""
    try:
        return EXIT_BY_CATEGORY[category]
    except KeyError:
        raise ValueError(f"category {category.value!r} has no exit code") from None
