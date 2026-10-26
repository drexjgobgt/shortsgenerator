"""Test parser JSON toleran untuk keluaran LLM."""
from __future__ import annotations

import pytest

from shortsgen.util.jsonparse import JsonParseError, extract_json, extract_json_object


def test_direct_json() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_fenced_json_block() -> None:
    text = "Berikut hasilnya:\n```json\n{\"hook\": \"halo\", \"scenes\": []}\n```\nSemoga membantu!"
    assert extract_json_object(text)["hook"] == "halo"


def test_plain_fence_without_language() -> None:
    text = "```\n{\"x\": true}\n```"
    assert extract_json(text) == {"x": True}


def test_prose_wrapped_object() -> None:
    text = 'Tentu! Ini JSON-nya: {"title": "Mars", "tags": ["a", "b"]} — silakan dipakai.'
    parsed = extract_json_object(text)
    assert parsed["title"] == "Mars"
    assert parsed["tags"] == ["a", "b"]


def test_trailing_comma_repair() -> None:
    text = '```json\n{"items": [1, 2, 3,], "ok": true,}\n```'
    assert extract_json_object(text) == {"items": [1, 2, 3], "ok": True}


def test_nested_braces_inside_strings() -> None:
    text = '{"narration": "Ia berkata {hai} lalu pergi", "n": 1}'
    assert extract_json_object(text)["narration"] == "Ia berkata {hai} lalu pergi"


def test_expect_type_mismatch_raises() -> None:
    with pytest.raises(JsonParseError):
        extract_json("[1,2]", expect=dict)


def test_invalid_text_raises_with_preview() -> None:
    with pytest.raises(JsonParseError) as exc_info:
        extract_json_object("maaf saya tidak bisa membantu kali ini")
    assert "preview" in str(exc_info.value)
    assert exc_info.value.preview


def test_empty_text_raises() -> None:
    with pytest.raises(JsonParseError):
        extract_json("")
