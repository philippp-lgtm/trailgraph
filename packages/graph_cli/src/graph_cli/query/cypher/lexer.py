"""Tokens of the Cypher subset.

Keywords are not a token kind: Cypher keywords are case-insensitive and many
may also be names (``n.count``, ``:Order``), so the parser decides by
context. A backticked name is kind ``escaped`` and is never a keyword.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.query.cypher.ast import Pos
from graph_cli.query.cypher.errors import syntax_error

TokenKind = Literal["ident", "escaped", "string", "integer", "float", "param", "symbol", "eof"]

MAX_INTEGER: Final = 2**63 - 1
# Longest first, so that ``<=`` is not read as ``<`` and ``=``.
SYMBOLS: Final = (
    "..",
    "<>",
    "<=",
    ">=",
    "=~",
    "+=",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ".",
    ":",
    ";",
    "|",
    "=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    "^",
    "&",
    "!",
)
_ESCAPES: Final = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


@dataclass(frozen=True, slots=True)
class Token:
    """``text`` is the decoded value for strings, names and parameters, else the source."""

    kind: TokenKind
    text: str
    pos: Pos
    end: int  # offset after the token

    def describe(self) -> str:
        if self.kind == "eof":
            return "end of query"
        if self.kind == "string":
            return "a string"
        if self.kind == "param":
            return f"${self.text}"
        if self.kind == "escaped":
            return f"`{self.text}`"
        return self.text


def _is_name_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def _is_name_part(char: str) -> bool:
    return char.isalnum() or char == "_"


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0
        self.line_starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]

    def pos(self, offset: int) -> Pos:
        line = bisect.bisect_right(self.line_starts, offset)
        return Pos(line, offset - self.line_starts[line - 1] + 1, offset)

    def peek(self, ahead: int = 0) -> str:
        index = self.at + ahead
        return self.text[index] if index < len(self.text) else ""

    def tokens(self) -> list[Token]:
        found: list[Token] = []
        while True:
            self.skip_space()
            if self.at >= len(self.text):
                found.append(Token("eof", "", self.pos(self.at), self.at))
                return found
            found.append(self.token())

    def skip_space(self) -> None:
        while self.at < len(self.text):
            char = self.peek()
            if char.isspace():
                self.at += 1
            elif char == "/" and self.peek(1) == "/":
                end = self.text.find("\n", self.at)
                self.at = len(self.text) if end < 0 else end
            elif char == "/" and self.peek(1) == "*":
                end = self.text.find("*/", self.at + 2)
                if end < 0:
                    raise syntax_error(self.pos(self.at), "unterminated comment", found="/*")
                self.at = end + 2
            else:
                return

    def token(self) -> Token:
        start = self.at
        char = self.peek()
        if _is_name_start(char):
            return self.name(start, "ident")
        if char == "`":
            return self.escaped(start, "escaped")
        if char.isdigit() or (char == "." and self.peek(1).isdigit()):
            return self.number(start)
        if char in "'\"":
            return self.string(start, char)
        if char == "$":
            self.at += 1
            if _is_name_start(self.peek()) or self.peek().isdigit():
                name = self.name(self.at, "param")
                return Token("param", name.text, self.pos(start), name.end)
            if self.peek() == "`":
                name = self.escaped(self.at, "param")
                return Token("param", name.text, self.pos(start), name.end)
            raise syntax_error(self.pos(start), "expected a parameter name after $", found="$")
        for symbol in SYMBOLS:
            if self.text.startswith(symbol, start):
                self.at += len(symbol)
                return Token("symbol", symbol, self.pos(start), self.at)
        raise syntax_error(self.pos(start), f"unexpected character {char!r}", found=char)

    def name(self, start: int, kind: TokenKind) -> Token:
        self.at = start
        while _is_name_part(self.peek()):
            self.at += 1
        return Token(kind, self.text[start : self.at], self.pos(start), self.at)

    def escaped(self, start: int, kind: TokenKind) -> Token:
        parts: list[str] = []
        self.at = start + 1
        while True:
            end = self.text.find("`", self.at)
            if end < 0:
                raise syntax_error(self.pos(start), "unterminated `name`", found="`")
            parts.append(self.text[self.at : end])
            self.at = end + 1
            if self.peek() != "`":  # a doubled backtick is a literal backtick
                break
            parts.append("`")
            self.at += 1
        text = "".join(parts)
        if not text:
            raise syntax_error(self.pos(start), "empty `name`", found="``")
        return Token(kind, text, self.pos(start), self.at)

    def number(self, start: int) -> Token:
        while self.peek().isdigit():
            self.at += 1
        is_float = False
        if self.peek() == "." and self.peek(1).isdigit():
            is_float = True
            self.at += 1
            while self.peek().isdigit():
                self.at += 1
        if self.peek() in ("e", "E") and (
            self.peek(1).isdigit() or (self.peek(1) in "+-" and self.peek(2).isdigit())
        ):
            is_float = True
            self.at += 2
            while self.peek().isdigit():
                self.at += 1
        text = self.text[start : self.at]
        if _is_name_part(self.peek()):
            raise syntax_error(
                self.pos(start), f"invalid number {text + self.peek()!r}", found=text
            )
        if not is_float and int(text) > MAX_INTEGER:
            raise syntax_error(self.pos(start), f"integer {text} is out of range", found=text)
        return Token("float" if is_float else "integer", text, self.pos(start), self.at)

    def string(self, start: int, quote: str) -> Token:
        parts: list[str] = []
        self.at = start + 1
        while True:
            char = self.peek()
            if not char:
                raise syntax_error(self.pos(start), "unterminated string", found=quote)
            self.at += 1
            if char == quote:
                return Token("string", "".join(parts), self.pos(start), self.at)
            if char != "\\":
                parts.append(char)
                continue
            parts.append(self.escape())

    def escape(self) -> str:
        at = self.at - 1
        char = self.peek()
        self.at += 1
        if char in _ESCAPES:
            return _ESCAPES[char]
        width = {"u": 4, "U": 8}.get(char)
        digits = self.text[self.at : self.at + width] if width else ""
        if width and len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
            self.at += width
            code = int(digits, 16)
            if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
                return chr(code)
        raise syntax_error(self.pos(at), f"invalid escape \\{char}", found=f"\\{char}")


def tokenize(text: str) -> list[Token]:
    """All tokens of ``text``, ending with one ``eof`` token."""
    return _Lexer(text).tokens()
