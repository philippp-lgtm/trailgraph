"""Every number in the prose of the documentation is checked against the code or exempted.

- `FACTS` pairs a phrase of a document with the values the code holds: the phrase must
  occur in the document (whitespace collapsed), and the numbers in it, read in order, must
  equal the values computed from the constants. A limit changed in code then fails here
  until the document follows.
- The coverage scan reads the prose of the root README and of every document in docs/
  (`SCANNED`; fenced blocks, code spans, HTML comments and link targets left out) and
  fails on every number that no fact and no `NOT_FACTS` entry covers. A number that is
  not worth a check is removed from the document or replaced by a link to its home
  (CONTRIBUTING.md "Documentation"). Standard names (`RFC 3339`, `SHA-256`, `UTF-8`
  ...), versions, dates and the markers of ordered lists are not numbers. Number words
  ("five graphs") are not scanned.
- An exit code (`exit 2`, `the exit code is 0`) is checked without a table: it must be the
  exit code of the error code or category the text gives it to, the first one in the same
  parentheses, else the nearest before it in the same sentence, else the nearest after it.
  An exit code with no error code in its sentence needs a fact; `0` is always success.
- A format number (`format 2`, `formats 1 and 2`) must name a format of the manifest or the
  index the code reads; a released format keeps its number, so nothing more can go stale.
- Every fact and every exemption must still occur, so the tables cannot rot.

Each scanner has a positive control, so a broken scan cannot pass silently.
"""

import json
import re
from dataclasses import dataclass

import pytest
from harness import REPO_ROOT

from graph_cli.app_cli.commands.read import find
from graph_cli.audit import verify
from graph_cli.catalog import access_log, config, enrollments, file_cache, spill
from graph_cli.cli_kit.exit_codes import EXIT_BY_CATEGORY, EXIT_OK
from graph_cli.extractors import detect, media_types, sqlite, table, text
from graph_cli.kernel import actor, changeset, ids, records
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory
from graph_cli.kinetic import card, view_format
from graph_cli.ontology_layer import affordances
from graph_cli.query import search
from graph_cli.query.cypher import execute, shape
from graph_cli.skills import tree
from graph_cli.skills.graph_skill import catalogs
from graph_cli.sources import reconcile
from graph_cli.storage.files import blobs, index, manifest, s3_blobs, store_marker
from graph_cli.temporal_knowledge import changes
from graph_cli_admin.maintenance_cli import view_server
from graph_cli_admin.maintenance_cli.commands import view as view_command
from graph_cli_admin.services_admin import view as live_view
from graph_cli_admin.services_admin.error_codes import ADMIN_CODES
from graph_cli_server import app as server_app
from graph_cli_server import enroll, http_status
from graph_cli_server import main as server_main

FENCED_BLOCK = re.compile(r"^( *)```.*?^\1```", re.MULTILINE | re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
LIST_MARKER = re.compile(r"^\s*\d+\.\s", re.MULTILINE)
CODE_SPAN_RUN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
NUMBER = re.compile(r"(?<![\w.,])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")
# Names of standards and versions hold digits that are not facts of graph_cli.
NOT_NUMBERS = re.compile(
    r"\b(?:RFC|BCP|ISO) \d+"
    r"|\b(?:SHA|UTF)-\d+"
    r"|\bHTTP/\d(?:\.\d)?"
    r"|JSON Schema \(2020-12\)"
    r"|\bIPv\d"
    r"|\b\d+\.\d+\.\d+\b"
    r"|\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?"
    r"|\b[Ss]kill 1\b"  # the name of the app skill (docs/skills.md)
)
EXIT = re.compile(r"\bexits? (?:code (?:is )?)?(\d+)\b")
FORMAT = re.compile(r"\b[Ff]ormats?[ -]\d+(?:(?:, | and | or | <-> )\d+)*")
SENTENCE_END = re.compile(r"\. |\|")


@dataclass(frozen=True)
class Fact:
    doc: str
    phrase: str
    values: tuple[object, ...]


def fact(doc: str, phrase: str, *values: object) -> Fact:
    return Fact(doc, phrase, values)


def _hours(seconds: float) -> float:
    hours = seconds / 3600
    return int(hours) if hours.is_integer() else hours


def _minutes(seconds: float) -> float:
    minutes = seconds / 60
    return int(minutes) if minutes.is_integer() else minutes


def _mode(mode: int) -> str:
    return f"{mode:04o}"


def _longest(pattern: re.Pattern[str], prefix: str = "") -> int:
    """The length of the longest run of `a` after `prefix` the pattern accepts (`a` is a
    letter and a hex digit)."""
    return max(n for n in range(4096) if pattern.fullmatch(prefix + "a" * n))


def _status(code: str) -> int:
    """The HTTP status of an error answer with `code`."""
    spec = ERROR_CODES[code]
    return http_status.status_for(spec.code, spec.category)


def _openapi_version() -> str:
    document = json.loads((REPO_ROOT / "tests/goldens/server/openapi_app.json").read_text())
    return ".".join(document["openapi"].split(".")[:2])


def _narrow_screen_px() -> int:
    """The width below which the live view lays out for a narrow screen."""
    css = (
        REPO_ROOT / "packages/graph_cli_admin/src/graph_cli_admin/maintenance_cli/static/view.css"
    ).read_text()
    widths = {int(w) for w in re.findall(r"@media \(max-width: (\d+)px\)", css)}
    assert len(widths) == 1, widths
    return widths.pop()


def _compose_ports(service: str) -> list[int]:
    """The container ports a service of `compose.yaml` publishes, in order."""
    text = (REPO_ROOT / "compose.yaml").read_text()
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert block, service
    return [int(port) for port in re.findall(r'^ +- "[^"]*:(\d+)"$', block.group(1), re.MULTILINE)]


def _known_formats() -> set[int]:
    """The format numbers of the manifest and of the index the code reads."""
    return set(manifest.MANIFEST_FORMATS) | {int(f.rsplit("/", 1)[1]) for f in index.READ_FORMATS}


def _shortest(pattern: re.Pattern[str]) -> int:
    return min(n for n in range(1, 4096) if pattern.fullmatch("a" * n))


# The documents whose prose the coverage scan reads: the root README and every document of
# docs/ (CONTRIBUTING.md describes the development process, the admin skills have their own
# checks).
SCANNED = ("README.md", *sorted(f"docs/{p.name}" for p in (REPO_ROOT / "docs").glob("*.md")))

FACTS: tuple[Fact, ...] = (
    # cli_graph.md
    fact(
        "docs/cli_graph.md",
        "older than 24 hours from the same directory",
        _hours(spill.KEEP_SECONDS),
    ),
    fact("docs/cli_graph.md", "in one answer, at most 6 KiB", card.BYTE_LIMIT // 1024),
    fact(
        "docs/cli_graph.md",
        "8-32 trailing hex digits",
        _shortest(ids.ABBREV_RE),
        _longest(ids.ABBREV_RE),
    ),
    fact(
        "docs/cli_graph.md",
        "at most 64 characters), never a location",
        _longest(records.SOURCE_NAME_RE),
    ),
    fact("docs/cli_graph.md", "text of at most 128 characters", records.MAX_SOURCE_TEXT),
    # 1: a key is not empty
    fact("docs/cli_graph.md", "(1 to 512 bytes of printable text", 1, changeset.MAX_KEY_BYTES),
    fact("docs/cli_graph.md", "more retires than 5 and a tenth", reconcile.MIN_RETIRE_LIMIT),
    fact(
        "docs/cli_graph.md",
        "At most 32 scopes; at most 5,000 ops",
        changeset.MAX_RECONCILE_SCOPES,
        changeset.MAX_RECONCILE_OPS,
    ),
    fact("docs/cli_graph.md", "(item keys; at most 20 in a write", reconcile.MAX_LISTED_KEYS),
    fact("docs/cli_graph.md", "At most 1,000 transactions are read per call", changes.SCAN_CAP),
    fact("docs/cli_graph.md", "(at most 50, then `targets_more`", changes.TARGET_CAP),
    fact("docs/cli_graph.md", "each cut at 40 entries", affordances.MAX_ENTRIES),
    fact("docs/cli_graph.md", "A string is cut at 100 characters", card.VALUE_CHARS),
    fact("docs/cli_graph.md", "by name, cut at 60 characters", card.KEY_CHARS),
    fact("docs/cli_graph.md", "The whole data stays below 6 KiB", card.BYTE_LIMIT // 1024),
    fact("docs/cli_graph.md", "values to 40 characters", card.GUARD_VALUE_CHARS),
    fact("docs/cli_graph.md", "the card's limit of 6 KiB", view_format.ENTITY_MAX_BYTES // 1024),
    fact("docs/cli_graph.md", "up to 500 per list", view_format.LIST_RELATION_MAX_DEFAULT),
    fact("docs/cli_graph.md", "`lookup` takes up to 50 names", find.MAX_LOOKUP_TEXTS),
    fact("docs/cli_graph.md", "at least 70% of", round(search.SIMILAR_MIN * 100)),
    fact("docs/cli_graph.md", "an `excerpt` of up to 160 characters", search.EXCERPT_CHARS),
    fact("docs/cli_graph.md", "more than 100,000 candidate", execute.DEFAULT_BUDGET),
    # configuration.md
    fact("docs/configuration.md", "`lock_timeout_s` (up to 3600)", config.MAX_LOCK_TIMEOUT_S),
    # 0: the loader takes any positive number
    fact("docs/configuration.md", "(above 0 up to 3600)", 0, config.MAX_EXTRACT_TIMEOUT_S),
    fact(
        "docs/configuration.md",
        "(64 to 65536)",
        config.MIN_EXTRACT_MEMORY_MIB,
        config.MAX_EXTRACT_MEMORY_MIB,
    ),
    fact(
        "docs/configuration.md",
        "`timeout_s` (1 to 3600)",
        config.MIN_REMOTE_TIMEOUT_S,
        config.MAX_REMOTE_TIMEOUT_S,
    ),
    # files.md
    fact("docs/files.md", "the magic bytes of its first 16 bytes", media_types.SNIFF_BYTES),
    fact("docs/files.md", "`column_names` (the first 200)", table.MAX_COLUMN_NAMES),
    fact("docs/files.md", "per table and view (the first 500", sqlite.MAX_OBJECTS),
    fact("docs/files.md", "from the first 64 KiB", detect.DETECT_BYTES // 1024),
    fact(
        "docs/files.md",
        "cut to 200 characters and 400 bytes",
        text.MAX_CHARACTERS,
        text.MAX_BYTES,
    ),
    fact(
        "docs/files.md", "nobody used for 24 hours", _hours(file_cache.UNUSED_FOR.total_seconds())
    ),
    fact(
        "docs/files.md",
        "older than 24 hours by the store's clock",
        _hours(s3_blobs.GC_GRACE.total_seconds()),
    ),
    fact("docs/files.md", "its manifest is format 2", manifest.MANIFEST_FORMAT_REQUIRES),
    # skills.md
    fact("docs/skills.md", "the 1,024 characters of a", tree.MAX_DESCRIPTION),
    fact(
        "docs/skills.md",
        "at most 12 functions and 8 actions",
        catalogs.FEATURED_LIMIT,
        catalogs.ACTIONS_FEATURED_LIMIT,
    ),
    # access_control.md
    fact("docs/access_control.md", "`_`, up to 64 characters)", _longest(actor.ROLE_NAME_RE)),
    fact("docs/access_control.md", "A token holds at most 16 roles", actor.MAX_ROLES),
    # live_view.md
    fact("docs/live_view.md", "more than 250 revisions", live_view.RELOAD_REVISIONS),
    fact("docs/live_view.md", "(1,000 events)", view_server.CLIENT_QUEUE),
    fact("docs/live_view.md", "It keeps the last 500 steps", live_view.TRAIL_STEPS),
    fact("docs/live_view.md", "at least 32 characters", view_command.MIN_TOKEN),
    fact("docs/live_view.md", "the 15 seconds between heartbeats", int(view_server.HEARTBEAT_S)),
    fact("docs/live_view.md", "(720 pixels or less", _narrow_screen_px()),
    # server.md
    fact("docs/server.md", "`--threads` (default 8)", server_main.DEFAULT_THREADS),
    fact("docs/server.md", "refused with 403 and `remote.unauthorized`", http_status.SCOPE_REFUSED),
    fact(
        "docs/server.md",
        "(`gce_` and 26 characters)",
        _longest(enrollments.CODE_RE, "gce_"),
    ),
    fact(
        "docs/server.md",
        "for at most 24 hours",
        _hours(enrollments.MAX_LIFETIME.total_seconds()),
    ),
    fact(
        "docs/server.md",
        "default 15 minutes",
        _minutes(enrollments.DEFAULT_LIFETIME.total_seconds()),
    ),
    fact("docs/server.md", "(at most 4 KiB, no token", enroll.MAX_BODY_BYTES // 1024),
    fact("docs/server.md", "`remote.unauthorized` (401)", _status("remote.unauthorized")),
    fact(
        "docs/server.md",
        "(200 ok, 400 usage, 401 no valid token, 403 denied, 404 not found, 409 conflict, 422"
        " refused or invalid, 503 unavailable, 500 internal)",
        200,  # a success; every error answer has its category's status
        http_status.STATUS_BY_CATEGORY[ErrorCategory.USAGE],
        _status("remote.unauthorized"),
        http_status.STATUS_BY_CATEGORY[ErrorCategory.DENIED],
        http_status.STATUS_BY_CATEGORY[ErrorCategory.NOT_FOUND],
        http_status.STATUS_BY_CATEGORY[ErrorCategory.CONFLICT],
        http_status.STATUS_BY_CATEGORY[ErrorCategory.REFUSED],
        http_status.STATUS_BY_CATEGORY[ErrorCategory.UNAVAILABLE],
        http_status.STATUS_BY_CATEGORY[ErrorCategory.INTERNAL],
    ),
    fact(
        "docs/server.md",
        "a retryable 503 (such as `backend.lock_timeout`)",
        _status("backend.lock_timeout"),
    ),
    fact("docs/server.md", "an OpenAPI 3.1 document", _openapi_version()),
    fact(
        "docs/server.md",
        "`conflict.idempotency_key_reuse` (409;",
        _status("conflict.idempotency_key_reuse"),
    ),
    fact("docs/server.md", "`remote.client_outdated` (422,", _status("remote.client_outdated")),
    fact("docs/server.md", "`skills.outdated` (422)", _status("skills.outdated")),
    fact("docs/server.md", "at most 1,000 files", server_app.MAX_DIRECTORY_FILES),
    fact("docs/server.md", "`channel_timeout` (120 seconds)", server_main.CHANNEL_TIMEOUT_S),
    fact("docs/server.md", "on port 8471", _compose_ports("graph")[1]),
    fact("docs/server.md", "then 80 and 443 are the only ports", *_compose_ports("caddy")),
    # architecture.md
    fact(
        "docs/architecture.md",
        "24-hour expiry",
        _hours(file_cache.UNUSED_FOR.total_seconds()),
    ),
    fact("docs/architecture.md", "magic bytes of the first 16 bytes", media_types.SNIFF_BYTES),
    # storage.md
    fact("docs/storage.md", "once 200 txs were written", index.FULL_CHECK_EVERY),
    fact("docs/storage.md", "at least every 200 txs", index.FULL_CHECK_EVERY),
    fact("docs/storage.md", "for 10 minutes", _minutes(store_marker.IDENTITY_TTL_S)),
    fact(
        "docs/storage.md",
        "older than 12 hours",
        _hours(s3_blobs.PROMOTE_REFRESH.total_seconds()),
    ),
    fact("docs/storage.md", "above 64 unconfirmed hashes", s3_blobs.LISTING_THRESHOLD),
    fact("docs/storage.md", "mode 0444", _mode(blobs.BLOB_MODE)),
    fact(
        "docs/storage.md",
        "older than 24 hours by the store's clock",
        _hours(s3_blobs.GC_GRACE.total_seconds()),
    ),
    fact(
        "docs/storage.md",
        "removed by gc after 24 hours",
        _hours(s3_blobs.GC_GRACE.total_seconds()),
    ),
    # audit.md
    fact("docs/audit.md", "text of up to 128 characters", records.MAX_SOURCE_TEXT),
    fact("docs/audit.md", "file mode 0600", _mode(access_log.FILE_MODE)),
    fact("docs/audit.md", "`null` above 4 KiB", shape.SHAPE_MAX_BYTES // 1024),
    fact("docs/audit.md", "logged as text up to 4 KiB", shape.SHAPE_MAX_BYTES // 1024),
    fact("docs/audit.md", "lists the first 20 revisions", verify.MAX_LISTED_REVISIONS),
)

# Phrases whose numbers are not facts of the code, with the reason.
NOT_FACTS: dict[tuple[str, str], str] = {
    ("docs/cli_graph.md", "`rev` (1 is the creation)"): "a definition: revisions count from 1",
    ("docs/cli_graph.md", "not the number 2.1"): "an example value",
    (
        "docs/configuration.md",
        "`spill_bytes` (0 turns it off)",
    ): "a definition: 0 is the off switch",
    ("docs/files.md", "`last_page` (from 1)"): "a definition: pages count from 1",
    ("docs/files.md", "`files` 1.1"): "the module version that added the format record",
    ("docs/files.md", "a store answers 403 instead of 404"): "how S3 stores answer, not graph_cli",
    ("docs/cli_graph_admin.md", "a missing key answering 404 (403 means"): "how S3 stores answer",
    ("docs/live_view.md", "limited to about 100 bytes"): "the operating system's socket path limit",
    ("docs/server.md", "answers 405 with `Allow`"): "HTTP semantics (RFC 9110)",
    ("docs/server.md", "(409; the draft says 422)"): "what the IETF idempotency draft says",
    ("docs/server.md", "answers 304 without them"): "HTTP semantics (RFC 9110)",
    ("docs/server.md", "off (404)"): "HTTP semantics: the route is absent",
    ("docs/server.md", "does step 1"): "refers to the first step of the list above",
    ("docs/server.md", "often 30 to 60 seconds"): "the usual defaults of reverse proxies",
    ("docs/storage.md", "on a 404 of a referenced object"): "how S3 stores answer",
    ("docs/audit.md", "(`blobs` and `bytes` 0)"): "a definition: the store is empty",
}


def _source(doc: str) -> str:
    return (REPO_ROOT / doc).read_text()


def _prose(text: str) -> str:
    """The prose of a document with whitespace collapsed: no fenced blocks, comments,
    link targets or list markers."""
    text = FENCED_BLOCK.sub(" ", text)
    text = HTML_COMMENT.sub(" ", text)
    text = LINK_TARGET.sub("]", text)
    text = LIST_MARKER.sub(" ", text)
    return re.sub(r"\s+", " ", text)


def _numbers(prose: str) -> list[tuple[int, str]]:
    """The numbers of a prose text with their positions, outside code spans and names
    of standards."""
    masked = prose
    for pattern in (CODE_SPAN_RUN, NOT_NUMBERS):
        masked = pattern.sub(lambda m: " " * len(m.group()), masked)
    return [(m.start(), m.group()) for m in NUMBER.finditer(masked)]


def _exit_codes() -> dict[str, int]:
    """The exit code of every error code of both programs and of every error category."""
    specs = [*ERROR_CODES.values(), *ADMIN_CODES]
    codes = {s.code: EXIT_BY_CATEGORY[s.category] for s in specs if s.category in EXIT_BY_CATEGORY}
    return codes | {
        c.value: n for c, n in EXIT_BY_CATEGORY.items() if c is not ErrorCategory.WARNING
    }


def _named_codes(prose: str, known: dict[str, int]) -> list[tuple[int, str]]:
    return [
        (m.start(), m.group(2).strip())
        for m in CODE_SPAN_RUN.finditer(prose)
        if m.group(2).strip() in known
    ]


def _enclosing_parens(prose: str, pos: int) -> range | None:
    depth = 0
    for start in range(pos, -1, -1):
        depth += {")": 1, "(": -1}.get(prose[start], 0)
        if depth < 0:
            end = start
            depth = 0
            for end in range(start + 1, len(prose)):
                depth += {"(": 1, ")": -1}.get(prose[end], 0)
                if depth < 0:
                    break
            return range(start, end)
    return None


def _sentence(prose: str, pos: int) -> range:
    starts = [m.end() for m in SENTENCE_END.finditer(prose, 0, pos)]
    end = SENTENCE_END.search(prose, pos)
    return range(starts[-1] if starts else 0, end.start() if end else len(prose))


def _exit_problems(prose: str, known: dict[str, int]) -> tuple[list[str], list[int]]:
    """The exit codes that do not match their error code, and the positions of the exit
    codes that have no error code to compare with."""
    named = _named_codes(prose, known)
    problems: list[str] = []
    unpaired: list[int] = []
    for match in EXIT.finditer(prose):
        pos, number = match.start(1), int(match.group(1))
        if number == EXIT_OK:
            continue
        parens = _enclosing_parens(prose, pos)
        inside = [c for c in named if parens and c[0] in parens]
        sentence = _sentence(prose, pos)
        before = [c for c in named if c[0] in sentence and c[0] < pos]
        after = [c for c in named if c[0] in sentence and c[0] > pos]
        chosen = inside[:1] or before[-1:] or after[:1]
        if not chosen:
            unpaired.append(pos)
        elif known[chosen[0][1]] != number:
            code = chosen[0][1]
            problems.append(f"{code} exits {known[code]}, the text says exit {number}")
    return problems, unpaired


def _token(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _fact_problem(prose: str, item: Fact) -> str | None:
    phrase = re.sub(r"\s+", " ", item.phrase)
    if phrase not in prose:
        return f"{item.doc}: phrase not found: {phrase!r}"
    found = [number.replace(",", "") for _, number in _numbers(phrase)]
    expected = [_token(value) for value in item.values]
    if found != expected:
        return f"{item.doc}: {phrase!r} says {found}, the code {expected}"
    return None


def _covered(prose: str, phrases: list[str]) -> list[range]:
    ranges: list[range] = []
    for phrase in phrases:
        needle = re.sub(r"\s+", " ", phrase)
        start = prose.find(needle)
        while start != -1:
            ranges.append(range(start, start + len(needle)))
            start = prose.find(needle, start + 1)
    return ranges


def _uncovered(prose: str, phrases: list[str], exits: list[int] | None = None) -> list[str]:
    """The numbers of a prose text that no phrase covers, each with a little context; the
    exit codes are covered by the exit check, except those at `exits` (unpaired), and the
    format numbers by the format check."""
    ranges = _covered(prose, phrases) + [range(*m.span()) for m in FORMAT.finditer(prose)]
    paired = {m.start(1) for m in EXIT.finditer(prose)} - set(exits or ())
    return [
        f"{number!r} in ...{prose[max(0, pos - 50) : pos + 30]}..."
        for pos, number in _numbers(prose)
        if pos not in paired and not any(pos in r for r in ranges)
    ]


def _unknown_formats(prose: str, known: set[int]) -> list[str]:
    return [
        match.group()
        for match in FORMAT.finditer(prose)
        if not {int(n) for n in re.findall(r"\d+", match.group())} <= known
    ]


def _phrases(doc: str) -> list[str]:
    return [f.phrase for f in FACTS if f.doc == doc] + [
        phrase for (d, phrase) in NOT_FACTS if d == doc
    ]


def test_numbers_scanner_finds_numbers_and_skips_names() -> None:
    prose = _prose(
        "Up to 6 KiB, 8-32 hex digits, at most 1,000 txs and 70% of the words.\n"
        "1. a list item\n`--limit 500` and\n```\n1234\n```\n"
        "RFC 3339, SHA-256, UTF-8, HTTP/1.1, version 0.30.0, 2026-11-25, BCP 47.\n"
        "<!-- 99 --> [a link](notes_2.md)\n"
    )
    assert [number for _, number in _numbers(prose)] == ["6", "8", "32", "1,000", "70"]


def test_fact_check_compares_the_numbers_of_the_phrase() -> None:
    prose = _prose("A page holds at most\n  1,000 transactions (`--limit 5`).")
    assert _fact_problem(prose, fact("x.md", "at most 1,000 transactions", 1000)) is None
    assert "the code ['999']" in str(_fact_problem(prose, fact("x.md", "at most 1,000", 999)))
    assert "not found" in str(_fact_problem(prose, fact("x.md", "at most 2,000", 2000)))
    assert _uncovered(prose, ["at most 1,000 transactions"]) == []
    assert len(_uncovered(prose, [])) == 1


def test_exit_check_pairs_codes() -> None:
    known = {"query.syntax": 2, "entity.not_found": 1, "invalid": 1}
    prose = _prose(
        "`query.syntax` (exit 2, with a hint). An `invalid` refusal (exit 1). "
        "A usage error (exit 2: `query.syntax`). `entity.not_found` (exit 2). "
        "Nothing to name (exit 4). The exit code is 0."
    )
    problems, unpaired = _exit_problems(prose, known)
    assert problems == ["entity.not_found exits 1, the text says exit 2"]
    assert [prose[pos] for pos in unpaired] == ["4"]
    assert [line.split(" in ")[0] for line in _uncovered(prose, [], unpaired)] == ["'4'"]


def test_format_check_reads_every_number() -> None:
    prose = _prose("Formats 1 and 2, format-1 manifests, format 1 <-> 3, a format 7.")
    assert _unknown_formats(prose, {1, 2}) == ["format 1 <-> 3", "format 7"]
    assert _uncovered(prose, []) == []


def test_helpers_probe_patterns() -> None:
    assert _longest(re.compile(r"^[a-z][a-z0-9_]{0,63}$")) == 64
    assert _shortest(re.compile(r"^[0-9a-f]{8,32}$")) == 8
    assert _hours(24 * 3600) == 24
    assert _hours(5400) == 1.5
    assert _longest(re.compile(r"^gce_[a-z2-7]{26}\Z"), "gce_") == 26
    assert _compose_ports("caddy") == [80, 443]


@pytest.mark.parametrize("item", FACTS, ids=lambda f: f"{f.doc}:{f.phrase[:40]}")
def test_fact_matches_the_code(item: Fact) -> None:
    problem = _fact_problem(_prose(_source(item.doc)), item)
    assert problem is None, problem


@pytest.mark.parametrize("doc", SCANNED)
def test_every_number_is_a_fact_or_exempted(doc: str) -> None:
    prose = _prose(_source(doc))
    _, unpaired = _exit_problems(prose, _exit_codes())
    uncovered = _uncovered(prose, _phrases(doc), unpaired)
    assert not uncovered, "\n".join(uncovered)


@pytest.mark.parametrize("doc", SCANNED)
def test_format_numbers_exist(doc: str) -> None:
    unknown = _unknown_formats(_prose(_source(doc)), _known_formats())
    assert not unknown, unknown


@pytest.mark.parametrize("doc", SCANNED)
def test_exit_codes_match_their_error_codes(doc: str) -> None:
    problems, _ = _exit_problems(_prose(_source(doc)), _exit_codes())
    assert not problems, problems


def test_exemptions_occur() -> None:
    missing = [
        f"{doc}: {phrase!r}"
        for (doc, phrase) in NOT_FACTS
        if re.sub(r"\s+", " ", phrase) not in _prose(_source(doc))
    ]
    assert not missing, missing


def test_facts_and_exemptions_name_scanned_documents() -> None:
    stray = sorted({f.doc for f in FACTS} - set(SCANNED) | {d for d, _ in NOT_FACTS} - set(SCANNED))
    assert not stray, stray
