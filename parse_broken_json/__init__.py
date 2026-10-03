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

from typing import IO, Any

from ._parser import _Hook
from ._result import BrokenJSONError, ParseResult, Repair
from ._stream import _BOM, StreamParser

# The "as" form makes type checkers treat these aliases as public.
from ._stream import AllowedKeys as AllowedKeys
from ._stream import Multiple as Multiple

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

# Defined in private modules; present them as part of the package in reprs,
# tracebacks and pickles, so the module layout stays an implementation detail.
BrokenJSONError.__module__ = __name__
ParseResult.__module__ = __name__
Repair.__module__ = __name__
StreamParser.__module__ = __name__


def _to_text(data: Any) -> str:
    if hasattr(data, "read"):
        data = data.read()
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode("utf-8", errors="replace")
    if not isinstance(data, str):
        raise TypeError(f"expected str, bytes or a file-like object, got {type(data).__name__}")
    return data.removeprefix(_BOM)


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
