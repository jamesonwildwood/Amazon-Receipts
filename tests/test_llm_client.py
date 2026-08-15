import pytest

from app.parsing.llm_client import _parse_json_response


def test_parses_plain_json_with_no_fence():
    assert _parse_json_response('{"a": 1}') == {"a": 1}


def test_strips_json_language_tagged_fence():
    raw = '```json\n{"a": 1}\n```'
    assert _parse_json_response(raw) == {"a": 1}


def test_strips_bare_fence_with_no_language_tag():
    raw = '```\n{"a": 1}\n```'
    assert _parse_json_response(raw) == {"a": 1}


def test_strips_surrounding_whitespace_around_fence():
    raw = '  \n```json\n{"a": 1}\n```\n  '
    assert _parse_json_response(raw) == {"a": 1}


def test_does_not_eat_a_legitimate_trailing_backtick_in_unfenced_content():
    """Regression test: the old implementation used text.strip("`"), which
    strips *any* leading/trailing backtick characters, not just a real code
    fence — eating a legitimate backtick that happens to be the first/last
    character of real (unfenced) JSON content. The regex only strips when the
    whole response is actually wrapped start-to-end in a ``` fence."""
    raw = '{"a": "back`tick"}'
    assert _parse_json_response(raw) == {"a": "back`tick"}


def test_malformed_json_still_raises_for_the_retry_path_to_catch():
    with pytest.raises(Exception):
        _parse_json_response("not json at all")
