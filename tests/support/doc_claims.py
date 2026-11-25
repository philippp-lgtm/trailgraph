"""Claim markers: a test names the sentence of a document it backs.

A document marks a guarantee written in prose with ``<!-- claim: <doc stem>.<name> -->`` at
the end of the sentence or list item; the tests that check it carry
``@pytest.mark.doc_claim("<doc stem>.<name>", ...)``. ``tests/test_doc_claims.py`` keeps both
sides matched. This plugin registers the marker and, when a marked test fails, adds to its
report the claims it backs, so a behavior changed on purpose changes the document too.

A marker says which test is responsible for a sentence; it does not prove that the test
checks all of it.
"""

from collections.abc import Generator

import pytest

MARKER = "doc_claim"


def claims_of(item: pytest.Item) -> list[str]:
    """The claim ids the markers of ``item`` name, in order."""
    return [str(arg) for mark in item.iter_markers(MARKER) for arg in mark.args]


def where(claim: str) -> str:
    """The document that holds ``claim``: its prefix is the document's stem."""
    return f"docs/{claim.split('.', 1)[0]}.md"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{MARKER}(*ids): the test backs these claims of the documents (<!-- claim: id -->)",
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    claims = claims_of(item)
    if report.failed and claims:
        lines = [f"  {claim} in {where(claim)}" for claim in claims]
        report.sections.append(
            (
                "documented guarantees",
                "This test backs these sentences:\n"
                + "\n".join(lines)
                + "\nIf the behavior changed on purpose, update them in the same change;"
                " if not, the change breaks what the documentation promises.",
            )
        )
    return report
