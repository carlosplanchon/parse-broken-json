"""Incremental parsing: :class:`StreamParser`, which every entry point goes through."""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from ._buffer import _Buffer
from ._parser import _Hook
from ._result import BrokenJSONError, ParseResult, Repair
from ._scan import _assemble, _Scan

Multiple = Literal["auto", "all", "first", "longest"]
_MULTIPLE_MODES = ("auto", "all", "first", "longest")

AllowedKeys = Iterable[str] | Mapping[str, Any] | None
_Allowed = frozenset[str] | dict[str, Any] | None

_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"


class _DuplicateKey(Exception):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise _DuplicateKey
    return dict(pairs)


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
