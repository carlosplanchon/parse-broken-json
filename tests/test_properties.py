"""Property-based tests. They pin down the guarantees the parser makes."""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from parse_broken_json import parse_broken_json_result

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**9), max_value=10**9)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=25,
)
containers = st.lists(json_values, max_size=4) | st.dictionaries(
    st.text(max_size=8), json_values, max_size=4
)


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield from leaves(sub, (*path, key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from leaves(sub, (*path, index))
    else:
        yield path, value


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=300))
def test_any_text_never_raises(text):
    result = parse_broken_json_result(text)
    assert isinstance(result.found, bool)


@settings(max_examples=300, deadline=None)
@given(json_values)
def test_valid_json_is_returned_unchanged(value):
    text = json.dumps(value)
    result = parse_broken_json_result(text)
    assert result.valid and result.complete and result.repairs == ()
    assert result.value == json.loads(text)


@settings(max_examples=300, deadline=None)
@given(containers)
def test_valid_container_with_trailing_text_is_recovered_verbatim(value):
    result = parse_broken_json_result(json.dumps(value) + " and some trailing words")
    assert result.value == value
    assert result.complete
    assert [repair.message for repair in result.repairs] == ["trailing text ignored"]


@settings(max_examples=300, deadline=None)
@given(containers, st.data())
def test_prefixes_never_raise_and_stream_stable_values_are_final(value, data):
    text = json.dumps(value)
    cut = data.draw(st.integers(min_value=0, max_value=len(text)))
    result = parse_broken_json_result(text[:cut], stream_stable=True)
    if result.found:
        final = dict(leaves(value))
        for path, leaf in leaves(result.value):
            assert final[path] == leaf


@settings(max_examples=200, deadline=None)
@given(json_values, st.data())
def test_random_chunking_matches_one_shot_and_snapshots_are_final(value, data):
    from parse_broken_json import StreamParser

    text = json.dumps(value)
    cuts = sorted(data.draw(st.lists(st.integers(min_value=0, max_value=len(text)), max_size=6)))
    final = dict(leaves([value]))
    parser = StreamParser(multiple="all")
    previous = 0
    for cut in [*cuts, len(text)]:
        parser.feed(text[previous:cut])
        previous = cut
        snapshot = parser.snapshot()
        if snapshot.found:
            for path, leaf in leaves(snapshot.value):
                assert final[path] == leaf
    assert parser.finish().value == [json.loads(text)]
