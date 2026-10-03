"""Scanning: find the candidate values in a text and pick the ones to return."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from ._buffer import _Buffer
from ._parser import _MISSING, _Hook, _Parsed, _PartialParser
from ._result import Repair

_CANDIDATE_RE = re.compile(r"[\[{]")


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
