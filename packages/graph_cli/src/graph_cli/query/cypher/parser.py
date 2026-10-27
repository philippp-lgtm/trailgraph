"""Recursive-descent parser of the read-only Cypher subset.

Grammar and precedence follow openCypher 9 for the supported part (plan
``cypher_query.md`` 3.2). Constructs outside it are recognized where Cypher
users write them and refused with ``query.unsupported`` (write clauses,
``CALL``, ``UNION``, named paths, unbounded variable length, pattern
comprehensions, map projections, subqueries, ...); anything else that does
not parse is ``query.syntax`` with line, column and the expected tokens.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Final, TypeVar

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.ast import (
    Binary,
    BinaryOp,
    Case,
    Clause,
    Const,
    CountStar,
    Direction,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListComprehension,
    ListExpr,
    MapExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Pos,
    Projection,
    ProjectionItem,
    Property,
    Quantifier,
    QuantifierKind,
    Query,
    RelPattern,
    Return,
    Slice,
    SortItem,
    Subscript,
    Unary,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.errors import PREFIXED_NAME_ADVICE, syntax_error, unsupported
from graph_cli.query.cypher.lexer import Token, tokenize

MAX_HOPS: Final = 5  # upper bound of a variable-length relationship
MAX_DEPTH: Final = 40  # nesting of expressions and patterns
# Words that start or join clauses and operators; not usable as bare names
# of variables and aliases (backticks lift that).
RESERVED: Final = frozenset(
    {
        "ALL",
        "AND",
        "AS",
        "ASC",
        "ASCENDING",
        "BY",
        "CALL",
        "CASE",
        "CONTAINS",
        "CREATE",
        "DELETE",
        "DESC",
        "DESCENDING",
        "DETACH",
        "DISTINCT",
        "ELSE",
        "END",
        "ENDS",
        "FALSE",
        "FOREACH",
        "IN",
        "IS",
        "LIMIT",
        "LOAD",
        "MATCH",
        "MERGE",
        "NOT",
        "NULL",
        "OPTIONAL",
        "OR",
        "ORDER",
        "REMOVE",
        "RETURN",
        "SET",
        "SKIP",
        "STARTS",
        "THEN",
        "TRUE",
        "UNION",
        "UNWIND",
        "WHEN",
        "WHERE",
        "WITH",
        "XOR",
    }
)
CLAUSE_STARTS: Final = ("MATCH", "OPTIONAL MATCH", "UNWIND", "WITH", "RETURN")
_WRITES: Final = ("CREATE", "MERGE", "SET", "DELETE", "REMOVE", "FOREACH")
_COMPARISONS: Final[dict[str, BinaryOp]] = {
    "=": "=",
    "<>": "<>",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
}
_QUANTIFIERS: Final[tuple[QuantifierKind, ...]] = ("all", "any", "none", "single")
_REMOVED_LIST_FUNCTIONS: Final = ("filter", "extract")  # gone since Cypher 10
_SUBQUERIES: Final = ("EXISTS", "COUNT", "COLLECT")
_PATH_FUNCTIONS: Final = ("shortestpath", "allshortestpaths")
_READ_ONLY: Final = "queries are read-only; writes go through a changeset"

T = TypeVar("T")


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.at = 0
        self.depth = 0

    # Token access

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.at + ahead, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.peek()
        self.at = min(self.at + 1, len(self.tokens) - 1)
        return token

    @staticmethod
    def is_word(token: Token, *words: str) -> bool:
        return token.kind == "ident" and token.text.upper() in words

    def at_word(self, *words: str) -> bool:
        return self.is_word(self.peek(), *words)

    def at_symbol(self, *symbols: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.kind == "symbol" and token.text in symbols

    def accept_word(self, word: str) -> bool:
        if self.at_word(word):
            self.advance()
            return True
        return False

    def accept_symbol(self, symbol: str) -> bool:
        if self.at_symbol(symbol):
            self.advance()
            return True
        return False

    def fail(self, *expected: str, message: str | None = None) -> GraphCliError:
        token = self.peek()
        found = token.describe()
        text = message or f"expected {_or_list(expected)}, found {found}"
        return syntax_error(token.pos, text, found=found, expected=expected)

    def expect_word(self, word: str) -> Token:
        if not self.at_word(word):
            raise self.fail(word)
        return self.advance()

    def expect_symbol(self, symbol: str) -> Token:
        if not self.at_symbol(symbol):
            raise self.fail(symbol)
        return self.advance()

    def nested(self, parse: Callable[[], T]) -> T:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.fail(message=f"nested more than {MAX_DEPTH} levels deep")
        try:
            return parse()
        finally:
            self.depth -= 1

    def name(self, what: str) -> str:
        """A label, relationship type, property key or map key: any word or `name`."""
        token = self.peek()
        if token.kind not in ("ident", "escaped"):
            raise self.fail(what)
        self.advance()
        return token.text

    def variable_name(self) -> str:
        token = self.peek()
        if token.kind == "escaped" or (
            token.kind == "ident" and token.text.upper() not in RESERVED
        ):
            self.advance()
            return token.text
        if token.kind == "ident":
            raise self.fail(
                "a variable", message=f"{token.text!r} is a reserved word; write `{token.text}`"
            )
        raise self.fail("a variable")

    def at_variable(self) -> bool:
        token = self.peek()
        return token.kind == "escaped" or (
            token.kind == "ident" and token.text.upper() not in RESERVED
        )

    # Query and clauses

    def query(self) -> Query:
        if self.at_word("EXPLAIN", "PROFILE"):
            token = self.peek()
            raise unsupported(
                token.pos,
                "clause",
                token.text.upper(),
                "graph_cli shows the resolved query with its explain option",
            )
        clauses: list[Clause] = []
        while True:
            token = self.peek()
            if self.is_word(token, "RETURN"):
                clauses.append(self.return_clause())
                break
            clauses.append(self.reading_clause())
        if self.at_word("UNION"):
            raise unsupported(self.peek().pos, "clause", "UNION", "run the queries one by one")
        if self.accept_symbol(";") and self.peek().kind != "eof":
            raise unsupported(self.peek().pos, "clause", "several statements in one query")
        if self.peek().kind != "eof":
            raise self.fail("end of query")
        return Query(tuple(clauses))

    def reading_clause(self) -> Clause:
        token = self.peek()
        if self.is_word(token, "MATCH"):
            return self.match_clause(optional=False)
        if self.is_word(token, "OPTIONAL"):
            self.advance()
            if not self.at_word("MATCH"):
                raise self.fail("MATCH")
            return self.match_clause(optional=True, pos=token.pos)
        if self.is_word(token, "UNWIND"):
            return self.unwind_clause()
        if self.is_word(token, "WITH"):
            return self.with_clause()
        self.refuse_clause(token)
        if token.kind == "eof":
            raise self.fail(*CLAUSE_STARTS, message="a query ends with RETURN")
        raise self.fail(*CLAUSE_STARTS)

    def refuse_clause(self, token: Token) -> None:
        if self.is_word(token, *_WRITES):
            raise unsupported(
                token.pos, "write", f"{token.text.upper()} (a write clause)", _READ_ONLY
            )
        if self.is_word(token, "DETACH"):
            raise unsupported(token.pos, "write", "DETACH DELETE (a write clause)", _READ_ONLY)
        if self.is_word(token, "CALL"):
            raise unsupported(token.pos, "clause", "CALL (procedures and subqueries)")
        if self.is_word(token, "LOAD"):
            raise unsupported(token.pos, "clause", "LOAD CSV")
        if self.is_word(token, "UNION"):
            raise unsupported(token.pos, "clause", "UNION", "run the queries one by one")
        if self.is_word(token, "USE"):
            raise unsupported(token.pos, "clause", "USE", "a query reads the selected graph")

    def match_clause(self, *, optional: bool, pos: Pos | None = None) -> Match:
        start = self.expect_word("MATCH")
        patterns = [self.pattern_part()]
        while self.accept_symbol(","):
            patterns.append(self.pattern_part())
        where = self.expression() if self.accept_word("WHERE") else None
        return Match(tuple(patterns), where, optional, pos or start.pos)

    def unwind_clause(self) -> Unwind:
        start = self.expect_word("UNWIND")
        expr = self.expression()
        self.expect_word("AS")
        return Unwind(expr, self.variable_name(), start.pos)

    def with_clause(self) -> With:
        start = self.expect_word("WITH")
        projection = self.projection(aliases_required=True)
        where = self.expression() if self.accept_word("WHERE") else None
        return With(projection, where, start.pos)

    def return_clause(self) -> Return:
        start = self.expect_word("RETURN")
        return Return(self.projection(aliases_required=False), start.pos)

    def projection(self, *, aliases_required: bool) -> Projection:
        distinct = self.accept_word("DISTINCT")
        star = self.accept_symbol("*")
        items: list[ProjectionItem] = []
        if not star or self.accept_symbol(","):
            items.append(self.projection_item(aliases_required))
            while self.accept_symbol(","):
                items.append(self.projection_item(aliases_required))
        order: list[SortItem] = []
        if self.accept_word("ORDER"):
            self.expect_word("BY")
            order.append(self.sort_item())
            while self.accept_symbol(","):
                order.append(self.sort_item())
        skip = self.expression() if self.accept_word("SKIP") else None
        limit = self.expression() if self.accept_word("LIMIT") else None
        return Projection(tuple(items), star, distinct, tuple(order), skip, limit)

    def projection_item(self, aliases_required: bool) -> ProjectionItem:
        start = self.peek()
        expr = self.expression()
        text = self.text[start.pos.offset : self.tokens[self.at - 1].end]
        if self.accept_word("AS"):
            return ProjectionItem(expr, self.variable_name(), text, start.pos)
        if aliases_required and not isinstance(expr, Variable):
            raise syntax_error(
                start.pos,
                f"expression {text!r} in WITH needs a name: {text} AS <name>",
                found=text,
                expected=["AS"],
            )
        return ProjectionItem(expr, None, text, start.pos)

    def sort_item(self) -> SortItem:
        start = self.peek()
        expr = self.expression()
        descending = False
        if self.at_word("DESC", "DESCENDING"):
            self.advance()
            descending = True
        elif self.at_word("ASC", "ASCENDING"):
            self.advance()
        return SortItem(expr, descending, start.pos)

    # Patterns

    def pattern_part(self) -> PatternPart:
        start = self.peek()
        if self.at_variable() and self.at_symbol("=", ahead=1):
            raise unsupported(
                start.pos, "pattern", "named paths", "return the nodes and relationships instead"
            )
        if start.kind == "ident" and start.text.lower() in _PATH_FUNCTIONS:
            raise unsupported(
                start.pos,
                "pattern",
                start.text,
                "use a variable-length relationship with an upper bound",
            )
        return self.nested(lambda: self.chain(start.pos))

    def chain(self, pos: Pos) -> PatternPart:
        nodes = [self.node_pattern()]
        rels: list[RelPattern] = []
        while self.at_relationship():
            rels.append(self.rel_pattern())
            nodes.append(self.node_pattern())
        return PatternPart(tuple(nodes), tuple(rels), pos)

    def at_relationship(self) -> bool:
        if self.at_symbol("<"):
            return self.at_symbol("-", ahead=1)
        return self.at_symbol("-") and self.at_symbol("-", "[", ahead=1)

    def node_pattern(self) -> NodePattern:
        start = self.expect_symbol("(")
        variable = self.variable_name() if self.at_variable() else None
        labels = self.labels() if self.at_symbol(":") else ()
        properties = self.properties()
        if self.at_word("WHERE"):
            raise unsupported(
                self.peek().pos, "pattern", "WHERE inside a node pattern", "add WHERE after MATCH"
            )
        self.expect_symbol(")")
        return NodePattern(variable, labels, properties, start.pos)

    def labels(self) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        while self.accept_symbol(":"):
            group = [self.label()]
            while self.accept_symbol("|"):
                self.accept_symbol(":")
                group.append(self.label())
            groups.append(tuple(group))
        self.refuse_label_expression()
        return tuple(groups)

    def label(self) -> str:
        self.refuse_label_expression()
        return self.name("a label")

    def refuse_label_expression(self) -> None:
        if self.at_symbol("&", "!"):
            raise unsupported(
                self.peek().pos,
                "pattern",
                f"label expressions with {self.peek().text}",
                "use :A:B for both labels, :A|B for either, NOT n:A in WHERE",
            )

    def properties(self) -> MapExpr | None:
        if self.at_symbol("{"):
            return self.nested(self.map_literal)
        if self.peek().kind == "param":
            raise unsupported(
                self.peek().pos,
                "expression",
                "a parameter as property map",
                "write the map with one parameter per value: {key: $value}",
            )
        return None

    def rel_pattern(self) -> RelPattern:
        start = self.peek()
        left = self.accept_symbol("<")
        self.expect_symbol("-")
        variable: str | None = None
        types: tuple[str, ...] = ()
        length: tuple[int, int] | None = None
        properties: MapExpr | None = None
        if self.accept_symbol("["):
            variable = self.variable_name() if self.at_variable() else None
            if self.accept_symbol(":"):
                found = [self.relationship_type()]
                while self.accept_symbol("|"):
                    self.accept_symbol(":")
                    found.append(self.relationship_type())
                types = tuple(found)
            if self.at_symbol("*"):
                length = self.length()
            properties = self.properties()
            self.expect_symbol("]")
        self.expect_symbol("-")
        right = self.accept_symbol(">")
        direction: Direction = "both" if left == right else "in" if left else "out"
        return RelPattern(variable, types, direction, properties, length, start.pos)

    def relationship_type(self) -> str:
        name = self.name("a relationship type")
        if self.at_symbol(":"):
            token = self.peek()
            raise syntax_error(
                token.pos,
                f"expected ']', found :; {PREFIXED_NAME_ADVICE}",
                found=token.describe(),
                expected=["]"],
                prefixed=True,
            )
        return name

    def length(self) -> tuple[int, int]:
        star = self.expect_symbol("*")
        low = self.integer() if self.peek().kind == "integer" else None
        if not self.accept_symbol(".."):
            if low is None:
                raise _unbounded(star.pos)
            high = low
        elif self.peek().kind == "integer":
            high = self.integer()
        else:
            raise _unbounded(star.pos)
        low = 1 if low is None else low
        if high > MAX_HOPS:
            raise unsupported(
                star.pos,
                "pattern",
                f"variable length up to {high}",
                f"the upper bound is at most {MAX_HOPS}",
            )
        return low, high  # low > high matches nothing, as in openCypher

    def integer(self) -> int:
        return int(self.advance().text)

    # Expressions, lowest precedence first

    def expression(self) -> Expr:
        return self.nested(self.or_expr)

    def or_expr(self) -> Expr:
        return self.left_assoc(self.xor_expr, "OR", "or")

    def xor_expr(self) -> Expr:
        return self.left_assoc(self.and_expr, "XOR", "xor")

    def and_expr(self) -> Expr:
        return self.left_assoc(self.not_expr, "AND", "and")

    def left_assoc(self, operand: Callable[[], Expr], word: str, op: BinaryOp) -> Expr:
        expr = operand()
        while self.at_word(word):
            pos = self.advance().pos
            expr = Binary(op, expr, operand(), pos)
        return expr

    def not_expr(self) -> Expr:
        nots: list[Pos] = []
        while self.at_word("NOT"):
            nots.append(self.advance().pos)
        expr = self.comparison()
        for pos in reversed(nots):
            expr = Unary("not", expr, pos)
        return expr

    def comparison(self) -> Expr:
        left = self.predicate()
        pairs: list[Expr] = []
        while True:
            token = self.peek()
            if token.kind == "symbol" and token.text == "=~":
                raise unsupported(
                    token.pos,
                    "expression",
                    "regular expressions (=~)",
                    "use STARTS WITH, ENDS WITH or CONTAINS",
                )
            if token.kind == "symbol" and token.text == "!" and self.at_symbol("=", ahead=1):
                raise self.fail("an operator", message="'!=' is not Cypher; write <>")
            if token.kind != "symbol" or token.text not in _COMPARISONS:
                break
            self.advance()
            right = self.predicate()
            pairs.append(Binary(_COMPARISONS[token.text], left, right, token.pos))
            left = right
        if not pairs:
            return left
        # a < b < c means a < b AND b < c
        expr = pairs[0]
        for pair in pairs[1:]:
            expr = Binary("and", expr, pair, pair.pos)
        return expr

    def predicate(self) -> Expr:
        expr = self.additive()
        while True:
            token = self.peek()
            if self.is_word(token, "IN"):
                self.advance()
                expr = Binary("in", expr, self.additive(), token.pos)
            elif self.is_word(token, "STARTS", "ENDS"):
                self.advance()
                self.expect_word("WITH")
                op: BinaryOp = "starts_with" if token.text.upper() == "STARTS" else "ends_with"
                expr = Binary(op, expr, self.additive(), token.pos)
            elif self.is_word(token, "CONTAINS"):
                self.advance()
                expr = Binary("contains", expr, self.additive(), token.pos)
            elif self.is_word(token, "IS"):
                self.advance()
                negated = self.accept_word("NOT")
                self.expect_word("NULL")
                expr = IsNull(expr, negated, token.pos)
            else:
                return expr

    def additive(self) -> Expr:
        expr = self.multiplicative()
        while self.at_symbol("+", "-"):
            token = self.advance()
            op: BinaryOp = "+" if token.text == "+" else "-"
            expr = Binary(op, expr, self.multiplicative(), token.pos)
        return expr

    def multiplicative(self) -> Expr:
        expr = self.power()
        while self.at_symbol("*", "/", "%"):
            token = self.advance()
            op: BinaryOp = "*" if token.text == "*" else "/" if token.text == "/" else "%"
            expr = Binary(op, expr, self.power(), token.pos)
        return expr

    def power(self) -> Expr:
        expr = self.unary()
        if self.at_symbol("^"):
            raise unsupported(self.peek().pos, "expression", "the power operator ^")
        return expr

    def unary(self) -> Expr:
        signs: list[Token] = []
        while self.at_symbol("+", "-"):
            signs.append(self.advance())
        expr = self.postfix()
        for sign in reversed(signs):
            expr = Unary("-" if sign.text == "-" else "+", expr, sign.pos)
        return expr

    def postfix(self) -> Expr:
        expr = self.atom()
        while True:
            token = self.peek()
            if self.at_symbol("."):
                self.advance()
                expr = Property(expr, self.name("a property key"), token.pos)
            elif self.at_symbol(":"):
                expr = HasLabels(expr, self.labels(), token.pos)
            elif self.at_symbol("["):
                expr = self.subscript(expr)
            elif self.at_symbol("{") and isinstance(expr, Variable):
                raise unsupported(
                    token.pos,
                    "expression",
                    "map projections",
                    "return the properties one by one: n.key AS key",
                )
            else:
                return expr

    def atom(self) -> Expr:
        token = self.peek()
        if token.kind == "integer":
            self.advance()
            return Const(int(token.text), token.pos)
        if token.kind == "float":
            self.advance()
            return Const(Decimal(token.text), token.pos)
        if token.kind == "string":
            self.advance()
            return Const(token.text, token.pos)
        if token.kind == "param":
            self.advance()
            return Parameter(token.text, token.pos)
        if token.kind == "symbol":
            return self.symbol_atom(token)
        if token.kind == "escaped":
            self.advance()
            return Variable(token.text, token.pos)
        if token.kind == "ident":
            return self.word_atom(token)
        raise self.fail("an expression")

    def symbol_atom(self, token: Token) -> Expr:
        if token.text == "[":
            return self.list_literal()
        if token.text == "{":
            return self.map_literal()
        if token.text == "(":
            pattern = self.try_pattern()
            if pattern is not None:
                return PatternPredicate(pattern, token.pos)
            self.advance()
            expr = self.expression()
            self.expect_symbol(")")
            return expr
        raise self.fail("an expression")

    def word_atom(self, token: Token) -> Expr:
        word = token.text.upper()
        following = self.peek(1)
        if word in ("TRUE", "FALSE"):
            self.advance()
            return Const(word == "TRUE", token.pos)
        if word == "NULL":
            self.advance()
            return Const(None, token.pos)
        if word == "CASE":
            return self.case()
        if word in _SUBQUERIES and following.kind == "symbol" and following.text == "{":
            raise unsupported(
                token.pos,
                "expression",
                f"{word} {{...}} subqueries",
                "use a pattern in WHERE, or MATCH with count() or collect()",
            )
        if following.kind == "symbol" and following.text == "(":
            return self.function_call(token)
        if following.kind == "symbol" and following.text == "." and self.at_namespaced_call():
            raise unsupported(token.pos, "function", f"namespaced functions ({self.dotted_name()})")
        if word in RESERVED:
            raise self.fail("an expression")
        self.advance()
        return Variable(token.text, token.pos)

    def at_namespaced_call(self) -> bool:
        ahead = 0
        while self.peek(ahead).kind == "ident" and self.at_symbol(".", ahead=ahead + 1):
            ahead += 2
        return self.peek(ahead).kind == "ident" and self.at_symbol("(", ahead=ahead + 1)

    def dotted_name(self) -> str:
        parts = [self.advance().text]
        while self.accept_symbol("."):
            parts.append(self.advance().text)
        return ".".join(parts)

    def function_call(self, token: Token) -> Expr:
        name = token.text.lower()
        if name in _PATH_FUNCTIONS:
            raise unsupported(
                token.pos,
                "pattern",
                token.text,
                "use a variable-length relationship with an upper bound",
            )
        if name == "reduce":
            raise unsupported(token.pos, "expression", "reduce(...)", "use UNWIND with sum()")
        in_form = self.peek(2).kind in ("ident", "escaped") and self.is_word(self.peek(3), "IN")
        if name in _REMOVED_LIST_FUNCTIONS and in_form:
            raise unsupported(
                token.pos,
                "expression",
                f"{name}(... IN ...)",
                "use a list comprehension: [x IN list WHERE ... | ...]",
            )
        if name in _QUANTIFIERS and in_form:
            return self.quantifier(token, _QUANTIFIERS[_QUANTIFIERS.index(name)])
        self.advance()
        self.expect_symbol("(")
        if name == "count" and self.accept_symbol("*"):
            self.expect_symbol(")")
            return CountStar(token.pos)
        distinct = self.accept_word("DISTINCT")
        args: list[Expr] = []
        if not self.at_symbol(")"):
            args.append(self.expression())
            while self.accept_symbol(","):
                args.append(self.expression())
        self.expect_symbol(")")
        return FunctionCall(name, tuple(args), distinct, token.pos)

    def case(self) -> Case:
        start = self.expect_word("CASE")
        subject = None if self.at_word("WHEN") else self.expression()
        whens: list[tuple[Expr, Expr]] = []
        while self.accept_word("WHEN"):
            condition = self.expression()
            self.expect_word("THEN")
            whens.append((condition, self.expression()))
        if not whens:
            raise self.fail("WHEN")
        default = self.expression() if self.accept_word("ELSE") else None
        self.expect_word("END")
        return Case(subject, tuple(whens), default, start.pos)

    def quantifier(self, token: Token, kind: QuantifierKind) -> Quantifier:
        self.advance()
        self.expect_symbol("(")
        variable = self.variable_name()
        self.expect_word("IN")
        source = self.expression()
        self.expect_word("WHERE")
        where = self.expression()
        self.expect_symbol(")")
        return Quantifier(kind, variable, source, where, token.pos)

    def comprehension(self, start: Token) -> ListComprehension:
        variable = self.variable_name()
        self.expect_word("IN")
        source = self.expression()
        where = self.expression() if self.accept_word("WHERE") else None
        project = self.expression() if self.accept_symbol("|") else None
        self.expect_symbol("]")
        return ListComprehension(variable, source, where, project, start.pos)

    def subscript(self, subject: Expr) -> Expr:
        start = self.expect_symbol("[")
        lower = None if self.at_symbol("..") else self.expression()
        if self.accept_symbol(".."):
            upper = None if self.at_symbol("]") else self.expression()
            self.expect_symbol("]")
            return Slice(subject, lower, upper, start.pos)
        self.expect_symbol("]")
        assert lower is not None
        return Subscript(subject, lower, start.pos)

    def list_literal(self) -> Expr:
        start = self.expect_symbol("[")
        if self.at_variable() and self.is_word(self.peek(1), "IN"):
            return self.nested(lambda: self.comprehension(start))
        if self.at_symbol("(") and self.try_pattern() is not None:
            raise unsupported(
                start.pos,
                "expression",
                "pattern comprehensions",
                "use OPTIONAL MATCH with collect()",
            )
        items: list[Expr] = []
        if not self.at_symbol("]"):
            items.append(self.expression())
            while self.accept_symbol(","):
                items.append(self.expression())
        self.expect_symbol("]")
        return ListExpr(tuple(items), start.pos)

    def map_literal(self) -> MapExpr:
        start = self.expect_symbol("{")
        entries: list[tuple[str, Expr]] = []
        seen: set[str] = set()
        if not self.at_symbol("}"):
            while True:
                key_token = self.peek()
                if self.at_symbol("."):
                    raise unsupported(
                        key_token.pos,
                        "expression",
                        "map projections",
                        "return the properties one by one: n.key AS key",
                    )
                key = self.name("a map key")
                if key in seen:
                    raise syntax_error(key_token.pos, f"duplicate map key {key!r}", found=key)
                seen.add(key)
                self.expect_symbol(":")
                entries.append((key, self.expression()))
                if not self.accept_symbol(","):
                    break
        self.expect_symbol("}")
        return MapExpr(tuple(entries), start.pos)

    def try_pattern(self) -> PatternPart | None:
        """A relationship pattern starting at ``(``, else ``None`` and no token consumed.

        ``(a)`` alone is a parenthesized expression; a pattern needs a
        relationship after the first node.
        """
        mark = self.at
        try:
            self.node_pattern()
        except GraphCliError as exc:
            if exc.code != "query.syntax":
                raise
            self.at = mark
            return None
        is_pattern = self.at_relationship()
        self.at = mark
        if not is_pattern:
            return None
        return self.nested(lambda: self.chain(self.peek().pos))


def _unbounded(pos: Pos) -> GraphCliError:
    return unsupported(
        pos,
        "pattern",
        "variable length without an upper bound",
        f"give an upper bound of at most {MAX_HOPS}: *1..3",
    )


def _or_list(items: tuple[str, ...]) -> str:
    quoted = [item if item[0].isalpha() else f"'{item}'" for item in items]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def parse(text: str) -> Query:
    """The syntax tree of one read query (``query.syntax``, ``query.unsupported``)."""
    return _Parser(text).query()
