import time

from parse_broken_json import parse_broken_json


def test_many_dead_candidates_are_not_quadratic():
    # Every "[" is a candidate that leads nowhere. Before the shared delimiter
    # table each one rescanned to the end of the input: 20 000 of them took
    # tens of seconds. Now the whole thing takes milliseconds.
    text = "[a " * 20_000
    started = time.perf_counter()
    assert parse_broken_json(text) is None
    assert time.perf_counter() - started < 2.0


def test_unterminated_comments_are_not_quadratic():
    text = "[/*" * 20_000
    started = time.perf_counter()
    parse_broken_json(text)
    assert time.perf_counter() - started < 2.0


def test_deep_nesting_does_not_crash():
    assert isinstance(parse_broken_json("[" * 10_000), list)
    assert isinstance(parse_broken_json('{"a": ' * 10_000), dict)


def test_large_valid_document_takes_the_fast_path():
    import json

    doc = json.dumps(list(range(200_000)))
    started = time.perf_counter()
    assert parse_broken_json(doc) == json.loads(doc)
    assert time.perf_counter() - started < 2.0


def test_unterminated_line_comments_are_not_quadratic():
    text = "[// x " * 20_000
    started = time.perf_counter()
    parse_broken_json(text)
    assert time.perf_counter() - started < 2.0


def test_streaming_a_long_string_one_character_at_a_time_is_fast():
    from parse_broken_json import StreamParser

    text = '{"a": "' + "x" * 20_000 + '", "b": 1}'
    parser = StreamParser()
    started = time.perf_counter()
    for char in text:
        parser.feed(char)
    assert parser.finish().value == {"a": "x" * 20_000, "b": 1}
    assert time.perf_counter() - started < 2.0
