import math

import pytest

from parse_broken_json import parse_broken_json, remove_leading_unwanted_text

LDQ, RDQ = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
LSQ, RSQ = "\N{LEFT SINGLE QUOTATION MARK}", "\N{RIGHT SINGLE QUOTATION MARK}"

KEYS = [
    "name",
    "age",
    "address",
    "city",
    "street",
    "a",
    "b",
    "c",
    "d",
    "x",
    "b_2",
    "$c",
    "first-name",
    "example",
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # README example: nested object truncated inside a string value.
        (
            '{"name": "John", "age": 30, "address": {"city": "New',
            {"name": "John", "age": 30, "address": {"city": "New"}},
        ),
        # Valid JSON is returned as-is.
        ('{"name": "John", "age": 30}', {"name": "John", "age": 30}),
        ("[1, 2, 3]", [1, 2, 3]),
        # Truncation at every kind of position.
        ('{"name": "John", "age": 30, "city": "New', {"name": "John", "age": 30, "city": "New"}),
        ('{"name": "John", "age": 30, ', {"name": "John", "age": 30}),
        ('{"name": "John", "age": 30, "ci', {"name": "John", "age": 30}),
        ('{"name": "John", "age": 30, "city"', {"name": "John", "age": 30}),
        ('{"name": "John", "age": 30, "city":', {"name": "John", "age": 30}),
        ('{"a": 1, "b": tru', {"a": 1}),
        ('{"a": 1, "b": 12', {"a": 1, "b": 12}),
        ("[1, 2.", [1, 2.0]),
        ("[1, 2e", [1]),
        ("[1, 2, 3", [1, 2, 3]),
        ('[{"a": 1}, {"a": 2}, {"a": 3', [{"a": 1}, {"a": 2}, {"a": 3}]),
        ('[{"a": 1}, {"a": 2}, {"a": 3, "b": "xx', [{"a": 1}, {"a": 2}, {"a": 3, "b": "xx"}]),
        ('{"a": {"x": 1}, "b": 2, "c": "tru', {"a": {"x": 1}, "b": 2, "c": "tru"}),
        ('{"a": [1, {"b": [2', {"a": [1, {"b": [2]}]}),
        # Scalar values.
        ('{"a": null, "b": 1, "c": "x', {"a": None, "b": 1, "c": "x"}),
        ('{"a": true, "b": false, "c": "x', {"a": True, "b": False, "c": "x"}),
        ('{"a": -1.5e3, "b": "x', {"a": -1500.0, "b": "x"}),
        # Escape sequences.
        ('{"a": "say \\"hi\\"", "b": "x', {"a": 'say "hi"', "b": "x"}),
        ('{"a": "line1\\nline2", "b": "x', {"a": "line1\nline2", "b": "x"}),
        ('{"a": "back\\\\slash", "b": "x', {"a": "back\\slash", "b": "x"}),
        (
            '{"a": "\\u00e9\\ud83d\\ude00", "b": "x',
            {"a": "\N{LATIN SMALL LETTER E WITH ACUTE}\N{GRINNING FACE}", "b": "x"},
        ),
        ('{"a": "x\\u00', {"a": "x"}),
        ('{"a": "x\\', {"a": "x"}),
        ('{"a": "x\\ud83d\\ude0', {"a": "x"}),
        ('{"a": "x\\qy"}', {"a": "x\\qy"}),  # unknown escape kept literally
        # Trailing commas.
        ('{"a": 1, "b": 2,}', {"a": 1, "b": 2}),
        ("[1, 2,]", [1, 2]),
        # Surrounding text.
        ('Sure! Here is the JSON:\n{"a": 1, "b": "x"}', {"a": 1, "b": "x"}),
        ('{"a": 1}\nHope this helps!', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('See [1] for {"a": 1}', {"a": 1}),
        ('See [1] for {"a": 1} ok?', {"a": 1}),
        ('Numbers: [1, 2] and then {"a": 1, "b": "x', {"a": 1, "b": "x"}),
        ('\N{ZERO WIDTH NO-BREAK SPACE}{"a": 1}', {"a": 1}),
        # Nothing to recover.
        ("", None),
        ("None", None),
        ("just prose", None),
        ("{", None),
        ("[", None),
    ],
)
def test_truncation_and_surrounding_text(text, expected):
    assert parse_broken_json(text, KEYS) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Quotes.
        ("{'a': 1, 'b': 'x'}", {"a": 1, "b": "x"}),
        ("{'a': \"it's\"}", {"a": "it's"}),
        ("{'a': 'it\\'s'}", {"a": "it's"}),
        ('{"a": \'x"y\'}', {"a": 'x"y'}),
        (
            "{" + LDQ + "a" + RDQ + ": " + LDQ + "x" + RDQ + ", " + LSQ + "b" + RSQ + ": 1}",
            {"a": "x", "b": 1},
        ),
        # Unquoted keys.
        (
            '{a: 1, b_2: "x", $c: true, first-name: "y"}',
            {"a": 1, "b_2": "x", "$c": True, "first-name": "y"},
        ),
        # Comments.
        ('{"a": 1, // comment\n "b": 2}', {"a": 1, "b": 2}),
        ('{"a": 1, /* c */ "b": 2}', {"a": 1, "b": 2}),
        ('{"a": 1, /* unterminated', {"a": 1}),
        # Python and JavaScript literals.
        (
            '{"a": True, "b": False, "c": None, "d": undefined}',
            {"a": True, "b": False, "c": None, "d": None},
        ),
        ('{"a": Infinity, "b": -Infinity}', {"a": math.inf, "b": -math.inf}),
        # Lenient numbers.
        ('{"a": 01, "b": +1, "c": .5, "d": 1.}', {"a": 1, "b": 1, "c": 0.5, "d": 1.0}),
        ('{"a": 1_000, "b": 0x1F, "c": -0x10}', {"a": 1000, "b": 31, "c": -16}),
        # Missing commas.
        ('{"a": 1 "b": 2}', {"a": 1, "b": 2}),
        ('{"a": "x" "b": "y"}', {"a": "x", "b": "y"}),
        ("[1 2 3]", [1, 2, 3]),
        ('["a" "b"]', ["a", "b"]),
        ('["a" ""]', ["a", ""]),
        # Missing colon after a quoted key.
        ('{"a" 1, "b": 2}', {"a": 1, "b": 2}),
        # Doubled commas and missing values.
        ('{"a": 1,, "b": 2}', {"a": 1, "b": 2}),
        ('["a", "b",, "c"]', ["a", "b", "c"]),
        ("[, 1]", [1]),
        ('{"a": , "b": 2}', {"b": 2}),
        ('{"a": 1, "b": }', {"a": 1}),
        ('{"a": 1, "b", "c": 3}', {"a": 1, "c": 3}),
        # Several top-level objects come back as a list; mixed types do not.
        ('{"a": 1}{"b": 2}', [{"a": 1}, {"b": 2}]),
        ('{"a": 1}\n{"b": "x', [{"a": 1}, {"b": "x"}]),
        ('[1, 2, 3] and {"a": 1}', [1, 2, 3]),
    ],
)
def test_tolerated_deviations(text, expected):
    assert parse_broken_json(text, KEYS) == expected


def test_nan_literal():
    result = parse_broken_json('{"a": NaN, "b": 1}', KEYS)
    assert math.isnan(result["a"]) and result["b"] == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Missing closing quote: a comma or newline followed by a quoted key closes it.
        ('{"a": "x, "b": 2}', {"a": "x", "b": 2}),
        ('{"a": "x\n"b": 2}', {"a": "x", "b": 2}),
        ('{"a: 1, "b": 2}', {"b": 2}),
        ('{"a": "x}', {"a": "x"}),
        ('{"a": {"b": "x}', {"a": {"b": "x"}}),
        # Unescaped quotes inside a string are content while it can still be closed.
        ('{"a": "say "hi"", "b": 2}', {"a": 'say "hi"', "b": 2}),
        ("{'a': 'it's'}", {"a": "it's"}),
        ('{"a": "x" garbage, "b": 2}', {"a": "x", "b": 2}),  # garbage becomes a key without a value
        # Unquoted values run up to the next delimiter.
        ('{"a": hello, "b": 2}', {"a": "hello", "b": 2}),
        ('{"a": hello world, "b": 2}', {"a": "hello world", "b": 2}),
        ('{"a": <b>bold</b>, "b": 2}', {"a": "<b>bold</b>", "b": 2}),
        ('{"a": 12:30, "b": 2}', {"a": "12:30", "b": 2}),
        ('{"a": 1.2.3}', {"a": "1.2.3"}),
        ('{"a": nullish}', {"a": "nullish"}),
        ("[a, b, c]", ["a", "b", "c"]),
        ('{"a": 1, "b": tru}', {"a": 1, "b": "tru"}),
        # A string followed by a colon inside an array closes the array.
        ('{"a": [1, 2, "b": 3}', {"a": [1, 2], "b": 3}),
        ('{"a": ["b": 3}', {"a": [], "b": 3}),
        # Documented limit: an inner quote followed by a comma is not recovered.
        ('{"a": "say "hi, there"", "b": 2}', {"a": "say "}),
    ],
)
def test_string_and_bare_value_repairs(text, expected):
    assert parse_broken_json(text, KEYS) == expected


def test_unescaped_quote_inside_key():
    assert parse_broken_json('{"my "key"": 1}', ['my "key"']) == {'my "key"': 1}


def test_allowed_keys_filter_applies_at_every_level():
    text = '{"a": 1, "zzz": 2, "b": {"zzz": 3, "a": 4}, "c": [{"zzz": 5, "a": 6}]}'
    assert parse_broken_json(text, ["a", "b", "c"]) == {"a": 1, "b": {"a": 4}, "c": [{"a": 6}]}


def test_allowed_keys_filter_applies_when_recovering():
    assert parse_broken_json('{"a": 1, "zzz": 2, "b": "x', ["a", "b"]) == {"a": 1, "b": "x"}


def test_remove_leading_unwanted_text():
    assert remove_leading_unwanted_text('See [1] for {"a": 1}') == '{"a": 1}'
    assert remove_leading_unwanted_text("no json here") == "no json here"
