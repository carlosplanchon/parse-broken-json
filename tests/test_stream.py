"""StreamParser and the stream_stable guarantee."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from parse_broken_json import BrokenJSONError, StreamParser, parse_broken_json_result

VALID = json.dumps(
    {
        "name": 'John "JJ" O\'Neil',
        "age": 30,
        "ok": True,
        "n": None,
        "f": -1.5e3,
        "tags": ["a", "b, c", 'd\n"e"'],
        "addr": {"city": "New York", "geo": [40.7, -74.0]},
        "note": "see [1], {2} and \\u00e9: done",
        "items": [{"id": 1, "v": []}, {"id": 2, "v": [{}]}],
    }
)
DEVIANT = [
    "{'name': 'John', age: 30, // comment\n tags: [a, b,], nested: {x: 1.5, ok: True}}",
    (
        'Sure! Here it is:\n```json\n{"a": 1, "b": "two words", "c": [1, 2, 3], "d": null}\n```\n'
        "Hope this helps."
    ),
    '{"a": "say "hi"", "b": 2, "c": "x, "d": 3}',
    '{"a": [1, 2, "b": 3, "c": {"d": "e"}}',
    '{"a": 1 "b": 2,, "c": 3,}',
    '{"a": hello world, "b": 12:30, "c": <b>bold</b>}',
    '{"a": 1}\n{"b": "x"}\n{"c": [true]}',
    '{"a": "line1\\nline2", "b": "\\u00e9\\ud83d\\ude00", "c": -1.5e3, "d": 0x1F, /* c */ "e": .5}',
    '[{"a": 1}, {"a": 2}, {"a": 3, "b": "xx',
]
DOCS = [VALID, *DEVIANT]


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield from leaves(sub, (*path, key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from leaves(sub, (*path, index))
    else:
        yield path, value


def feed_in_chunks(doc, size, **options):
    parser = StreamParser(**options)
    for k in range(0, len(doc), size):
        parser.feed(doc[k : k + size])
    return parser


@pytest.mark.parametrize("size", [1, 2, 3, 7, 50])
@pytest.mark.parametrize("doc", DOCS)
def test_chunked_feed_matches_one_shot(doc, size):
    expected = parse_broken_json_result(doc, multiple="all")
    assert feed_in_chunks(doc, size, multiple="all").finish() == expected


@pytest.mark.parametrize("doc", DOCS)
def test_snapshots_only_contain_final_values(doc):
    final = dict(leaves(parse_broken_json_result(doc, multiple="all").value))
    parser = StreamParser(multiple="all")
    for char in doc:
        parser.feed(char)
        snapshot = parser.snapshot()
        if snapshot.found:
            for path, leaf in leaves(snapshot.value):
                assert final[path] == leaf, (path, leaf, parser.text)


@pytest.mark.parametrize("doc", DOCS)
def test_one_shot_stream_stable_matches_the_incremental_snapshot(doc):
    for cut in range(len(doc) + 1):
        one_shot = parse_broken_json_result(doc[:cut], stream_stable=True, multiple="all")
        incremental = feed_in_chunks(doc[:cut], 3, multiple="all").snapshot()
        assert one_shot.value == incremental.value, cut


def test_values_appear_as_soon_as_they_are_decided():
    parser = StreamParser(multiple="first")
    seen = []
    for chunk in ['{"done": tr', 'ue, "text": "Once upon', ' a time", "n": 1', "2}"]:
        parser.feed(chunk)
        seen.append(parser.value)
    assert seen == [
        None,
        {"done": True},
        {"done": True, "text": "Once upon a time"},
        {"done": True, "text": "Once upon a time", "n": 12},
    ]
    assert parser.finish() == parse_broken_json_result(parser.text)


def test_bytes_chunks_may_split_a_multibyte_character():
    parser = StreamParser()
    parser.feed(b'{"a": "\xc3')
    parser.feed(b'\xa9", "b": 1}')
    assert parser.finish().value == {"a": "\N{LATIN SMALL LETTER E WITH ACUTE}", "b": 1}


def test_bom_is_dropped_and_str_and_bytes_may_be_mixed():
    parser = StreamParser()
    parser.feed(b'\xef\xbb\xbf{"a"')
    parser.feed(": 1}")
    result = parser.finish()
    assert (result.value, result.valid) == ({"a": 1}, True)


def test_snapshot_value_is_a_copy():
    parser = StreamParser()
    parser.feed('{"a": 1, "b": [')
    value = parser.value
    assert value == {"a": 1}
    value["a"] = 99
    parser.feed("2]}")
    assert parser.finish().value == {"a": 1, "b": [2]}


def test_finish_is_final():
    parser = StreamParser()
    parser.feed('{"a": 1')
    first = parser.finish()
    assert first.value == {"a": 1} and first.complete is False
    assert parser.finish() is first
    assert parser.snapshot() is first
    with pytest.raises(ValueError, match="finish"):
        parser.feed("}")


def test_strict_stream_raises_on_the_first_repair():
    parser = StreamParser(strict=True)
    parser.feed('{"a": 1,, "b": 2}')
    with pytest.raises(BrokenJSONError, match="extra comma at position 8"):
        parser.finish()


def test_first_keeps_the_shape_stable_across_several_objects():
    parser = StreamParser(multiple="first")
    values = []
    for chunk in ['{"a": 1}', '\n{"b": 2}', '\n{"c": 3}']:
        parser.feed(chunk)
        values.append(parser.value)
    assert values == [{"a": 1}, {"a": 1}, {"a": 1}]
    assert parser.finish().value == {"a": 1}


def test_stream_parser_rejects_bad_arguments():
    with pytest.raises(ValueError, match="multiple must be one of"):
        StreamParser(multiple="some")
    with pytest.raises(TypeError):
        StreamParser().feed(123)


def test_module_entry_point_runs_the_cli():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "parse_broken_json", "--compact"],
        input='{"a": 1,}',
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    assert (proc.returncode, proc.stdout) == (0, '{"a": 1}\n')
