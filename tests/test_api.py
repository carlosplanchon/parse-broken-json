import io
import math
import pickle
import typing

import pytest

from parse_broken_json import (
    BrokenJSONError,
    ParseResult,
    Repair,
    StreamParser,
    __version__,
    from_file,
    load,
    loads,
    parse_broken_json,
    parse_broken_json_result,
)

SENTINEL = object()


def test_version_is_a_string():
    assert isinstance(__version__, str) and __version__


# --- default and found -------------------------------------------------------


def test_recovered_null_is_distinguishable_from_failure():
    assert parse_broken_json("null", default=SENTINEL) is None
    assert parse_broken_json("garbage", default=SENTINEL) is SENTINEL
    assert parse_broken_json_result("null").found is True
    assert parse_broken_json_result("garbage").found is False


def test_result_for_valid_json():
    result = parse_broken_json_result('{"a": 1}')
    assert result == ParseResult({"a": 1}, True, True, True, 0, 8, ())


def test_result_for_repaired_and_truncated_json():
    text = 'Sure: {"a": "x, "b": tru'
    result = parse_broken_json_result(text)
    assert result.value == {"a": "x"}
    assert (result.found, result.valid, result.complete) == (True, False, False)
    assert (result.start, result.end) == (6, len(text))
    assert [r.message for r in result.repairs] == [
        "leading text skipped",
        "missing closing quote",
        "input ended inside a value",
    ]
    assert result.repairs[1] == Repair(14, "missing closing quote")


def test_result_for_nothing_found():
    assert parse_broken_json_result("no json") == ParseResult(None, False, False, False, 0, 0, ())


@pytest.mark.parametrize(
    ("text", "messages"),
    [
        ("{'a': 1}", ["non-standard quotes"]),
        ('{"a": 1 // c\n}', ["comment"]),
        ('{"a": 1 "b": 2}', ["missing comma"]),
        ("{a: 1}", ["unquoted key"]),
        ('{"a" 1}', ["missing colon"]),
        ('{"a": 1, "b"}', ["key without value dropped"]),
        ('{"a": 1,, "b": 2}', ["extra comma"]),
        ('{"a": 1,}', ["trailing comma"]),
        ('{"a": True}', ["non-standard literal"]),
        ('{"a": 01}', ["non-standard number"]),
        ('{"a": hello}', ["unquoted value"]),
        ('{"a": "say "hi""}', ["unescaped quote inside string", "unescaped quote inside string"]),
        ('{"a": [1, "b": 2}', ["array closed before key"]),
        ('{"a": "x\\qy"}', ["unknown escape kept"]),
        ('{"a": "x\\uZZZZ"}', ["invalid unicode escape kept"]),
        ('{"a": "x\ty"}', ["control character inside string"]),
        ('{"a": 1}{"b": 2}', ["multiple values found"]),
        ('{"a": 1} and then', ["trailing text ignored"]),
        ('{"a": <}', ["unquoted value"]),
        ('{"a": 1, "b": ]}', ["unexpected text", "trailing text ignored"]),
        ('{"a": 1 :}', ["unexpected text inside object", "trailing text ignored"]),
    ],
)
def test_repairs_are_reported(text, messages):
    result = parse_broken_json_result(text)
    assert [r.message for r in result.repairs] == messages
    assert all(0 <= r.position <= len(text) for r in result.repairs)


def test_valid_json_reports_no_repairs():
    assert parse_broken_json_result('[1, {"a": null}]').repairs == ()


# --- strict ------------------------------------------------------------------


def test_strict_accepts_valid_json():
    assert parse_broken_json('{"a": 1}', strict=True) == {"a": 1}


def test_strict_raises_on_the_first_repair():
    with pytest.raises(BrokenJSONError) as info:
        parse_broken_json('{"a": 1,, "b": 2,}', strict=True)
    err = info.value
    assert isinstance(err, ValueError)
    assert (err.message, err.position) == ("extra comma", 8)
    assert [r.message for r in err.repairs] == ["extra comma", "trailing comma"]
    assert str(err) == "extra comma at position 8"


def test_strict_raises_when_nothing_is_found():
    with pytest.raises(BrokenJSONError, match="no JSON value found"):
        parse_broken_json("garbage", strict=True)


# --- stream_stable -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "loose", "stable"),
    [
        ('{"a": 1, "b": 12', {"a": 1, "b": 12}, {"a": 1}),
        ('{"a": 1, "b": "x', {"a": 1, "b": "x"}, {"a": 1}),
        ('{"a": 1, "b": tru', {"a": 1}, {"a": 1}),
        ('{"a": "x}', {"a": "x"}, None),
        ("[1, 2.", [1, 2.0], [1]),
        ("[1, 2]", [1, 2], [1, 2]),
    ],
)
def test_stream_stable_drops_values_that_may_still_grow(text, loose, stable):
    assert parse_broken_json(text) == loose
    assert parse_broken_json(text, stream_stable=True) == stable


def test_stream_stable_prefixes_only_contain_final_values():
    import json

    doc = json.dumps(
        {"name": "John", "n": 12.5, "ok": True, "tags": ["a", "b"], "sub": {"k": None}}
    )
    final = json.loads(doc)

    def leaves(value, path=()):
        if isinstance(value, dict):
            for k, v in value.items():
                yield from leaves(v, (*path, k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from leaves(v, (*path, i))
        else:
            yield path, value

    final_leaves = dict(leaves(final))
    for cut in range(len(doc) + 1):
        result = parse_broken_json_result(doc[:cut], stream_stable=True)
        if not result.found:
            continue
        for path, value in leaves(result.value):
            assert final_leaves[path] == value, (cut, path)


# --- multiple ----------------------------------------------------------------

TWO_OBJECTS = '{"a": 1} {"b": 2}'
MIXED = '[1, 2, 3] and {"a": 1}'


@pytest.mark.parametrize(
    ("text", "mode", "expected"),
    [
        (TWO_OBJECTS, "auto", [{"a": 1}, {"b": 2}]),
        (TWO_OBJECTS, "all", [{"a": 1}, {"b": 2}]),
        (TWO_OBJECTS, "first", {"a": 1}),
        (TWO_OBJECTS, "longest", {"a": 1}),
        (MIXED, "auto", [1, 2, 3]),
        (MIXED, "all", [[1, 2, 3], {"a": 1}]),
        (MIXED, "first", [1, 2, 3]),
        (MIXED, "longest", [1, 2, 3]),
        ('{"a": 1}', "all", [{"a": 1}]),
        ('{"a": 1}', "auto", {"a": 1}),
    ],
)
def test_multiple_modes(text, mode, expected):
    assert parse_broken_json(text, multiple=mode) == expected


def test_multiple_rejects_unknown_mode():
    with pytest.raises(ValueError, match="multiple must be one of"):
        parse_broken_json("{}", multiple="some")


# --- allowed_keys ------------------------------------------------------------

DOC = '{"user": {"name": "Ann", "pw": "x", "tags": [{"id": 1, "zz": 2}]}, "meta": 3, "junk": 4}'


def test_allowed_keys_none_keeps_everything():
    assert parse_broken_json(DOC) == parse_broken_json(DOC, None)
    assert parse_broken_json(DOC)["junk"] == 4


def test_allowed_keys_iterable_applies_at_every_level():
    assert parse_broken_json(DOC, ("user", "name", "tags", "id")) == {
        "user": {"name": "Ann", "tags": [{"id": 1}]}
    }


def test_allowed_keys_mapping_describes_a_shape():
    shape = {"user": {"name": None, "tags": {"id": True}}, "meta": True, "junk": False}
    assert parse_broken_json(DOC, shape) == {
        "user": {"name": "Ann", "tags": [{"id": 1}]},
        "meta": 3,
    }


def test_allowed_keys_shape_keeps_everything_under_none():
    assert parse_broken_json(DOC, {"user": None}) == {
        "user": {"name": "Ann", "pw": "x", "tags": [{"id": 1, "zz": 2}]}
    }


def test_allowed_keys_rejects_a_bare_string():
    with pytest.raises(TypeError):
        parse_broken_json(DOC, "user")


# --- inputs ------------------------------------------------------------------


def test_accepts_bytes_and_bom():
    assert parse_broken_json(b'{"a": "\xc3\xa9"}') == {"a": "\N{LATIN SMALL LETTER E WITH ACUTE}"}
    assert parse_broken_json(b'\xef\xbb\xbf{"a": 1}') == {"a": 1}
    assert parse_broken_json_result(b'\xef\xbb\xbf{"a": 1}').valid is True
    assert parse_broken_json(b'{"a": "\xff"}') == {"a": "\N{REPLACEMENT CHARACTER}"}


def test_accepts_file_like_objects():
    assert parse_broken_json(io.StringIO('{"a": 1')) == {"a": 1}
    assert parse_broken_json(io.BytesIO(b'{"a": 1')) == {"a": 1}
    assert load(io.StringIO("[1, 2")) == [1, 2]


def test_rejects_other_types():
    with pytest.raises(TypeError):
        parse_broken_json(123)


def test_loads_and_from_file(tmp_path):
    assert loads('{"a": 1,}') == {"a": 1}
    path = tmp_path / "broken.json"
    path.write_text('{"a": [1, 2', encoding="utf-8")
    assert from_file(str(path)) == {"a": [1, 2]}
    assert from_file(str(path), ["zzz"]) == {}


def test_nan_survives_the_result_object():
    result = parse_broken_json_result('{"a": NaN, "b": 1')
    assert math.isnan(result.value["a"]) and result.complete is False


# --- json hooks and duplicate keys -------------------------------------------


@pytest.mark.parametrize(
    "text",
    ['{"a": 1.5, "b": [2, {"c": 3}]}', '{"a": 1.5, "b": [2, {"c": 3'],
    ids=["valid", "broken"],
)
def test_json_hooks_work_on_both_paths(text):
    from decimal import Decimal

    assert parse_broken_json(text, parse_float=Decimal) == {"a": Decimal("1.5"), "b": [2, {"c": 3}]}
    assert parse_broken_json(text, parse_int=str) == {"a": 1.5, "b": ["2", {"c": "3"}]}
    assert parse_broken_json(text, object_hook=lambda d: ("obj", d)) == (
        "obj",
        {"a": 1.5, "b": [2, ("obj", {"c": 3})]},
    )
    assert parse_broken_json(text, object_pairs_hook=lambda pairs: pairs) == [
        ("a", 1.5),
        ("b", [2, [("c", 3)]]),
    ]


@pytest.mark.parametrize(
    "text", ['{"a": NaN, "b": -Infinity}', '{"a": NaN, "b": -Infinity'], ids=["valid", "broken"]
)
def test_parse_constant(text):
    assert parse_broken_json(text, parse_constant=str) == {"a": "NaN", "b": "-Infinity"}


def test_lenient_numbers_reach_the_hooks_normalized():
    text = '{"a": 01, "b": +1.50, "c": .5, "d": 1_000, "e": 7.'
    assert parse_broken_json(text, parse_float=str, parse_int=str) == {
        "a": "1",
        "b": "1.50",
        "c": "0.5",
        "d": "1000",
        "e": "7.0",
    }


def test_object_hooks_run_after_key_filtering():
    assert parse_broken_json('{"a": 1, "z": 2', ["a"], object_hook=sorted) == ["a"]


def test_duplicate_keys_keep_the_last_value_and_are_reported():
    result = parse_broken_json_result('{"a": 1, "a": 2, "b": 3')
    assert result.value == {"a": 2, "b": 3}
    assert next(r.message for r in result.repairs) == "duplicate key, last one wins"


def test_strict_rejects_duplicate_keys_even_in_valid_json():
    assert parse_broken_json('{"a": 1, "a": 2}') == {"a": 2}
    with pytest.raises(BrokenJSONError) as info:
        parse_broken_json('{"a": 1, "a": 2}', strict=True)
    assert (info.value.message, info.value.position) == ("duplicate key, last one wins", 9)


# --- public classes ----------------------------------------------------------


def test_public_classes_belong_to_the_package():
    for cls in (BrokenJSONError, ParseResult, Repair, StreamParser):
        assert cls.__module__ == "parse_broken_json"
    result = parse_broken_json_result('{"a": tru')
    assert pickle.loads(pickle.dumps(result)) == result
    assert typing.get_type_hints(ParseResult)["repairs"] == tuple[Repair, ...]
