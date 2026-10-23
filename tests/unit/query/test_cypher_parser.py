"""Parser of the read-only Cypher subset (plan cypher_query.md 3.2, 6 "Parser")."""

import random
import re
from decimal import Decimal

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.ast import (
    Binary,
    Case,
    Const,
    CountStar,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListExpr,
    MapExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Projection,
    ProjectionItem,
    Property,
    Query,
    RelPattern,
    Return,
    SortItem,
    Unary,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.parser import MAX_DEPTH, parse

V = Variable
C = Const


def node(
    variable: str | None = None, *labels: tuple[str, ...], props: MapExpr | None = None
) -> NodePattern:
    return NodePattern(variable, tuple(labels), props)


def returned(text: str) -> Expr:
    """The expression of ``RETURN <text>``."""
    return parse(f"RETURN {text}").returns.projection.items[0].expr


def where(text: str) -> Expr:
    match = parse(f"MATCH (n) WHERE {text} RETURN n").clauses[0]
    assert isinstance(match, Match) and match.where is not None
    return match.where


def pattern(text: str) -> PatternPart:
    match = parse(f"MATCH {text} RETURN 1").clauses[0]
    assert isinstance(match, Match)
    return match.patterns[0]


def failure(text: str) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        parse(text)
    return caught.value


# Clauses


def test_match_where_return() -> None:
    query = parse("MATCH (n:Note) WHERE n.title = 'x' RETURN n")
    assert query == Query(
        (
            Match(
                (PatternPart((node("n", ("Note",)),)),),
                Binary("=", Property(V("n"), "title"), C("x")),
            ),
            Return(Projection((ProjectionItem(V("n"), None, "n"),))),
        )
    )


def test_keywords_are_case_insensitive() -> None:
    assert parse("match (n) where n.a = 1 return n") == parse("MATCH (n) WHERE n.a = 1 RETURN n")


def test_several_patterns_and_match_clauses() -> None:
    query = parse("MATCH (a), (b) MATCH (c) OPTIONAL MATCH (d) RETURN a")
    first, second, third = query.clauses[:3]
    assert isinstance(first, Match) and len(first.patterns) == 2
    assert isinstance(second, Match) and not second.optional
    assert isinstance(third, Match) and third.optional


def test_with_unwind_and_paging() -> None:
    query = parse(
        "UNWIND [1, 2] AS x WITH DISTINCT x AS y, count(*) AS c WHERE c > 1 "
        "RETURN y ORDER BY y DESC, c SKIP 1 LIMIT $page"
    )
    unwind, with_, ret = query.clauses
    assert unwind == Unwind(ListExpr((C(1), C(2))), "x")
    assert with_ == With(
        Projection(
            (ProjectionItem(V("x"), "y", "x"), ProjectionItem(CountStar(), "c", "count(*)")),
            distinct=True,
        ),
        Binary(">", V("c"), C(1)),
    )
    assert ret == Return(
        Projection(
            (ProjectionItem(V("y"), None, "y"),),
            order=(SortItem(V("y"), descending=True), SortItem(V("c"))),
            skip=C(1),
            limit=Parameter("page"),
        )
    )


@pytest.mark.parametrize(
    ("words", "descending"),
    [("", False), ("ASC", False), ("ascending", False), ("DESC", True), ("DESCENDING", True)],
)
def test_sort_directions(words: str, descending: bool) -> None:
    projection = parse(f"MATCH (n) RETURN n ORDER BY n.a {words}").returns.projection
    assert projection.order == (SortItem(Property(V("n"), "a"), descending),)


def test_return_star_and_column_texts() -> None:
    projection = parse("MATCH (n) RETURN *, n.title,  count( n )  AS k").returns.projection
    assert projection.star
    assert [item.name for item in projection.items] == ["n.title", "k"]
    assert projection.items[1].text == "count( n )"
    assert parse("MATCH (n) RETURN DISTINCT *").returns.projection.distinct


def test_return_alone_and_trailing_semicolon() -> None:
    assert parse("RETURN 1;").returns.projection.items[0].expr == C(1)


def test_with_star_keeps_everything() -> None:
    with_ = parse("MATCH (n) WITH * RETURN n").clauses[1]
    assert isinstance(with_, With) and with_.projection.star and not with_.projection.items


def test_escaped_variable_and_reserved_alias() -> None:
    projection = parse("MATCH (`order`) RETURN `order` AS `limit`").returns.projection
    assert projection.items[0] == ProjectionItem(V("order"), "limit", "`order`")


# Patterns


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(n)", node("n")),
        ("()", node()),
        ("(:Note)", node(None, ("Note",))),
        ("(n:A:B)", node("n", ("A",), ("B",))),
        ("(n:A|B)", node("n", ("A", "B"))),
        ("(n:A|:B:C)", node("n", ("A", "B"), ("C",))),
        ("(n:`ex:Note`)", node("n", ("ex:Note",))),
        (
            "(n {title: 'x', rank: $r})",
            node("n", props=MapExpr((("title", C("x")), ("rank", Parameter("r"))))),
        ),
    ],
)
def test_node_patterns(text: str, expected: NodePattern) -> None:
    assert pattern(text) == PatternPart((expected,))


@pytest.mark.parametrize(
    ("arrow", "expected"),
    [
        ("-->", RelPattern(direction="out")),
        ("<--", RelPattern(direction="in")),
        ("--", RelPattern()),
        ("<-->", RelPattern()),
        ("-[r]->", RelPattern("r", direction="out")),
        ("<-[:cites]-", RelPattern(None, ("cites",), "in")),
        ("-[r:a|b]-", RelPattern("r", ("a", "b"))),
        ("-[:a|:b]->", RelPattern(None, ("a", "b"), "out")),
        ("-[:`ex:cites`]->", RelPattern(None, ("ex:cites",), "out")),
        ("-[*2]->", RelPattern(direction="out", length=(2, 2))),
        ("-[*1..3]->", RelPattern(direction="out", length=(1, 3))),
        ("-[*..5]->", RelPattern(direction="out", length=(1, 5))),
        ("-[*0..2]->", RelPattern(direction="out", length=(0, 2))),
        ("-[*3..1]->", RelPattern(direction="out", length=(3, 1))),
        (
            "-[r:cites*1..2 {role: 'x'}]->",
            RelPattern("r", ("cites",), "out", MapExpr((("role", C("x")),)), (1, 2)),
        ),
    ],
)
def test_relationship_patterns(arrow: str, expected: RelPattern) -> None:
    assert pattern(f"(a){arrow}(b)") == PatternPart((node("a"), node("b")), (expected,))


def test_chains() -> None:
    part = pattern("(a)-[:x]->(b)<-[:y]-(c:C)")
    assert part.nodes == (node("a"), node("b"), node("c", ("C",)))
    assert [r.types for r in part.rels] == [("x",), ("y",)]
    assert [r.direction for r in part.rels] == ["out", "in"]


# Expressions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", C(1)),
        ("1.5", C(Decimal("1.5"))),
        ("'x'", C("x")),
        ("true", C(True)),
        ("FALSE", C(False)),
        ("null", C(None)),
        ("$p", Parameter("p")),
        ("[]", ListExpr(())),
        ("[1, 'a', [null]]", ListExpr((C(1), C("a"), ListExpr((C(None),))))),
        ("{}", MapExpr(())),
        ("{a: 1, `b c`: 'x'}", MapExpr((("a", C(1)), ("b c", C("x"))))),
        ("-1", Unary("-", C(1))),
        ("- -x", Unary("-", Unary("-", V("x")))),
        ("+x", Unary("+", V("x"))),
        ("n.a.b", Property(Property(V("n"), "a"), "b")),
        ("n.count", Property(V("n"), "count")),
        ("n.end", Property(V("n"), "end")),
        ("n:Note", HasLabels(V("n"), (("Note",),))),
        ("(1)", C(1)),
    ],
)
def test_atoms(text: str, expected: Expr) -> None:
    assert returned(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "a OR b XOR c AND NOT d",
            Binary(
                "or", V("a"), Binary("xor", V("b"), Binary("and", V("c"), Unary("not", V("d"))))
            ),
        ),
        ("a OR b OR c", Binary("or", Binary("or", V("a"), V("b")), V("c"))),
        ("NOT NOT a", Unary("not", Unary("not", V("a")))),
        ("NOT a = 1", Unary("not", Binary("=", V("a"), C(1)))),
        ("a + 1 = b * 2", Binary("=", Binary("+", V("a"), C(1)), Binary("*", V("b"), C(2)))),
        ("a - b - c", Binary("-", Binary("-", V("a"), V("b")), V("c"))),
        ("a / b % c", Binary("%", Binary("/", V("a"), V("b")), V("c"))),
        ("a + 1 IN [2]", Binary("in", Binary("+", V("a"), C(1)), ListExpr((C(2),)))),
        ("a IN [1] = true", Binary("=", Binary("in", V("a"), ListExpr((C(1),))), C(True))),
        (
            "1 < a <= 3",
            Binary("and", Binary("<", C(1), V("a")), Binary("<=", V("a"), C(3))),
        ),
        ("a <> b", Binary("<>", V("a"), V("b"))),
        ("a >= b", Binary(">=", V("a"), V("b"))),
        ("a STARTS WITH 'x'", Binary("starts_with", V("a"), C("x"))),
        ("a ends with 'x'", Binary("ends_with", V("a"), C("x"))),
        ("a CONTAINS 'x'", Binary("contains", V("a"), C("x"))),
        ("a IS NULL", IsNull(V("a"))),
        ("a.b IS NOT NULL", IsNull(Property(V("a"), "b"), negated=True)),
        ("NOT a IS NULL", Unary("not", IsNull(V("a")))),
        ("(a OR b) AND c", Binary("and", Binary("or", V("a"), V("b")), V("c"))),
    ],
)
def test_precedence(text: str, expected: Expr) -> None:
    assert returned(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("count(*)", CountStar()),
        ("count(n)", FunctionCall("count", (V("n"),))),
        ("COUNT(DISTINCT n.a)", FunctionCall("count", (Property(V("n"), "a"),), distinct=True)),
        ("collect(n)", FunctionCall("collect", (V("n"),))),
        ("toLower(n.a)", FunctionCall("tolower", (Property(V("n"), "a"),))),
        ("coalesce(n.a, 'x')", FunctionCall("coalesce", (Property(V("n"), "a"), C("x")))),
        ("labels(n)", FunctionCall("labels", (V("n"),))),
        ("exists(n.a)", FunctionCall("exists", (Property(V("n"), "a"),))),
        ("id(n)", FunctionCall("id", (V("n"),))),
        ("all(n)", FunctionCall("all", (V("n"),))),
        ("f()", FunctionCall("f", ())),
    ],
)
def test_function_calls_keep_any_name(text: str, expected: Expr) -> None:
    assert returned(text) == expected


def test_case_forms() -> None:
    assert returned("CASE WHEN a THEN 1 WHEN b THEN 2 ELSE 3 END") == Case(
        None, ((V("a"), C(1)), (V("b"), C(2))), C(3)
    )
    assert returned("CASE n.k WHEN 'x' THEN 1 END") == Case(
        Property(V("n"), "k"), ((C("x"), C(1)),)
    )


def test_pattern_predicates() -> None:
    assert where("NOT (n)-[:superseded_by]->()") == Unary(
        "not",
        PatternPredicate(
            PatternPart((node("n"), node()), (RelPattern(None, ("superseded_by",), "out"),))
        ),
    )
    assert where("(n)<--(:Note) AND (n:Note)") == Binary(
        "and",
        PatternPredicate(
            PatternPart((node("n"), node(None, ("Note",))), (RelPattern(direction="in"),))
        ),
        HasLabels(V("n"), (("Note",),)),
    )


def test_parenthesized_node_alone_is_an_expression() -> None:
    assert where("(n)") == V("n")
    assert where("(n:Note)") == HasLabels(V("n"), (("Note",),))


def test_positions_point_at_the_first_token() -> None:
    query = parse("MATCH (n)\nWHERE n.a = $x\nRETURN n")
    match = query.clauses[0]
    assert isinstance(match, Match) and isinstance(match.where, Binary)
    assert isinstance(match.where.right, Parameter)
    assert (match.where.right.pos.line, match.where.right.pos.column) == (2, 13)
    assert (query.returns.pos.line, query.returns.pos.column) == (3, 1)


# Refused constructs


@pytest.mark.parametrize(
    ("text", "kind", "construct"),
    [
        ("CREATE (n)", "write", "CREATE (a write clause)"),
        ("MATCH (n) MERGE (m)", "write", "MERGE (a write clause)"),
        ("MATCH (n) SET n.a = 1", "write", "SET (a write clause)"),
        ("MATCH (n) DELETE n", "write", "DELETE (a write clause)"),
        ("MATCH (n) DETACH DELETE n", "write", "DETACH DELETE (a write clause)"),
        ("MATCH (n) REMOVE n.a", "write", "REMOVE (a write clause)"),
        ("MATCH (n) FOREACH (x IN [1] | SET n.a = x)", "write", "FOREACH (a write clause)"),
        ("CALL db.labels()", "clause", "CALL (procedures and subqueries)"),
        ("MATCH (n) CALL { RETURN 1 } RETURN n", "clause", "CALL (procedures and subqueries)"),
        ("LOAD CSV FROM 'x' AS row RETURN row", "clause", "LOAD CSV"),
        ("RETURN 1 UNION RETURN 2", "clause", "UNION"),
        ("USE other MATCH (n) RETURN n", "clause", "USE"),
        ("EXPLAIN MATCH (n) RETURN n", "clause", "EXPLAIN"),
        ("profile RETURN 1", "clause", "PROFILE"),
        ("RETURN 1; RETURN 2", "clause", "several statements in one query"),
        ("MATCH p = (a)-->(b) RETURN p", "pattern", "named paths"),
        ("MATCH shortestPath((a)-->(b)) RETURN a", "pattern", "shortestPath"),
        ("MATCH (a), (b) RETURN shortestPath((a)-[*..3]-(b))", "pattern", "shortestPath"),
        ("MATCH (a)-[*]->(b) RETURN a", "pattern", "variable length without an upper bound"),
        ("MATCH (a)-[*2..]->(b) RETURN a", "pattern", "variable length without an upper bound"),
        ("MATCH (a)-[*1..6]->(b) RETURN a", "pattern", "variable length up to 6"),
        ("MATCH (a)-[*9]->(b) RETURN a", "pattern", "variable length up to 9"),
        ("MATCH (n:A&B) RETURN n", "pattern", "label expressions with &"),
        ("MATCH (n:!A) RETURN n", "pattern", "label expressions with !"),
        ("MATCH (n WHERE n.a = 1) RETURN n", "pattern", "WHERE inside a node pattern"),
        ("MATCH (n $props) RETURN n", "expression", "a parameter as property map"),
        ("MATCH (a) RETURN [(a)-->(b) | b]", "expression", "pattern comprehensions"),
        ("MATCH (n) RETURN n {.title}", "expression", "map projections"),
        ("MATCH (n) RETURN n{title: n.title}", "expression", "map projections"),
        ("RETURN {.x}", "expression", "map projections"),
        ("MATCH (n) WHERE EXISTS { (n)-->() } RETURN n", "expression", "EXISTS {...} subqueries"),
        ("MATCH (n) RETURN COUNT { (n)-->() }", "expression", "COUNT {...} subqueries"),
        ("MATCH (n) WHERE n.a =~ 'x.*' RETURN n", "expression", "regular expressions (=~)"),
        ("RETURN 2 ^ 3", "expression", "the power operator ^"),
        ("RETURN filter(x IN [1] WHERE x = 1)", "expression", "filter(... IN ...)"),
        ("RETURN extract(x IN [1] | x)", "expression", "extract(... IN ...)"),
        ("RETURN reduce(s = 0, x IN [1] | s + x)", "expression", "reduce(...)"),
        ("MATCH (n:A|!B) RETURN n", "pattern", "label expressions with !"),
        ("RETURN apoc.text.join(['a'], ',')", "function", "namespaced functions (apoc.text.join)"),
    ],
)
def test_refused_constructs_name_themselves(text: str, kind: str, construct: str) -> None:
    error = failure(text)
    assert error.code == "query.unsupported", error.message
    assert error.details["kind"] == kind
    assert error.details["construct"] == construct
    assert isinstance(error.details["line"], int) and isinstance(error.details["column"], int)


def test_write_refusal_points_at_the_clause() -> None:
    error = failure("MATCH (n)\n  SET n.a = 1")
    assert (error.details["line"], error.details["column"]) == (2, 3)
    assert error.details["alternative"] == "queries are read-only; writes go through a changeset"


# Syntax errors


@pytest.mark.parametrize(
    ("text", "column", "found", "expected"),
    [
        ("", 1, "end of query", ["MATCH", "OPTIONAL MATCH", "UNWIND", "WITH", "RETURN"]),
        ("MATCH (n)", 10, "end of query", ["MATCH", "OPTIONAL MATCH", "UNWIND", "WITH", "RETURN"]),
        ("MATCH (n RETURN n", 10, "RETURN", [")"]),
        ("RETURN 1 +", 11, "end of query", ["an expression"]),
        ("RETURN 1 2", 10, "2", ["end of query"]),
        ("MATCH n RETURN n", 7, "n", ["("]),
        ("OPTIONAL RETURN 1", 10, "RETURN", ["MATCH"]),
        ("RETURN CASE END", 13, "END", ["an expression"]),
        ("RETURN CASE x END", 15, "END", ["WHEN"]),
        ("MATCH (n) RETURN n AS order", 23, "order", ["a variable"]),
        ("UNWIND [1] x RETURN x", 12, "x", ["AS"]),
        ("RETURN 1 != 2", 10, "!", ["an operator"]),
        ("RETURN {a: 1, a: 2}", 15, "a", []),
        ("MATCH (n) WITH n.a RETURN 1", 16, "n.a", ["AS"]),
        ("MATCH (a)-[:]->(b) RETURN a", 13, "]", ["a relationship type"]),
        ("RETURN n.", 10, "end of query", ["a property key"]),
        ("RETURN 1 STARTS 'x'", 17, "a string", ["WITH"]),
    ],
)
def test_syntax_errors_name_position_and_expectation(
    text: str, column: int, found: str, expected: list[str]
) -> None:
    error = failure(text)
    assert error.code == "query.syntax", error.message
    assert error.details["line"] == 1
    assert error.details["column"] == column
    assert error.details["found"] == found
    assert error.details["expected"] == expected
    assert error.message.startswith(f"line 1, column {column}: ")


def test_query_without_return_says_so() -> None:
    assert "a query ends with RETURN" in failure("MATCH (n) WHERE n.a = 1").message


def test_reserved_variable_suggests_backticks() -> None:
    assert "write `order`" in failure("MATCH (n) RETURN n AS order").message


def test_nesting_is_bounded() -> None:
    deep = "(" * (MAX_DEPTH + 5) + "1" + ")" * (MAX_DEPTH + 5)
    error = failure(f"RETURN {deep}")
    assert error.code == "query.syntax" and "nested more than" in error.message
    assert returned("(" * 10 + "1" + ")" * 10) == C(1)
    assert parse("RETURN " + "NOT " * 500 + "true").returns.projection.items


# Fuzz: random token streams end in a result or a query error, never in a crash.

_VOCABULARY = re.findall(
    r"\S+",
    "MATCH OPTIONAL WHERE WITH RETURN UNWIND AS ORDER BY SKIP LIMIT DISTINCT AND OR XOR NOT "
    "IN STARTS ENDS CONTAINS IS NULL CASE WHEN THEN ELSE END true false count collect "
    "CREATE CALL n m r x ( ) [ ] { } , . : | = <> < <= > >= + - * / % .. $p 1 2.5 'a' "
    "`ex:Note` -> <- --> * ; ^ =~ ! &",
)


@pytest.mark.parametrize("seed", range(20))
def test_random_token_streams_never_crash(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        text = " ".join(rng.choice(_VOCABULARY) for _ in range(rng.randint(0, 25)))
        try:
            parse(text)
        except GraphCliError as exc:
            assert exc.code in ("query.syntax", "query.unsupported"), (text, exc.message)


@pytest.mark.parametrize("seed", range(10))
def test_mutated_valid_queries_never_crash(seed: int) -> None:
    rng = random.Random(seed)
    base = (
        "MATCH (a:Note {title: $t})-[r:cites*1..2]->(b) WHERE NOT (b)-->() AND a.x IN [1, 2] "
        "WITH a, count(DISTINCT b) AS k ORDER BY k DESC RETURN a.title AS t, "
        "CASE WHEN k > 1 THEN 'many' ELSE 'one' END AS size SKIP 1 LIMIT 5"
    )
    for _ in range(200):
        chars = list(base)
        for _ in range(rng.randint(1, 4)):
            at = rng.randrange(len(chars))
            if rng.random() < 0.5:
                del chars[at]
            else:
                chars.insert(at, rng.choice("()[]{}:,.'`$-<>*|=! \n"))
        try:
            parse("".join(chars))
        except GraphCliError as exc:
            assert exc.code in ("query.syntax", "query.unsupported"), exc.message
