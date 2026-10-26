"""Test mock provider: offline, deterministik, hasil valid."""
from __future__ import annotations

import json
import struct
import wave
import zlib
from pathlib import Path

import pytest

from shortsgen.errors import NotConfiguredError, ProviderError
from shortsgen.providers.mock import (
    MockFetch,
    MockImage,
    MockLLM,
    MockSearch,
    MockSTT,
    MockTTS,
    MockVideo,
)


def test_mock_llm_returns_json_when_asked() -> None:
    llm = MockLLM()
    result = llm.chat([{"role": "user", "content": "Balas dalam JSON ya"}])
    payload = json.loads(result.text)
    assert payload["mock"] is True
    assert result.usage["input_tokens"] >= 0


def test_mock_llm_deterministic_and_failure_injection() -> None:
    llm = MockLLM(fixed_response="tetap")
    assert llm.chat([{"role": "user", "content": "a"}]).text == "tetap"
    assert llm.chat([{"role": "user", "content": "b"}]).text == "tetap"
    assert len(llm.calls) == 2

    llm.fail_times = 1
    with pytest.raises(ProviderError) as exc_info:
        llm.chat([{"role": "user", "content": "c"}])
    assert exc_info.value.retryable is True
    llm.chat([{"role": "user", "content": "d"}])  # sukses lagi setelah satu kegagalan


def test_mock_tts_writes_valid_wav(tmp_path: Path) -> None:
    tts = MockTTS(words_per_minute=120)
    out = tmp_path / "voice.wav"
    result = tts.synthesize("satu dua tiga empat lima enam", out)
    assert out.exists() and result.bytes_written > 44
    with wave.open(str(out), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == MockTTS.SAMPLE_RATE
        frames = wav.getnframes()
    # 6 kata @120 wpm = 3 detik
    assert abs(frames / MockTTS.SAMPLE_RATE - 3.0) < 0.2


def test_mock_image_writes_valid_png(tmp_path: Path) -> None:
    image = MockImage()
    out = tmp_path / "img.png"
    result = image.generate("latar merah", out, size="64x32")
    assert out.exists()
    raw = out.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (64, 32)
    # chunk IEND ada di akhir
    assert raw.endswith(b"IEND\xaeB`\x82") or b"IEND" in raw[-12:]
    assert result.mime == "image/png"
    # deterministik: prompt sama -> byte sama
    out2 = tmp_path / "img2.png"
    image.generate("latar merah", out2, size="64x32")
    assert out.read_bytes() == out2.read_bytes()


def test_mock_video_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(NotConfiguredError):
        MockVideo().generate("gerakan", tmp_path / "v.mp4")


def test_mock_search_marks_results_as_mock() -> None:
    hits = MockSearch().search("mars", max_results=2)
    assert len(hits) == 2
    assert all("[MOCK]" in h.title for h in hits)
    assert all("example.invalid" in h.url for h in hits)


def test_mock_fetch_truncates(tmp_path: Path) -> None:
    fetch = MockFetch()
    result = fetch.fetch("https://example.com", max_chars=20)
    assert result.truncated is True
    assert len(result.text) == 20


def test_mock_stt_requires_existing_file(tmp_path: Path) -> None:
    stt = MockSTT()
    with pytest.raises(ProviderError):
        stt.transcribe(tmp_path / "tidak-ada.wav")
    audio = tmp_path / "ada.wav"
    audio.write_bytes(b"RIFF....WAVE")
    assert "mock" in stt.transcribe(audio).text.lower()


def test_mock_provider_health_all_available_except_video() -> None:
    from shortsgen.providers.base import HealthStatus

    assert MockLLM().health().status == HealthStatus.AVAILABLE
    assert MockTTS().health().status == HealthStatus.AVAILABLE
    assert MockImage().health().status == HealthStatus.AVAILABLE
    assert MockSearch().health().status == HealthStatus.AVAILABLE
    assert MockFetch().health().status == HealthStatus.AVAILABLE
    assert MockSTT().health().status == HealthStatus.AVAILABLE
    assert MockVideo().health().status == HealthStatus.NOT_CONFIGURED
