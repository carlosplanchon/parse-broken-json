# Changelog

## 0.4.0 - 2026-09-30

The parser was rewritten from scratch. It is a small recursive-descent parser
with no dependencies; `ijson` is no longer required.

### Breaking changes

- `parse_broken_json` returns a value that mirrors the JSON structure. A
  truncated object comes back as a `dict`, not as a list with one `dict`.
- `allowed_keys` is optional. When given, it also filters valid JSON and
  applies at every nesting level.
- Several top-level objects come back together as a list.
- The special case that returned `[]` for text ending in `None` is gone.
- The `JSONEventPartialItpr` class is gone.

### Added

- `StreamParser` parses a stream chunk by chunk in linear time. Its
  snapshots only contain values that were decided without looking past the
  end of the input received, so they never change when more input arrives.
- The `json.loads` hooks: `object_hook`, `object_pairs_hook`, `parse_float`,
  `parse_int` and `parse_constant`, on both the fast path and the recovery path.
- Duplicate keys are reported (the last value wins, as in `json.loads`) and
  rejected in strict mode.
- `parse_broken_json_result` returns a `ParseResult` with `found`, `valid`,
  `complete`, `start`, `end` and the list of `repairs`, so a recovered `null`
  can be told apart from a failure and truncation is reported.
- `default=` on `parse_broken_json`, returned when nothing is recovered.
- `strict=True` raises `BrokenJSONError` instead of repairing.
- `stream_stable=True` drops the values that may still grow at the end of a
  streamed prefix, so results never change when more input arrives.
- `multiple=` controls what to return when the text holds several values.
- `allowed_keys` accepts a nested mapping describing the shape to keep.
- Input may be `str`, UTF-8 `bytes` or a file-like object. A leading byte
  order mark is ignored.
- `loads`, `load` and `from_file`, mirroring the `json` module.
- A command-line tool: `parse_broken_json [file] [-o OUT] [-i] [--report] ...`.
- Tolerance for single and typographic quotes, unquoted keys, comments,
  Python and JavaScript literals, lenient numbers, missing or doubled commas,
  missing colons and missing values.
- Bounded string repairs: missing closing quotes, unescaped quotes inside
  strings, unquoted values, and arrays left open before the next key.
- `py.typed`, `__version__`, a test suite with property-based tests, and CI.

### Fixed

- Nested objects are recovered. The README example returned `None`.
- Escape sequences (`\"`, `\n`, `\uXXXX`, surrogate pairs) are decoded.
- `null` is returned as `None`, not as the string `"None"`.
- A `[` in the prose before the JSON no longer hijacks the parse.
- The return type no longer depends on which recovery path succeeded.
- Text with many candidate starts that lead nowhere no longer takes
  quadratic time.

## 0.3 - 2026-02-06

- Packaging moved to PEP 639 license metadata.
