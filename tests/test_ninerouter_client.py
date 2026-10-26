"""Test klien 9Router: retry, timeout, parsing JSON/SSE, masking, error mapping.

Semua pengujian memakai FakeSession - TIDAK ada jaringan nyata.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import requests

from shortsgen.errors import ProviderError
from shortsgen.providers.ninerouter.client import NineRouterClient, parse_sse_chat
from shortsgen.providers.mock import write_solid_png
from pathlib import Path

API_KEY = "sk-unit-test-key-1234567890"


class FakeResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


class FakeSession:
    def __init__(self, queue: list):
        self.queue = list(queue)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def close(self) -> None:
        pass


def make_client(session: FakeSession, **kwargs: Any) -> NineRouterClient:
    return NineRouterClient(
        "http://127.0.0.1:20128",
        API_KEY,
        session=session,
        backoff_base=0.001,
        max_backoff_s=0.001,
        **kwargs,
    )


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


# -- dasar ------------------------------------------------------------

def test_health_and_models() -> None:
    session = FakeSession(
        [
            FakeResponse(200, content=b'{"ok":true}'),
            FakeResponse(
                200,
                content=json_bytes(
                    {
                        "object": "list",
                        "data": [
                            {"id": "kios/model-a"},
                            {"id": "ag/model-b"},
                            {"tanpa_id": True},
                        ],
                    }
                ),
            ),
        ]
    )
    client = make_client(session)
    assert client.health() == {"ok": True}
    models = client.list_models()
    assert [m["id"] for m in models] == ["kios/model-a", "ag/model-b"]
    # header Authorization terpasang
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"


def test_chat_json_response() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"Content-Type": "application/json"},
                content=json_bytes(
                    {
                        "model": "kios/model-a",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    }
                ),
            )
        ]
    )
    client = make_client(session)
    result = client.chat([{"role": "user", "content": "ping"}], "kios/model-a", max_tokens=8)
    assert result.text == "OK"
    assert result.transport == "json"
    assert result.usage["prompt_tokens"] == 5
    body = json.loads(session.calls[0]["data"])
    assert body["stream"] is False and body["max_tokens"] == 8


def test_chat_sse_response_even_when_stream_false() -> None:
    """Audit: provider gemini mengembalikan SSE walau stream=false."""
    sse = (
        'data: {"model":"gemini-3.8-flash","choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Ha"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":"berpikir..."}}]}\n\n'
        'data: {"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    session = FakeSession(
        [FakeResponse(200, headers={"Content-Type": "text/event-stream"}, content=sse.encode())]
    )
    client = make_client(session)
    result = client.chat([{"role": "user", "content": "hai"}], "gemini/model")
    assert result.transport == "sse"
    assert result.text == "Halo"
    assert result.model == "gemini-3.8-flash"
    assert result.usage["completion_tokens"] == 3


def test_parse_sse_handles_message_style_chunks() -> None:
    text = (
        'data: {"choices":[{"message":{"content":"full"}}]}\n'
        "data: tidak-json\n"
        "data: [DONE]\n"
    )
    parsed = parse_sse_chat(text)
    assert parsed.text == "full"
    assert parsed.transport == "sse"


# -- retry / error ----------------------------------------------------

def test_retry_on_429_then_success() -> None:
    session = FakeSession(
        [
            FakeResponse(429, content=json_bytes({"error": {"message": "rate limited", "code": "rate_limit"}})),
            FakeResponse(200, content=b'{"ok":true}'),
        ]
    )
    client = make_client(session)
    assert client.health() == {"ok": True}
    assert len(session.calls) == 2


def test_non_retryable_400_raises_immediately() -> None:
    session = FakeSession(
        [FakeResponse(400, content=json_bytes({"error": {"message": "Missing model", "code": "bad_request"}}))]
    )
    client = make_client(session)
    with pytest.raises(ProviderError) as exc_info:
        client.health()
    err = exc_info.value
    assert err.status == 400
    assert err.retryable is False
    assert "Missing model" in str(err)
    assert len(session.calls) == 1


def test_retries_exhausted_on_500() -> None:
    session = FakeSession([FakeResponse(500, content=b'{"error":{"message":"boom"}}') for _ in range(3)])
    client = make_client(session, max_retries=2)
    with pytest.raises(ProviderError) as exc_info:
        client.health()
    assert exc_info.value.retryable is True
    assert len(session.calls) == 3


def test_network_error_mapped_and_retried() -> None:
    session = FakeSession(
        [
            requests.exceptions.ConnectionError("refused"),
            FakeResponse(200, content=b'{"ok":true}'),
        ]
    )
    client = make_client(session)
    assert client.health() == {"ok": True}
    assert len(session.calls) == 2


def test_error_message_redacts_api_key() -> None:
    # Server (atau logger) tidak boleh membocorkan key walau ia meng-echo di pesan.
    session = FakeSession(
        [FakeResponse(400, content=json_bytes({"error": {"message": f"invalid key {API_KEY}"}}))]
    )
    client = make_client(session)
    with pytest.raises(ProviderError) as exc_info:
        client.health()
    assert API_KEY not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)


def test_retry_disabled_single_call() -> None:
    session = FakeSession([FakeResponse(503, content=b"down")])
    client = make_client(session)
    with pytest.raises(ProviderError):
        client.request("GET", "/api/health", retry=False)
    assert len(session.calls) == 1


# -- audio / image ----------------------------------------------------

def test_speech_returns_audio_bytes() -> None:
    mp3 = b"ID3\x04\x00\x00" + b"\x00" * 600
    session = FakeSession([FakeResponse(200, headers={"Content-Type": "audio/mp3"}, content=mp3)])
    client = make_client(session)
    assert client.speech("halo", "edge-tts/id-ID-ArdiNeural") == mp3
    body = json.loads(session.calls[0]["data"])
    assert body["model"] == "edge-tts/id-ID-ArdiNeural"


def test_chat_attempts_override_for_fast_fallback() -> None:
    """Chat memakai 1 percobaan per model (attempts=1) agar fallback cepat."""
    session = FakeSession([FakeResponse(500, content=b'{"error":{"message":"boom"}}')])
    client = make_client(session, max_retries=5)
    with pytest.raises(ProviderError):
        client.chat([{"role": "user", "content": "hi"}], "model-x", attempts=1)
    assert len(session.calls) == 1


def test_speech_rejects_non_audio() -> None:
    session = FakeSession([FakeResponse(200, content=b'{"not":"audio"}')])
    client = make_client(session)
    with pytest.raises(ProviderError) as exc_info:
        client.speech("halo", "edge-tts/id-ID-ArdiNeural")
    assert exc_info.value.kind == "bad_audio"


def test_images_b64_payload(tmp_path: Path) -> None:
    png_path = tmp_path / "src.png"
    write_solid_png(png_path, 8, 8, (255, 0, 0))
    b64 = base64.b64encode(png_path.read_bytes()).decode()
    session = FakeSession(
        [FakeResponse(200, content=json_bytes({"created": 1, "data": [{"b64_json": b64}]}))]
    )
    client = make_client(session)
    payload = client.images("lingkaran merah", "ag/gemini-3.1-flash-image")
    assert payload.mime == "image/png"
    assert payload.data.startswith(b"\x89PNG")


def test_images_bad_shape_raises() -> None:
    session = FakeSession([FakeResponse(200, content=json_bytes({"created": 1}))])
    client = make_client(session)
    with pytest.raises(ProviderError):
        client.images("x", "model")


def test_transcribe_multipart() -> None:
    audio = Path("dummy_audio.mp3")
    session = FakeSession([FakeResponse(200, content=json_bytes({"text": "halo dunia"}))])
    client = make_client(session)
    try:
        audio.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 100)
        result = client.transcribe("whisper-1", audio)
    finally:
        audio.unlink(missing_ok=True)
    assert result["text"] == "halo dunia"
    call = session.calls[0]
    assert "files" in call and "file" in call["files"]
    assert call["data"]["model"] == "whisper-1"


def test_search_and_fetch_payloads() -> None:
    session = FakeSession(
        [
            FakeResponse(200, content=json_bytes({"results": []})),
            FakeResponse(200, content=json_bytes({"text": "isi"})),
        ]
    )
    client = make_client(session)
    assert client.search("searxng", "mars", max_results=3) == {"results": []}
    assert client.web_fetch("jina", "https://example.com", max_chars=100)["text"] == "isi"
    search_body = json.loads(session.calls[0]["data"])
    assert search_body == {"provider": "searxng", "query": "mars", "max_results": 3}
    fetch_body = json.loads(session.calls[1]["data"])
    assert fetch_body["max_characters"] == 100


# -- gateway timeout (Cloudflare 52x) ---------------------------------

def test_524_is_retryable_and_retried() -> None:
    """Pesan 9Router 'Provider request failed with HTTP 524' harus di-retry."""
    session = FakeSession(
        [
            FakeResponse(
                524,
                content=json_bytes({"error": {"message": "Provider request failed with HTTP 524"}}),
            ),
            FakeResponse(200, content=b'{"ok":true}'),
        ]
    )
    client = make_client(session)
    assert client.health() == {"ok": True}
    assert len(session.calls) == 2


def test_524_error_is_explicit_gateway_timeout() -> None:
    session = FakeSession([FakeResponse(524, content=b"")])
    client = make_client(session)
    with pytest.raises(ProviderError) as exc_info:
        client.request("POST", "/v1/chat/completions", retry=False)
    err = exc_info.value
    assert err.status == 524
    assert err.kind == "gateway_timeout"
    assert err.retryable is True
    assert "524" in str(err)
    assert len(session.calls) == 1


def test_llm_chain_reports_every_failed_model() -> None:
    """Rantai fallback harus melaporkan semua model yang gagal (mis. 524 + 429)."""
    from shortsgen.providers.ninerouter.llm import LLMAdapter

    session = FakeSession(
        [
            FakeResponse(
                524,
                content=json_bytes({"error": {"message": "Provider request failed with HTTP 524"}}),
            ),
            FakeResponse(
                429,
                content=json_bytes({"error": {"message": "rate limited", "code": "rate_limit"}}),
            ),
        ]
    )
    client = make_client(session, max_retries=5)
    adapter = LLMAdapter(
        client,
        "kios/deepseek-v4.1-flash-free",
        ("openrouter/nvidia/nemotron-3.5-lightning:free",),
    )
    with pytest.raises(ProviderError) as exc_info:
        adapter.chat([{"role": "user", "content": "hi"}])
    err = exc_info.value
    assert err.kind == "all_failed"
    assert "kios/deepseek-v4.1-flash-free" in str(err)
    assert "openrouter/nvidia/nemotron-3.5-lightning:free" in str(err)
    assert "524" in str(err)
    # satu percobaan per model: tidak ada retry lama di dalam klien
    assert len(session.calls) == 2
