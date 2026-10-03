"""Input buffer: the text so far, plus lookup tables that grow with it."""

from __future__ import annotations

import re
from bisect import bisect_left

_DELIM_RE = re.compile(r"[,}\]\r\n]")
# Structural characters: whatever follows one of them cannot change the
# outcome of the "missing closing quote" lookahead.
_STRUCT_RE = re.compile(r"[,}\]:{\[\r\n]")


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
