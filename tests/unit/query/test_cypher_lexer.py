"""Tokens of the Cypher subset (plan cypher_query.md 3.2)."""

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.ast import Pos
from graph_cli.query.cypher.lexer import tokenize


def kinds_and_texts(text: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text) for t in tokenize(text)]


def test_names_symbols_and_keywords_stay_words() -> None:
    assert kinds_and_texts("match (n:Note)-->(m) RETURN n") == [
        ("ident", "match"),
        ("symbol", "("),
        ("ident", "n"),
        ("symbol", ":"),
        ("ident", "Note"),
        ("symbol", ")"),
        ("symbol", "-"),
        ("symbol", "-"),
        ("symbol", ">"),
        ("symbol", "("),
        ("ident", "m"),
        ("symbol", ")"),
        ("ident", "RETURN"),
        ("ident", "n"),
        ("eof", ""),
    ]


def test_longest_symbol_wins() -> None:
    assert [t.text for t in tokenize("<> <= >= =~ .. < -")][:-1] == [
        "<>",
        "<=",
        ">=",
        "=~",
        "..",
        "<",
        "-",
    ]


def test_escaped_names_keep_colons_and_doubled_backticks() -> None:
    assert kinds_and_texts("`ex:Note` `a``b`")[:2] == [("escaped", "ex:Note"), ("escaped", "a`b")]


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("42", "integer", "42"),
        ("1.5", "float", "1.5"),
        (".5", "float", ".5"),
        ("1e3", "float", "1e3"),
        ("2.5E-2", "float", "2.5E-2"),
        ("'it\\'s'", "string", "it's"),
        ('"a\\tb\\n"', "string", "a\tb\n"),
        ("'\\u00e9\\U0001F600'", "string", "é\U0001f600"),
        ("'say \"hi\"'", "string", 'say "hi"'),
        ("$repo", "param", "repo"),
        ("$0", "param", "0"),
        ("$`odd name`", "param", "odd name"),
    ],
)
def test_literals_and_parameters(text: str, kind: str, value: str) -> None:
    (token, eof) = tokenize(text)
    assert (token.kind, token.text) == (kind, value)
    assert eof.kind == "eof"


def test_range_is_not_a_float() -> None:
    assert kinds_and_texts("*1..3")[:4] == [
        ("symbol", "*"),
        ("integer", "1"),
        ("symbol", ".."),
        ("integer", "3"),
    ]


def test_comments_are_skipped_and_positions_count_lines() -> None:
    tokens = tokenize("// first\nMATCH /* a\nb */ (n)\n  RETURN n")
    assert [t.text for t in tokens] == ["MATCH", "(", "n", ")", "RETURN", "n", ""]
    assert tokens[0].pos == Pos(2, 1, 9)
    assert tokens[1].pos.line == 3 and tokens[1].pos.column == 6
    assert tokens[4].pos == Pos(4, 3, 31)
    assert tokens[-1].pos.line == 4


@pytest.mark.parametrize(
    ("text", "message", "column"),
    [
        ("RETURN 'abc", "unterminated string", 8),
        ("RETURN 1 /* x", "unterminated comment", 10),
        ("RETURN `abc", "unterminated `name`", 8),
        ("RETURN ``", "empty `name`", 8),
        ("RETURN #", "unexpected character '#'", 8),
        ("RETURN '\\q'", "invalid escape \\q", 9),
        ("RETURN '\\u12'", "invalid escape \\u", 9),
        ("RETURN '\\uD800'", "invalid escape \\u", 9),
        ("RETURN 9223372036854775808", "out of range", 8),
        ("RETURN 1abc", "invalid number", 8),
        ("RETURN $ x", "expected a parameter name", 8),
    ],
)
def test_lexical_errors_name_the_position(text: str, message: str, column: int) -> None:
    with pytest.raises(GraphCliError) as caught:
        tokenize(text)
    assert caught.value.code == "query.syntax"
    assert message in caught.value.message
    assert caught.value.details["line"] == 1
    assert caught.value.details["column"] == column


def test_largest_integer_is_accepted() -> None:
    assert tokenize("9223372036854775807")[0].kind == "integer"
