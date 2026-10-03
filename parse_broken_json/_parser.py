"""Parser: a tolerant recursive descent, written as generators that suspend for input."""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from typing import Any

from ._buffer import _Buffer
from ._result import Repair

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


def _is_word_char(s: str, i: int) -> bool:
    return i < len(s) and (s[i].isalnum() or s[i] == "_")


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
