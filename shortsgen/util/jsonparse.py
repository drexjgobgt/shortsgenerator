"""Parsing JSON dari keluaran LLM yang sering "kotor" (fence, prosa, koma ekor).

Urutan strategi:
1. json.loads langsung
2. blok fenced ```json ... ``` (atau ``` ... ```)
3. substring antara `{` pertama dan `}` terakhir (atau `[` ... `]`)
4. perbaikan ringan: koma ekor sebelum }/], tanda kutip tipografis, karakter kontrol
Kegagalan melempar JsonParseError dengan cuplikan teks untuk diagnosis.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..errors import ShortsGenError

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class JsonParseError(ShortsGenError):
    """Keluaran LLM bukan JSON yang bisa diparsing."""

    def __init__(self, message: str, *, preview: str = ""):
        self.preview = preview
        super().__init__(message + (f" | preview: {preview[:200]!r}" if preview else ""))


def _try_loads(candidate: str) -> Optional[Any]:
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    repaired = _TRAILING_COMMA_RE.sub(r"\1", _CONTROL_CHARS_RE.sub("", candidate))
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _balanced_slice(text: str, opener: str, closer: str) -> Optional[str]:
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]  # mungkin terpotong; biarkan pemanggil gagal dengan jelas


def extract_json(text: str, *, expect: Optional[type] = None) -> Any:
    """Ambil objek/array JSON pertama yang valid dari teks keluaran LLM.

    Args:
        text: keluaran mentah LLM.
        expect: tipe yang diharapkan (dict/list) untuk validasi cepat.

    Raises:
        JsonParseError: bila tidak ada JSON valid yang ditemukan.
    """
    if not text or not text.strip():
        raise JsonParseError("keluaran LLM kosong")

    direct = _try_loads(text)
    if direct is not None:
        if expect is not None and not isinstance(direct, expect):
            raise JsonParseError(
                f"JSON ditemukan tetapi bertipe {type(direct).__name__}, diharapkan {expect.__name__}",
                preview=text,
            )
        return direct

    for match in _FENCE_RE.finditer(text):
        parsed = _try_loads(match.group(1))
        if parsed is not None:
            if expect is not None and not isinstance(parsed, expect):
                continue
            return parsed

    for opener, closer in (("{", "}"), ("[", "]")):
        slice_text = _balanced_slice(text, opener, closer)
        if slice_text:
            parsed = _try_loads(slice_text)
            if parsed is not None:
                if expect is not None and not isinstance(parsed, expect):
                    continue
                return parsed

    raise JsonParseError("tidak ditemukan JSON valid pada keluaran LLM", preview=text)


def extract_json_object(text: str) -> dict[str, Any]:
    return extract_json(text, expect=dict)
