"""Result types: :class:`Repair`, :class:`ParseResult` and :class:`BrokenJSONError`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle with the constructor's arguments; ``args`` only holds the formatted message."""
        return type(self), (self.message, self.position, self.repairs), self.__dict__
