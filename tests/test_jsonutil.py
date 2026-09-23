import pytest

from jsonutil import JSONParseError, loads_loose


def test_plain_object():
    assert loads_loose('{"a": 1}') == {"a": 1}


def test_code_fence():
    assert loads_loose('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}


def test_prose_around_json():
    assert loads_loose('Here you go: {"kind": "index"} hope that helps') == {"kind": "index"}


def test_garbage_raises():
    with pytest.raises(JSONParseError):
        loads_loose("no json here")
