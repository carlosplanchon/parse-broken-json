"""Parse broken or truncated JSON strings, recovering as much as possible.

Valid JSON is parsed with :mod:`json`. Anything else goes through a small
recursive-descent parser that never raises: it tolerates the usual deviations
found in LLM output, repairs strings with missing or unescaped quotes and
unquoted values with bounded guesses, and, on truncated input, keeps every
complete key/value pair, every nested container and a trailing unterminated
string.

The parser is written as a set of generators. Whenever a decision depends on
input that has not arrived yet, it suspends until more text is fed or the end
of the input is declared. That gives an incremental :class:`StreamParser`
whose total cost is linear in the stream, and a ``stream_stable`` mode whose
values are final by construction: they were decided without looking past the
end of the available input.

Public API:

* :func:`parse_broken_json` returns the recovered value, or ``default``.
* :func:`parse_broken_json_result` returns a :class:`ParseResult` with the
  value plus ``found``, ``valid``, ``complete`` and the list of repairs.
* :class:`StreamParser` parses a stream chunk by chunk.
* :func:`loads`, :func:`load` and :func:`from_file` mirror the :mod:`json`
  module.
* :class:`BrokenJSONError` is raised in strict mode.
"""

from __future__ import annotations

import codecs
import json
import re
from bisect import bisect_left
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import IO, Any, Literal

__version__ = "0.4.0"

__all__ = [
    "BrokenJSONError",
    "ParseResult",
    "Repair",
    "StreamParser",
    "__version__",
    "from_file",
    "load",
    "loads",
    "parse_broken_json",
    "parse_broken_json_result",
    "remove_leading_unwanted_text",
]

Multiple = Literal["auto", "all", "first", "longest"]
_MULTIPLE_MODES = ("auto", "all", "first", "longest")

AllowedKeys = Iterable[str] | Mapping[str, Any] | None
_Allowed = frozenset[str] | dict[str, Any] | None
_Hook = Callable[[Any], Any] | None
_Wait = Generator[None, None, None]
_Parsed = Generator[None, None, tuple[Any, bool]]

# Sentinel for "nothing usable could be recovered here".
_MISSING = object()

# Nesting deeper than this is treated as truncation. It keeps the recursive
# parser well inside the interpreter's default recursion limit.
_MAX_DEPTH = 200

_LDQ = "\N{LEFT DOUBLE QUOTATION MARK}"
_RDQ = "\N{RIGHT DOUBLE QUOTATION MARK}"
_LSQ = "\N{LEFT SINGLE QUOTATION MARK}"
_RSQ = "\N{RIGHT SINGLE QUOTATION MARK}"
_CURLY = _LDQ + _RDQ + _LSQ + _RSQ
_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"

# Opening quote -> characters that may close it.
_CLOSERS = {
    '"': '"',
    "'": "'",
    _LDQ: _LDQ + _RDQ,
    _RDQ: _LDQ + _RDQ,
    _LSQ: _LSQ + _RSQ,
    _RSQ: _LSQ + _RSQ,
}
_QUOTES = frozenset(_CLOSERS)
# What may legitimately follow a closing quote.
_AFTER_QUOTE = frozenset(",}]:\r\n")
# Characters that end a bare value and bound the search for a later quote.
_SEGMENT_END = frozenset(",}]\r\n")
_DELIM_RE = re.compile(r"[,}\]\r\n]")
# Structural characters: whatever follows one of them cannot change the
# outcome of the "missing closing quote" lookahead.
_STRUCT_RE = re.compile(r"[,}\]:{\[\r\n]")
# What may legitimately follow a number: whitespace, a delimiter, a comment
# or a quote (a missing comma before a string).
_AFTER_NUMBER = frozenset(" \t\r\n,}]/") | _QUOTES
# Characters a number or a literal may be made of.
_WORD_EXTRA = frozenset("._+-")

_NUMBER_RE = re.compile(
    r"[+-]?0[xX][0-9a-fA-F]+"  # hexadecimal
    r"|[+-]?(?=\.?[0-9])(?:[0-9][0-9_]*)?(?:\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?"
)
_STRICT_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LEADING_ZEROS_RE = re.compile(r"^(-?)0+(?=[0-9])")
_CANDIDATE_RE = re.compile(r"[\[{]")
# An unquoted key runs until whitespace, a structural character or a quote.
_BARE_KEY_RE = re.compile(r"[^\s:,{}\[\]\"'" + _CURLY + r"]+")
# A quoted key followed by a colon: what comes right after a comma or newline
# inside a string whose closing quote is missing.
_KEY_AHEAD_RE = re.compile(
    r"\s*[\"'"
    + _LDQ
    + _LSQ
    + r"]"
    + r"[^\"'"
    + _CURLY
    + r":,{}\[\]\r\n]*"
    + r"[\"'"
    + _RDQ
    + _RSQ
    + r"]\s*:"
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
# (word, value, standard). "Standard" means json.loads accepts it too.
_LITERALS = (
    ("true", True, True),
    ("false", False, True),
    ("null", None, True),
    ("True", True, False),
    ("False", False, False),
    ("None", None, False),
    ("undefined", None, False),
    ("NaN", float("nan"), True),
    ("Infinity", float("inf"), True),
)
_SIGNED_INFINITY = (("-Infinity", float("-inf")), ("+Infinity", float("inf")))


@dataclass(frozen=True)
class Repair:
    """One deviation from strict JSON that the parser worked around.

    ``position`` is a zero-based offset into the input text.
    """

    position: int
    message: str


@dataclass(frozen=True)
class ParseResult:
    """Outcome of :func:`parse_broken_json_result` or :meth:`StreamParser.finish`.

    * ``value``: the recovered value, ``None`` when ``found`` is ``False``.
    * ``found``: whether any JSON value could be recovered at all. This is
      how a recovered ``null`` is told apart from a failure.
    * ``valid``: whether the text was valid JSON and no repair was needed.
    * ``complete``: ``False`` when the value was cut short by the end of the
      input or by text the parser could not make sense of.
    * ``start`` / ``end``: offsets of the value inside the text.
    * ``repairs``: every deviation the parser worked around, in order.
    """

    value: Any
    found: bool
    valid: bool
    complete: bool
    start: int
    end: int
    repairs: tuple[Repair, ...] = ()


class BrokenJSONError(ValueError):
    """Raised in strict mode when the input is not clean JSON."""

    def __init__(self, message: str, position: int, repairs: tuple[Repair, ...] = ()) -> None:
        super().__init__(f"{message} at position {position}")
        self.message = message
        self.position = position
        self.repairs = repairs


class _DuplicateKey(Exception):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise _DuplicateKey
    return dict(pairs)


def _is_word_char(s: str, i: int) -> bool:
    return i < len(s) and (s[i].isalnum() or s[i] == "_")


class _Buffer:
    """The input so far, plus lookup tables that grow with it.

    The tables make "where is the next delimiter" a bisection instead of a
    scan. Without them, a text with many candidate starts that lead nowhere
    makes every candidate rescan to the end of the input, which is quadratic.
    """

    __slots__ = (
        "_delims",
        "_delims_upto",
        "_no_block_end_from",
        "_no_newline_from",
        "_structs",
        "_structs_upto",
        "eof",
        "n",
        "s",
    )

    def __init__(self, text: str = "", *, eof: bool = False) -> None:
        self.s = text
        self.n = len(text)
        self.eof = eof
        self._delims: list[int] = []
        self._delims_upto = 0
        self._structs: list[int] = []
        self._structs_upto = 0
        # s[p:n] is known to hold no newline / no "*/" for p at or past these.
        self._no_newline_from = self.n
        self._no_block_end_from = self.n

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self.s += chunk
        self.n = len(self.s)
        self._no_newline_from = self.n
        self._no_block_end_from = self.n

    def _next(self, regex: re.Pattern[str], positions: list[int], upto: int, i: int) -> int:
        if upto < self.n:
            positions.extend(match.start() for match in regex.finditer(self.s, upto))
        k = bisect_left(positions, i)
        return positions[k] if k < len(positions) else self.n

    def next_delimiter(self, i: int) -> int:
        """Index of the first ``, } ] \\r \\n`` at or after ``i``, else ``n``."""
        result = self._next(_DELIM_RE, self._delims, self._delims_upto, i)
        self._delims_upto = self.n
        return result

    def next_structural(self, i: int) -> int:
        """Index of the first ``, } ] : { [ \\r \\n`` at or after ``i``, else ``n``."""
        result = self._next(_STRUCT_RE, self._structs, self._structs_upto, i)
        self._structs_upto = self.n
        return result

    def find_newline(self, i: int) -> int:
        if i >= self._no_newline_from:
            return -1
        j = self.s.find("\n", i, self._no_newline_from)
        if j == -1:
            self._no_newline_from = i
        return j

    def find_block_end(self, i: int) -> int:
        if i >= self._no_block_end_from:
            return -1
        j = self.s.find("*/", i, min(self.n, self._no_block_end_from + 1))
        if j == -1:
            self._no_block_end_from = i
        return j


class _PartialParser:
    """Recursive-descent JSON parser that tolerates broken input.

    Every ``_parse_*`` method is a generator that returns ``(value,
    complete)``. ``complete`` is ``False`` when the input ended, or stopped
    making sense, inside that value. ``value`` then holds whatever was
    recovered so far, or ``_MISSING`` when nothing was. The generator yields
    whenever a decision needs input that has not arrived yet; the driver
    resumes it once more text was appended to the buffer or its end was
    declared. Deviations from strict JSON are recorded in ``repairs``;
    truncation is not, the caller records it once.
    """

    def __init__(
        self,
        buffer: _Buffer,
        start: int,
        *,
        parse_float: _Hook = None,
        parse_int: _Hook = None,
        parse_constant: _Hook = None,
    ) -> None:
        self.b = buffer
        self.i = start
        self.stack: list[str] = []  # enclosing containers, innermost last
        self.repairs: list[Repair] = []
        self.too_deep = False
        self.root: Any = None  # the outermost container, built in place
        self.parse_float = parse_float
        self.parse_int = parse_int
        self.parse_constant = parse_constant

    def _log(self, position: int, message: str) -> None:
        self.repairs.append(Repair(position, message))

    def _peek(self) -> str:
        return self.b.s[self.i] if self.i < self.b.n else ""

    # --- waiting for input --------------------------------------------------

    def _need(self, count: int) -> _Wait:
        """Wait until ``count`` characters are available from ``i``, or the end."""
        while self.i + count > self.b.n and not self.b.eof:
            yield

    def _need_at(self, pos: int) -> _Wait:
        while pos >= self.b.n and not self.b.eof:
            yield

    def _need_delimiter(self, pos: int) -> _Wait:
        while self.b.next_delimiter(pos) >= self.b.n and not self.b.eof:
            yield

    def _need_structural(self, pos: int) -> _Wait:
        while self.b.next_structural(pos) >= self.b.n and not self.b.eof:
            yield

    def _need_blank_end(self, pos: int) -> Generator[None, None, int]:
        """Index of the first character past spaces and tabs from ``pos``."""
        while True:
            s, n = self.b.s, self.b.n
            j = pos
            while j < n and s[j] in " \t":
                j += 1
            if j < n or self.b.eof:
                return j
            yield

    def _need_space_end(self, pos: int) -> Generator[None, None, int]:
        """Index of the first non-whitespace character from ``pos``."""
        while True:
            s, n = self.b.s, self.b.n
            j = pos
            while j < n and s[j].isspace():
                j += 1
            if j < n or self.b.eof:
                return j
            yield

    def _need_word_end(self, pos: int) -> _Wait:
        """Wait until the number or literal starting at ``pos`` can be delimited."""
        while True:
            s, n = self.b.s, self.b.n
            j = pos
            while j < n and (s[j].isalnum() or s[j] in _WORD_EXTRA):
                j += 1
            if j < n or self.b.eof:
                return
            yield

    def _need_closer_or_delimiter(self, pos: int, closers: str) -> _Wait:
        while True:
            s, n = self.b.s, self.b.n
            if (
                self.b.next_delimiter(pos) < n
                or any(s.find(ch, pos) != -1 for ch in closers)
                or self.b.eof
            ):
                return
            yield

    def _need_bare_key_end(self, pos: int) -> Generator[None, None, re.Match[str] | None]:
        while True:
            match = _BARE_KEY_RE.match(self.b.s, pos)
            if match is None or match.end() < self.b.n or self.b.eof:
                return match
            yield

    # --- lexical helpers ----------------------------------------------------

    def _skip_ws(self) -> _Wait:
        """Skip whitespace and ``//`` or ``/* */`` comments."""
        while True:
            i = yield from self._need_space_end(self.i)
            self.i = i
            if i >= self.b.n or self.b.s[i] != "/":
                return
            yield from self._need(2)
            s = self.b.s
            if i + 1 >= self.b.n:
                return  # a lone slash at the very end
            if s[i + 1] == "/":
                self._log(i, "comment")
                search_from = i + 2
                while True:
                    j = self.b.find_newline(search_from)
                    if j != -1:
                        self.i = j + 1
                        break
                    if self.b.eof:
                        self.i = self.b.n
                        break
                    search_from = self.b.n
                    yield
            elif s[i + 1] == "*":
                self._log(i, "comment")
                search_from = i + 2
                while True:
                    j = self.b.find_block_end(search_from)
                    if j != -1:
                        self.i = j + 2
                        break
                    if self.b.eof:
                        self.i = self.b.n
                        break
                    search_from = max(search_from, self.b.n - 1)
                    yield
            else:
                return

    def _at_value_start(self) -> bool:
        c = self._peek()
        return bool(c) and (c in "{[+-." or c in _CLOSERS or c.isalnum())

    def _at_key_start(self) -> bool:
        c = self._peek()
        return bool(c) and (c in _CLOSERS or _BARE_KEY_RE.match(self.b.s, self.i) is not None)

    def _constant(self, word: str, value: float) -> Any:
        if self.parse_constant is not None:
            return self.parse_constant(word)
        return value

    # --- grammar ------------------------------------------------------------

    def parse_value(self) -> _Parsed:
        yield from self._need(1)
        c = self._peek()
        if c == "{":
            return (yield from self._parse_object())
        if c == "[":
            return (yield from self._parse_array())
        if c in _CLOSERS:
            return (yield from self._parse_string())
        if c in ("-", "+", ".") or "0" <= c <= "9":
            value, complete = yield from self._parse_number()
            if value is not _MISSING:
                return value, complete
        elif c.isalpha():
            value, complete = yield from self._parse_literal()
            if value is not _MISSING:
                return value, complete
        return (yield from self._parse_bare())

    def _parse_object(self) -> _Parsed:
        opened = self.i
        self.i += 1  # opening brace
        result: dict[str, Any] = {}
        if self.root is None:
            self.root = result
        if len(self.stack) >= _MAX_DEPTH:
            self._log(opened, "nesting too deep")
            self.too_deep = True
            return result, False
        self.stack.append("object")
        try:
            seen_member = False
            after_comma = False
            while True:
                yield from self._skip_ws()
                yield from self._need(1)
                c = self._peek()
                if c == "}":
                    if after_comma:
                        self._log(self.i, "trailing comma")
                    self.i += 1
                    return result, True
                if c == ",":
                    if after_comma or not seen_member:
                        self._log(self.i, "extra comma")
                    self.i += 1
                    after_comma = True
                    continue
                if c == "":
                    return result, False
                if seen_member and not after_comma:
                    if not self._at_key_start():
                        self._log(self.i, "unexpected text inside object")
                        return result, False
                    self._log(self.i, "missing comma")
                key_at = self.i
                if c in _CLOSERS:
                    key, complete = yield from self._parse_string()
                    if not complete:
                        return result, False  # key cut by the end of input
                    quoted = True
                else:
                    match = yield from self._need_bare_key_end(self.i)
                    if match is None:
                        self._log(self.i, "unexpected text inside object")
                        return result, False
                    key = match.group()
                    self.i = match.end()
                    quoted = False
                    self._log(key_at, "unquoted key")
                yield from self._skip_ws()
                yield from self._need(1)
                c = self._peek()
                if c == ":":
                    self.i += 1
                    yield from self._skip_ws()
                    yield from self._need(1)
                    c = self._peek()
                elif c in (",", "}"):
                    pass
                elif quoted and self._at_value_start():
                    self._log(self.i, "missing colon")
                elif c == "":
                    return result, False
                else:
                    # A bare word not followed by a colon is not a key.
                    self._log(self.i, "unexpected text after key")
                    return result, False
                seen_member = True
                after_comma = False
                if c in (",", "}"):
                    self._log(key_at, "key without value dropped")
                    continue
                value, complete = yield from self.parse_value()
                if value is not _MISSING:
                    if key in result:
                        self._log(key_at, "duplicate key, last one wins")
                    result[key] = value
                if not complete:
                    return result, False
        finally:
            self.stack.pop()

    def _parse_array(self) -> _Parsed:
        opened = self.i
        self.i += 1  # opening bracket
        result: list[Any] = []
        if self.root is None:
            self.root = result
        if len(self.stack) >= _MAX_DEPTH:
            self._log(opened, "nesting too deep")
            self.too_deep = True
            return result, False
        self.stack.append("array")
        try:
            seen_item = False
            after_comma = False
            comma_at = -1
            while True:
                yield from self._skip_ws()
                yield from self._need(1)
                c = self._peek()
                if c == "]":
                    if after_comma:
                        self._log(self.i, "trailing comma")
                    self.i += 1
                    return result, True
                if c == ",":
                    if after_comma or not seen_item:
                        self._log(self.i, "extra comma")
                    comma_at = self.i
                    self.i += 1
                    after_comma = True
                    continue
                if c == "":
                    return result, False
                if seen_item and not after_comma:
                    if not self._at_value_start():
                        self._log(self.i, "unexpected text inside array")
                        return result, False
                    self._log(self.i, "missing comma")
                at = self.i
                value, complete = yield from self.parse_value()
                if (
                    complete
                    and c in _CLOSERS
                    and len(self.stack) > 1
                    and self.stack[-2] == "object"
                ):
                    yield from self._skip_ws()
                    yield from self._need(1)
                    if self._peek() == ":":
                        # A string followed by a colon is a key of the enclosing
                        # object: this array was never closed. Hand the key back.
                        self._log(at, "array closed before key")
                        self.i = comma_at if after_comma else at
                        return result, True
                if value is not _MISSING:
                    result.append(value)
                seen_item = True
                after_comma = False
                if not complete:
                    return result, False
        finally:
            self.stack.pop()

    def _unterminated(self, chunks: list[str]) -> tuple[Any, bool]:
        """Finish a string cut by the end of input."""
        self.i = self.b.n
        return "".join(chunks), False

    def _ensure_quote_decidable(self, i: int, closers: str) -> _Wait:
        """Wait until :meth:`_quote_closes` can decide about the quote at ``i``."""
        j = yield from self._need_blank_end(i + 1)
        s, n = self.b.s, self.b.n
        if j >= n or s[j] in _AFTER_QUOTE:
            return
        if s[j] in _QUOTES:
            yield from self._need_blank_end(j + 1)
            return
        yield from self._need_closer_or_delimiter(j, closers)

    def _parse_string(self) -> _Parsed:
        b = self.b
        opened = self.i
        closers = _CLOSERS[b.s[opened]]
        if b.s[opened] != '"':
            self._log(opened, "non-standard quotes")
        i = opened + 1
        chunks: list[str] = []
        start = i
        control_logged = False
        while True:
            s, n = b.s, b.n
            while i < n:
                c = s[i]
                if c in closers:
                    yield from self._ensure_quote_decidable(i, closers)
                    s, n = b.s, b.n
                    if self._quote_closes(i, closers):
                        chunks.append(s[start:i])
                        self.i = i + 1
                        return "".join(chunks), True
                    self._log(i, "unescaped quote inside string")
                    i += 1
                    continue
                if c in ",\r\n":
                    yield from self._need_structural(i + 1)
                    s, n = b.s, b.n
                    if _KEY_AHEAD_RE.match(s, i + 1):
                        # The closing quote is missing: what follows is the next key.
                        self._log(i, "missing closing quote")
                        chunks.append(s[start:i].rstrip())
                        self.i = i
                        return "".join(chunks), True
                if c < " ":
                    if not control_logged:
                        self._log(i, "control character inside string")
                        control_logged = True
                    i += 1
                    continue
                if c != "\\":
                    i += 1
                    continue
                chunks.append(s[start:i])
                yield from self._need_at(i + 1)
                s, n = b.s, b.n
                if i + 1 >= n:
                    return self._unterminated(chunks)  # ends inside an escape
                e = s[i + 1]
                if e != "u":
                    if e not in _ESCAPES:
                        self._log(i, "unknown escape kept")
                    # Unknown escapes are kept literally.
                    chunks.append(_ESCAPES.get(e, "\\" + e))
                    i += 2
                    start = i
                    continue
                yield from self._need_at(i + 5)
                s, n = b.s, b.n
                if i + 6 <= n and set(s[i + 2 : i + 6]) <= _HEX_DIGITS:
                    if 0xD800 <= int(s[i + 2 : i + 6], 16) <= 0xDBFF:
                        yield from self._need_at(i + 11)  # room for the low surrogate
                        s, n = b.s, b.n
                decoded, i = self._parse_unicode_escape(i)
                if decoded is None:
                    return self._unterminated(chunks)  # ends inside the escape
                chunks.append(decoded)
                start = i
            if b.eof:
                break
            yield  # the buffer ends inside the string
        s, n = b.s, b.n
        tail = s[start:n]
        if tail.endswith(("}", "]")):
            # More likely a missing closing quote than a truncated string.
            self._log(n - 1, "missing closing quote")
            chunks.append(tail[:-1].rstrip())
            self.i = n - 1
            return "".join(chunks), True
        chunks.append(tail)
        return self._unterminated(chunks)

    def _quote_closes(self, i: int, closers: str) -> bool:
        """Decide whether the quote at ``i`` ends the string or is content."""
        s, n = self.b.s, self.b.n
        j = i + 1
        while j < n and s[j] in " \t":
            j += 1
        if j >= n or s[j] in _AFTER_QUOTE:
            return True
        if s[j] in _QUOTES:
            # Two quotes in a row. If a delimiter follows the second one, this
            # is a doubled closing quote and the first one is content. Otherwise
            # a comma is missing between two strings and the first one closes.
            k = j + 1
            while k < n and s[k] in " \t":
                k += 1
            return not (k >= n or s[k] in _AFTER_QUOTE)
        # Something else follows. The quote is content only if the string can
        # still be closed later within this segment.
        end = self.b.next_delimiter(j)
        return all(s.find(ch, j, end) == -1 for ch in closers)

    def _parse_unicode_escape(self, i: int) -> tuple[str | None, int]:
        """Decode a ``\\uXXXX`` escape whose backslash sits at ``i``.

        Returns ``(text, next_index)``. ``text`` is ``None`` when the input
        ends inside the escape (or inside the low half of a surrogate pair).
        """
        s, n = self.b.s, self.b.n
        hexs = s[i + 2 : i + 6]
        if len(hexs) < 4:
            return None, n
        if not set(hexs) <= _HEX_DIGITS:
            self._log(i, "invalid unicode escape kept")
            return "\\u", i + 2
        code = int(hexs, 16)
        i += 6
        if 0xD800 <= code <= 0xDBFF and s.startswith("\\u", i):
            low_hex = s[i + 2 : i + 6]
            if len(low_hex) < 4:
                return None, n
            if set(low_hex) <= _HEX_DIGITS:
                low = int(low_hex, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    i += 6
        return chr(code), i

    def _parse_number(self) -> _Parsed:
        yield from self._need_word_end(self.i)
        s, n, i = self.b.s, self.b.n, self.i
        for word, value in _SIGNED_INFINITY:
            if s.startswith(word, i) and not _is_word_char(s, i + len(word)):
                self.i = i + len(word)
                return self._constant(word.lstrip("+"), value), self.i < n
        match = _NUMBER_RE.match(s, i)
        if match is None:
            return _MISSING, False
        end = match.end()
        if end < n and s[end] not in _AFTER_NUMBER:
            # Not a clean number ("1.2.3", "12:30", "12ab"): let the bare-value
            # fallback have it.
            return _MISSING, False
        text = match.group()
        try:
            number = self._number(text)
        except ValueError:
            return _MISSING, False
        if _STRICT_NUMBER_RE.fullmatch(text) is None:
            self._log(i, "non-standard number")
        self.i = end
        return number, end < n

    def _number(self, text: str) -> Any:
        """Convert a lenient number literal; hooks receive its normalized form."""
        if "x" in text or "X" in text:
            value = int(text, 16)
            return self.parse_int(str(value)) if self.parse_int is not None else value
        clean = _LEADING_ZEROS_RE.sub(r"\1", text.replace("_", "").lstrip("+"))
        if clean.startswith(".") or clean.startswith("-."):
            clean = clean.replace(".", "0.", 1)
        if clean.endswith("."):
            clean += "0"
        if any(ch in clean for ch in ".eE"):
            return self.parse_float(clean) if self.parse_float is not None else float(clean)
        return self.parse_int(clean) if self.parse_int is not None else int(clean)

    def _parse_literal(self) -> _Parsed:
        yield from self._need_word_end(self.i)
        s, i = self.b.s, self.i
        for word, value, standard in _LITERALS:
            if s.startswith(word, i) and not _is_word_char(s, i + len(word)):
                if not standard:
                    self._log(i, "non-standard literal")
                self.i = i + len(word)
                if isinstance(value, float):
                    return self._constant(word, value), True
                return value, True
        return _MISSING, False

    def _parse_bare(self) -> _Parsed:
        """Read an unquoted value up to the next delimiter.

        A bare word cut by the end of input is dropped: it is far more likely
        a truncated literal or number than a truncated unquoted string.
        """
        i = self.i
        yield from self._need_delimiter(i)
        j = self.b.next_delimiter(i)
        if j >= self.b.n:
            self.i = self.b.n  # cut by the end of input
            return _MISSING, False
        text = self.b.s[i:j].strip()
        if not text:
            self._log(i, "unexpected text")
            return _MISSING, False
        self._log(i, "unquoted value")
        self.i = j
        return text, True


def _usable(value: Any, complete: bool) -> bool:
    if value is _MISSING:
        return False
    if complete:
        return True
    return bool(value)  # a truncated container with nothing inside is worthless


@dataclass
class _Found:
    start: int
    end: int
    value: Any
    complete: bool
    repairs: list[Repair]


class _Scan:
    """Drive candidate parses over a growing buffer.

    Every ``{`` or ``[`` is a candidate start. Each candidate is parsed as far
    as it goes; candidates nested inside an accepted span are skipped.
    """

    def __init__(self, buffer: _Buffer, *, multiple: str, **number_hooks: _Hook) -> None:
        self.b = buffer
        self.multiple = multiple
        self.number_hooks = number_hooks
        self.found: list[_Found] = []
        self.pos = 0
        self.start = 0
        self.parser: _PartialParser | None = None
        self.gen: _Parsed | None = None
        self.done = False

    def advance(self) -> None:
        """Parse as far as the buffer allows."""
        while not self.done:
            if self.parser is None or self.gen is None:
                match = _CANDIDATE_RE.search(self.b.s, self.pos)
                if match is None:
                    self.pos = self.b.n
                    if self.b.eof:
                        self.done = True
                    return
                self.start = match.start()
                self.parser = _PartialParser(self.b, self.start, **self.number_hooks)
                self.gen = self.parser.parse_value()
            try:
                next(self.gen)
            except StopIteration as stop:
                value, complete = stop.value
                self._finish_candidate(value, complete)
            except RecursionError:
                self._finish_candidate(_MISSING, False)
            else:
                return  # the parser needs more input

    def _finish_candidate(self, value: Any, complete: bool) -> None:
        parser = self.parser
        assert parser is not None
        end = parser.i
        if _usable(value, complete):
            self.found.append(_Found(self.start, end, value, complete, list(parser.repairs)))
            if self.multiple == "first" or parser.too_deep:
                self.done = True  # nothing later matters, or it is all nested
            self.pos = end
        else:
            self.pos = self.start + 1
        self.parser = None
        self.gen = None

    def snapshot(self) -> list[_Found]:
        """The completed candidates plus the one being parsed, if usable."""
        found = list(self.found)
        parser = self.parser
        if parser is not None and parser.root is not None and _usable(parser.root, False):
            found.append(_Found(self.start, self.b.n, parser.root, False, list(parser.repairs)))
        return found


def _assemble(
    text: str, found: list[_Found], multiple: str
) -> tuple[Any, bool, int, int, list[Repair]]:
    """Pick the candidates to return and put the report together.

    * ``"auto"``: several top-level objects come back together as a list
      (NDJSON-like output); otherwise the longest candidate wins.
    * ``"all"``: every top-level value, as a list.
    * ``"first"``: the first value found.
    * ``"longest"``: the candidate covering the longest span.

    Returns ``(value, complete, start, end, repairs)``.
    """
    n = len(text)
    if multiple == "all" or (
        multiple == "auto" and len(found) > 1 and all(isinstance(f.value, dict) for f in found)
    ):
        chosen = found
    elif multiple == "first":
        chosen = found[:1]
    else:
        chosen = [max(found, key=lambda f: f.end - f.start)]
    repairs: list[Repair] = []
    if text[: chosen[0].start].strip():
        repairs.append(Repair(0, "leading text skipped"))
    for prev, cur in pairwise(chosen):
        if text[prev.end : cur.start].strip():
            repairs.append(Repair(prev.end, "text between values ignored"))
    if len(chosen) > 1:
        repairs.append(Repair(chosen[1].start, "multiple values found"))
    for f in chosen:
        repairs.extend(f.repairs)
    end = chosen[-1].end
    if text[end:].strip():
        repairs.append(Repair(end, "trailing text ignored"))
    complete = all(f.complete for f in chosen)
    if not complete and end >= n:
        repairs.append(Repair(n, "input ended inside a value"))
    repairs.sort(key=lambda r: r.position)
    if multiple == "all" or len(chosen) > 1:
        value: Any = [f.value for f in chosen]
    else:
        value = chosen[0].value
    return value, complete, chosen[0].start, end, repairs


def _to_text(data: Any) -> str:
    if hasattr(data, "read"):
        data = data.read()
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode("utf-8", errors="replace")
    if not isinstance(data, str):
        raise TypeError(f"expected str, bytes or a file-like object, got {type(data).__name__}")
    return data.removeprefix(_BOM)


def _normalize_allowed(allowed_keys: AllowedKeys) -> _Allowed:
    if allowed_keys is None:
        return None
    if isinstance(allowed_keys, Mapping):
        shape: dict[str, Any] = {}
        for key, sub in allowed_keys.items():
            if sub is False:
                continue
            shape[str(key)] = None if sub is None or sub is True else _normalize_allowed(sub)
        return shape
    if isinstance(allowed_keys, (str, bytes)):
        raise TypeError("allowed_keys must be an iterable of keys or a mapping, not a string")
    return frozenset(str(key) for key in allowed_keys)


def _filter_keys(value: Any, allowed: _Allowed) -> Any:
    """Copy ``value``, keeping only the allowed keys at every level."""
    if isinstance(value, dict):
        if allowed is None:
            return {k: _filter_keys(v, None) for k, v in value.items()}
        if isinstance(allowed, dict):
            return {k: _filter_keys(v, allowed[k]) for k, v in value.items() if k in allowed}
        return {k: _filter_keys(v, allowed) for k, v in value.items() if k in allowed}
    if isinstance(value, list):
        return [_filter_keys(v, allowed) for v in value]
    return value


def _apply_hooks(value: Any, object_hook: _Hook, object_pairs_hook: _Hook) -> Any:
    """Apply ``json``-style object hooks bottom-up."""
    if object_hook is None and object_pairs_hook is None:
        return value
    if isinstance(value, dict):
        items = [(k, _apply_hooks(v, object_hook, object_pairs_hook)) for k, v in value.items()]
        if object_pairs_hook is not None:
            return object_pairs_hook(items)
        result = dict(items)
        return object_hook(result) if object_hook is not None else result
    if isinstance(value, list):
        return [_apply_hooks(v, object_hook, object_pairs_hook) for v in value]
    return value


class StreamParser:
    """Parse a stream of text chunk by chunk.

    Feed chunks with :meth:`feed`. At any point, :attr:`value` and
    :meth:`snapshot` return what has been decided so far: every value they
    contain was settled without looking past the end of the input received,
    so it never changes when more input arrives. :meth:`finish` declares the
    end of the stream and returns the final result, exactly as
    :func:`parse_broken_json_result` would for the whole text.

    The work done is linear in the stream: the parser suspends where it needs
    more input and resumes from there. Each snapshot copies the current value.

    With ``multiple="auto"`` the shape of the value can change once a second
    top-level object shows up (a dict becomes a list of dicts); use
    ``multiple="all"`` or ``multiple="first"`` when the shape must not change.
    """

    def __init__(
        self,
        allowed_keys: AllowedKeys = None,
        *,
        strict: bool = False,
        multiple: Multiple = "auto",
        object_hook: _Hook = None,
        object_pairs_hook: _Hook = None,
        parse_float: _Hook = None,
        parse_int: _Hook = None,
        parse_constant: _Hook = None,
    ) -> None:
        if multiple not in _MULTIPLE_MODES:
            raise ValueError(f"multiple must be one of {_MULTIPLE_MODES}, got {multiple!r}")
        self._allowed = _normalize_allowed(allowed_keys)
        self._strict = strict
        self._multiple = multiple
        self._object_hook = object_hook
        self._object_pairs_hook = object_pairs_hook
        self._parse_float = parse_float
        self._parse_int = parse_int
        self._parse_constant = parse_constant
        self._buffer = _Buffer()
        self._scan = _Scan(
            self._buffer,
            multiple=multiple,
            parse_float=parse_float,
            parse_int=parse_int,
            parse_constant=parse_constant,
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._first_chunk = True
        self._final: ParseResult | None = None

    @property
    def text(self) -> str:
        """Everything fed so far."""
        return self._buffer.s

    def feed(self, chunk: str | bytes | bytearray | memoryview) -> None:
        """Append a chunk of text, or of UTF-8 bytes, to the stream."""
        if self._final is not None:
            raise ValueError("finish() was already called")
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk = self._decoder.decode(bytes(chunk))
        elif not isinstance(chunk, str):
            raise TypeError(f"expected str or bytes, got {type(chunk).__name__}")
        if self._first_chunk and chunk:
            chunk = chunk.removeprefix(_BOM)
            self._first_chunk = False
        self._buffer.append(chunk)

    @property
    def value(self) -> Any:
        """What has been decided so far, or ``None``."""
        return self.snapshot().value

    def snapshot(self) -> ParseResult:
        """The result so far. Its values are final; ``complete`` is not."""
        if self._final is not None:
            return self._final
        return self._result(eof=False)

    def finish(self) -> ParseResult:
        """Declare the end of the stream and return the final result."""
        if self._final is None:
            tail = self._decoder.decode(b"", final=True)
            self._buffer.append(tail)
            self._buffer.eof = True
            self._final = self._result(eof=True)
        return self._final

    def _finalize(self, value: Any, *, fresh: bool = False) -> Any:
        """Filter and hook ``value``; a fresh value needs no defensive copy."""
        if not fresh or self._allowed is not None:
            value = _filter_keys(value, self._allowed)
        return _apply_hooks(value, self._object_hook, self._object_pairs_hook)

    def _fast_path(self, text: str) -> ParseResult | None:
        """Parse valid JSON with the standard module."""
        hook = _reject_duplicates if self._strict else None
        try:
            value = json.loads(
                text,
                object_pairs_hook=hook,
                parse_float=self._parse_float,
                parse_int=self._parse_int,
                parse_constant=self._parse_constant,
            )
        except (ValueError, RecursionError, _DuplicateKey):
            return None
        if self._multiple == "all":
            value = [value]
        return ParseResult(self._finalize(value, fresh=True), True, True, True, 0, len(text), ())

    def _result(self, *, eof: bool) -> ParseResult:
        text = self._buffer.s
        if eof or text.rstrip()[-1:] in ("}", "]"):
            result = self._fast_path(text)
            if result is not None:
                return result
        self._scan.advance()
        found = self._scan.snapshot()
        if not found:
            if self._strict:
                raise BrokenJSONError("no JSON value found", 0)
            return ParseResult(None, False, False, False, 0, 0, ())
        value, complete, start, end, repairs = _assemble(text, found, self._multiple)
        if self._strict:
            first = repairs[0] if repairs else Repair(end, "not valid JSON")
            raise BrokenJSONError(first.message, first.position, tuple(repairs))
        return ParseResult(self._finalize(value), True, False, complete, start, end, tuple(repairs))


def parse_broken_json_result(
    data: str | bytes | IO[Any],
    allowed_keys: AllowedKeys = None,
    *,
    strict: bool = False,
    stream_stable: bool = False,
    multiple: Multiple = "auto",
    object_hook: _Hook = None,
    object_pairs_hook: _Hook = None,
    parse_float: _Hook = None,
    parse_int: _Hook = None,
    parse_constant: _Hook = None,
) -> ParseResult:
    """Parse ``data`` as JSON, recovering what it can, and report how it went.

    ``data`` may be text, UTF-8 bytes or a file-like object. A leading byte
    order mark is ignored.

    Valid JSON is returned as :func:`json.loads` would, with ``valid`` set.
    Otherwise the most plausible JSON value in the text is located (leading
    and trailing prose or code fences are ignored) and parsed as far as it
    goes, tolerating the usual deviations found in LLM output: single and
    typographic quotes, unquoted keys, ``//`` and ``/* */`` comments, Python
    and JavaScript literals, lenient numbers, missing or doubled commas, a
    missing colon after a quoted key, keys without a value (dropped),
    strings with a missing or unescaped quote, unquoted values, and an array
    left open before the next key. Every deviation is listed in ``repairs``.
    Duplicate keys keep the last value, as :func:`json.loads` does.

    On truncated input, complete key/value pairs, nested containers and a
    trailing unterminated string are kept; anything cut in the middle (a key,
    a literal, a number, an escape) is dropped and ``complete`` is ``False``.
    Nesting deeper than 200 levels is cut there.

    * ``allowed_keys``: keys to keep. An iterable applies at every nesting
      level. A mapping describes a shape: ``{"user": {"name": None}}`` keeps
      ``user`` and, inside it, only ``name``; ``None`` or ``True`` keeps
      everything under a key. ``None`` keeps all keys.
    * ``strict``: raise :class:`BrokenJSONError` instead of repairing. Valid
      JSON with duplicate keys is rejected too.
    * ``stream_stable``: treat ``data`` as the prefix of a stream and return
      only what was decided without looking past its end, so that the values
      never change when more input arrives. See :class:`StreamParser`.
    * ``multiple``: what to do when the text holds several top-level values.
      ``"auto"`` returns several objects as a list and otherwise the longest
      value; ``"all"`` always returns a list; ``"first"`` and ``"longest"``
      return one value.
    * ``object_hook``, ``object_pairs_hook``, ``parse_float``, ``parse_int``
      and ``parse_constant`` work as in :func:`json.loads`. The object hooks
      run after ``allowed_keys`` filtering.
    """
    parser = StreamParser(
        allowed_keys,
        strict=strict,
        multiple=multiple,
        object_hook=object_hook,
        object_pairs_hook=object_pairs_hook,
        parse_float=parse_float,
        parse_int=parse_int,
        parse_constant=parse_constant,
    )
    parser.feed(_to_text(data))
    return parser.snapshot() if stream_stable else parser.finish()


def parse_broken_json(
    data: str | bytes | IO[Any],
    allowed_keys: AllowedKeys = None,
    *,
    default: Any = None,
    strict: bool = False,
    stream_stable: bool = False,
    multiple: Multiple = "auto",
    object_hook: _Hook = None,
    object_pairs_hook: _Hook = None,
    parse_float: _Hook = None,
    parse_int: _Hook = None,
    parse_constant: _Hook = None,
) -> Any:
    """Parse ``data`` as JSON, recovering what it can when the JSON is broken.

    Returns the recovered value, mirroring the JSON structure, or ``default``
    when nothing could be recovered. Pass your own sentinel as ``default`` to
    tell a recovered ``null`` apart from a failure, or use
    :func:`parse_broken_json_result` for the full report. The other
    parameters are described there.
    """
    result = parse_broken_json_result(
        data,
        allowed_keys,
        strict=strict,
        stream_stable=stream_stable,
        multiple=multiple,
        object_hook=object_hook,
        object_pairs_hook=object_pairs_hook,
        parse_float=parse_float,
        parse_int=parse_int,
        parse_constant=parse_constant,
    )
    return result.value if result.found else default


def loads(data: str | bytes, allowed_keys: AllowedKeys = None, **options: Any) -> Any:
    """Drop-in replacement for :func:`json.loads`.

    Accepts the same options as :func:`parse_broken_json`, including the
    ``json`` hooks.
    """
    return parse_broken_json(data, allowed_keys, **options)


def load(fp: IO[Any], allowed_keys: AllowedKeys = None, **options: Any) -> Any:
    """Drop-in replacement for :func:`json.load`: parse a text or binary file object."""
    return parse_broken_json(fp.read(), allowed_keys, **options)


def from_file(
    path: str, allowed_keys: AllowedKeys = None, *, encoding: str = "utf-8", **options: Any
) -> Any:
    """Parse the file at ``path``. Undecodable bytes are replaced, never raised."""
    with open(path, encoding=encoding, errors="replace") as fp:
        return parse_broken_json(fp.read(), allowed_keys, **options)


def remove_leading_unwanted_text(text: str) -> str:
    """Return ``text`` from the start of the most plausible JSON value in it.

    Kept for backward compatibility; ``ParseResult.start`` gives the offset.
    """
    text = _to_text(text)
    result = parse_broken_json_result(text)
    return text[result.start :] if result.found else text
