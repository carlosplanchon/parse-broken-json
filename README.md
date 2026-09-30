# parse_broken_json

[![CI](https://github.com/carlosplanchon/parse_broken_json/actions/workflows/ci.yml/badge.svg)](https://github.com/carlosplanchon/parse_broken_json/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/parse_broken_json)](https://pypi.org/project/parse_broken_json/)

Parse broken or truncated JSON, as produced by LLMs, recovering as much as possible.

`parse_broken_json` takes text that should have been JSON and was not: cut
short by a token limit, wrapped in prose or a code fence, written with single
quotes, unquoted keys, comments or trailing commas, or with a quote that was
never closed. It returns whatever structure can be recovered, tells you what
it had to repair, and never raises on bad input unless you ask it to.

- Pure Python, no dependencies, typed.
- Valid JSON takes the fast path through the standard `json` module and is never touched.
- Truncated input keeps every complete key/value pair, nested container and trailing string.
- Streams are parsed incrementally, in linear time, and every value returned mid-stream is final.
- A result object reports whether the input was valid, whether it was complete, and every repair made.

## Installation

```bash
uv add parse_broken_json
```

or `pip install parse_broken_json`. Python 3.11 or later.

## Quick start

```python
from parse_broken_json import parse_broken_json

parse_broken_json('Sure! Here it is:\n{"name": "John", "age": 30, "address": {"city": "New')
# {'name': 'John', 'age': 30, 'address': {'city': 'New'}}

parse_broken_json("{'name': 'John', age: 30, // comment\n tags: [a, b,]}")
# {'name': 'John', 'age': 30, 'tags': ['a', 'b']}
```

Nothing recoverable returns `None`. Pass `default=` to tell that apart from
a recovered `null`, or ask for the full report:

```python
from parse_broken_json import parse_broken_json_result

result = parse_broken_json_result('{"a": "x, "b": tru')
result.value     # {'a': 'x'}
result.found     # True: something was recovered
result.valid     # False: the text was not valid JSON
result.complete  # False: the input ended inside the value
result.repairs   # (Repair(position=8, message='missing closing quote'),
                 #  Repair(position=18, message='input ended inside a value'))
```

### Streaming

`StreamParser` takes the stream chunk by chunk. At any point, `value` holds
what has been decided so far. Every value in it was settled without looking
past the end of the input received, so it never changes when more input
arrives: a string shows up once its closing quote and the character after it
are in, a number once the character after it is in.

```python
from parse_broken_json import StreamParser

parser = StreamParser(multiple="first")
for chunk in ['{"done": tr', 'ue, "text": "Once upon', ' a time", "n": 1', '2}']:
    parser.feed(chunk)
    print(parser.value)
# None
# {'done': True}
# {'done': True, 'text': 'Once upon a time'}
# {'done': True, 'text': 'Once upon a time', 'n': 12}

parser.finish()  # the same ParseResult parse_broken_json_result would give for the whole text
```

The work done is linear in the stream: the parser suspends where it needs
more input and resumes from there. Chunks may be text or UTF-8 bytes, even
when a multi-byte character is split between two of them. `snapshot()`
returns the same report as `finish()`, for the input so far.

The one-shot equivalent is `stream_stable=True`: `parse_broken_json(prefix,
stream_stable=True)` returns exactly what a `StreamParser` fed that prefix
would.

```python
parse_broken_json('{"done": true, "text": "Once upon a ti', stream_stable=True)
# {'done': True}
parse_broken_json('{"done": true, "text": "Once upon a ti')
# {'done': True, 'text': 'Once upon a ti'}
```

With `multiple="auto"` (the default) the shape of the value changes once a
second top-level object shows up: a dict becomes a list of dicts. Use
`multiple="first"` or `multiple="all"` when streaming if the shape must not
change.

### Strict mode

```python
parse_broken_json('{"a": 1,}', strict=True)
# BrokenJSONError: trailing comma at position 8
```

`BrokenJSONError` is a `ValueError` with `.position`, `.message` and
`.repairs`. Strict mode also rejects valid JSON with duplicate keys, which
`json.loads` accepts.

### Keeping only known keys

`allowed_keys` drops every other key. An iterable applies at every nesting
level; a mapping describes a shape, where `None` or `True` keeps everything
under a key:

```python
text = '{"user": {"name": "Ann", "pw": "x"}, "meta": 3, "junk": 4}'
parse_broken_json(text, ["user", "name"])
# {'user': {'name': 'Ann'}}
parse_broken_json(text, {"user": {"name": None}, "meta": True})
# {'user': {'name': 'Ann'}, 'meta': 3}
```

### Several values in one text

Several top-level objects come back together as a list, which suits
NDJSON-like output. Otherwise the longest value wins. `multiple=` changes
that: `"all"` always returns a list, `"first"` and `"longest"` return one
value.

### The `json` module names and hooks

The input may be text, UTF-8 bytes or a file-like object. A leading byte
order mark is ignored. `loads`, `load` and `from_file` mirror the `json`
module, and the `json.loads` hooks work everywhere: `parse_float`,
`parse_int`, `parse_constant`, `object_hook` and `object_pairs_hook`. The
object hooks run after `allowed_keys` filtering.

```python
from decimal import Decimal

from parse_broken_json import loads

loads('{"price": 19.90, "qty": 2', parse_float=Decimal)
# {'price': Decimal('19.90'), 'qty': 2}
```

Duplicate keys keep the last value, as in `json.loads`, and are listed in the
repairs.

### Command line

```bash
parse_broken_json broken.json                 # repaired JSON on standard output
cat broken.json | parse_broken_json --compact
parse_broken_json broken.json -o fixed.json
parse_broken_json broken.json -i --report     # fix in place, list the repairs on stderr
parse_broken_json --help
```

## What it repairs

Every repair is a bounded guess that only applies to invalid input.

| Problem | Example | Result |
|---|---|---|
| Truncated anywhere | `{"a": 1, "b": {"c": "d` | `{'a': 1, 'b': {'c': 'd'}}` |
| Prose or code fences around the JSON | `` Here: ```json {"a": 1} ``` `` | `{'a': 1}` |
| Single or typographic quotes | `{'a': “x”}` | `{'a': 'x'}` |
| Unquoted keys and values | `{a: hello world}` | `{'a': 'hello world'}` |
| Comments | `{"a": 1 /* c */, // d`⏎`"b": 2}` | `{'a': 1, 'b': 2}` |
| Python and JavaScript literals | `{"a": True, "b": None, "c": undefined}` | `{'a': True, 'b': None, 'c': None}` |
| Lenient numbers | `{"a": 01, "b": +1, "c": .5, "d": 1_000, "e": 0x1F}` | `{'a': 1, 'b': 1, 'c': 0.5, 'd': 1000, 'e': 31}` |
| Missing, doubled or trailing commas | `{"a": 1 "b": 2,, "c": 3,}` | `{'a': 1, 'b': 2, 'c': 3}` |
| Missing colon or missing value | `{"a" 1, "b": , "c": 2}` | `{'a': 1, 'c': 2}` |
| Missing closing quote | `{"a": "x, "b": 2}` | `{'a': 'x', 'b': 2}` |
| Unescaped quotes inside a string | `{"a": "say "hi"", "b": 2}` | `{'a': 'say "hi"', 'b': 2}` |
| Array left open before the next key | `{"a": [1, 2, "b": 3}` | `{'a': [1, 2], 'b': 3}` |

Limits:

- Repairs are guesses and can pick the wrong reading. An unescaped quote
  followed by a comma before the closing quote is not recovered:
  `{"a": "say "hi, there"", "b": 2}` stops at `say `.
- Nesting deeper than 200 levels is cut there on the recovery path. The fast
  path for valid JSON has the limits of `json.loads`.
- No schema-guided repair. Validate the result with Pydantic instead, see below.
- Pure Python. Fine for LLM-sized output; see the numbers below for large documents.

## Using with Pydantic

Repair first, then validate. Pydantic reports what is missing or of the wrong
type, which is a better place for that logic than a parser:

```python
from pydantic import BaseModel

from parse_broken_json import parse_broken_json


class Article(BaseModel):
    title: str
    tags: list[str] = []


Article.model_validate(parse_broken_json(text, default={}))
```

For truncated JSON with no syntax errors, Pydantic can also parse partial
input by itself:
`TypeAdapter(Article).validate_json(text, experimental_allow_partial="trailing-strings")`.

## Compared with json_repair and jiter

Measured on 2026-09-30 with json_repair 0.63.5 and jiter 0.17.0.

- [json_repair](https://github.com/mangiucugna/json_repair) repairs a similar
  set of problems, adds schema-guided repair and returns a repaired JSON
  string by default. On a battery of 41 malformed inputs the two agree on
  every recoverable case; where they differ, this library keeps `12:30` and
  `<b>bold</b>` intact, reads `NaN` and `0x1F` as numbers, and drops a key
  with a missing value instead of inventing an empty string.
- [jiter](https://github.com/pydantic/jiter), the Rust parser behind
  Pydantic, handles truncation only, with no tolerance for syntax
  deviations.

On a 284 KB document truncated in the middle: parse_broken_json 48 ms,
json_repair 64 ms, jiter 1 ms. Streaming the same document through
`StreamParser` in 4 KB chunks, with a snapshot after each: 53 ms in total.

## Development

```bash
uv sync                                  # installs the dev group
uv run pytest --cov=parse_broken_json
uv run ruff check parse_broken_json tests
uv run ruff format --check parse_broken_json tests
uv run ty check                          # the everyday type checker, fast
uv run mypy                              # the gate in CI, strict
```

Both type checkers run in CI. ty is the one to use while working; mypy
stays because the package ships `py.typed` and its users check their own
code with mypy or pyright. A `# type: ignore` comment silences both.

The property-based tests in `tests/test_properties.py` pin down the
guarantees: no input raises, valid JSON comes back unchanged with no repairs,
and any chunking of a stream gives the same final result while every
snapshot only holds final values.

## License

MIT.
