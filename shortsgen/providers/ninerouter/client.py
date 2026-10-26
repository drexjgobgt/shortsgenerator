"""Klien HTTP 9Router.

Hanya memakai endpoint yang sudah diverifikasi pada audit Fase A:
  GET  /api/health
  GET  /v1/models
  POST /v1/chat/completions        (JSON atau SSE)
  POST /v1/audio/speech            (biner audio)
  POST /v1/audio/transcriptions    (multipart)
  POST /v1/images/generations      (OpenAI-compatible, data[].b64_json / url)
  POST /v1/videos/generations      (async; status via /v1/videos/{id})
  POST /v1/search
  POST /v1/web/fetch

Karakteristik yang diverifikasi saat audit dan ditangani di sini:
- Autentikasi Bearer API key.
- Respons chat bisa JSON walau `stream=false` (beberapa provider mengembalikan SSE).
- Error 9Router: {"error": {"message": ..., "type": ..., "code": ...}}.
- Latensi bisa 30-180+ detik -> timeout longgar + retry terbatas.

Rahasia tidak pernah ditulis ke log/error: semua pesan melewati SecretRedactor.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import requests

from ...errors import NotConfiguredError, ProviderError
from ...util.secrets import SecretRedactor, mask

_RETRYABLE_STATUS = {
    408,
    409,
    425,
    429,
    500,
    502,
    503,
    504,
    520,  # Cloudflare: respons origin tidak dikenal
    521,  # Cloudflare: origin mati
    522,  # Cloudflare: koneksi ke origin timeout
    523,  # Cloudflare: origin tidak terjangkau
    524,  # Cloudflare: origin tidak merespons sebelum batas waktu
    525,  # Cloudflare: handshake SSL gagal
    526,  # Cloudflare: sertifikat SSL origin tidak valid
}

# Keluarga "gateway timeout": upstream lambat/putus sebelum menjawab (mis.
# KiosAPI di balik Cloudflare). Percobaan ulang pada model yang sama hampir
# pasti timeout lagi, jadi jeda backoff dipendekkan agar rantai fallback model
# cepat mengambil alih alih-alih menunggu lama.
_GATEWAY_TIMEOUT_STATUS = {522, 523, 524}
_GATEWAY_TIMEOUT_BACKOFF_S = 2.0
_AUDIO_MAGIC_PREFIXES = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa")


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed_s: float = 0.0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "") or self.headers.get("Content-Type", "")

    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text())


@dataclass
class ChatResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    transport: str = "json"          # json | sse
    latency_ms: int = 0
    raw: Optional[dict[str, Any]] = None


@dataclass
class ImagePayload:
    data: bytes
    mime: str
    model: str
    source: str = "b64_json"         # b64_json | url


class NineRouterClient:
    """Klien tipis dengan retry terbatas, timeout, dan penanganan error terpadu."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: int = 180,
        connect_timeout_s: int = 10,
        max_retries: int = 2,
        backoff_base: float = 1.5,
        max_backoff_s: float = 20.0,
        session: Optional[Any] = None,
        redactor: Optional[SecretRedactor] = None,
        logger: Optional[Any] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff_s = max_backoff_s
        self._session = session if session is not None else requests.Session()
        self.redactor = redactor or SecretRedactor([api_key] if api_key else [])
        if api_key:
            self.redactor.register(api_key)
        self.logger = logger

    # -- infrastruktur ------------------------------------------------

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if callable(closer):
            closer()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _log(self, level: str, message: str, **extra: Any) -> None:
        if self.logger is None:
            return
        safe = {k: (mask(v) if k in ("api_key",) else v) for k, v in extra.items()}
        getattr(self.logger, level, self.logger.info)(self.redactor.redact(message))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
        retry: bool = True,
        attempts_override: Optional[int] = None,
    ) -> HttpResponse:
        """Kirim request dengan retry terbatas; kembalikan HttpResponse.

        `attempts_override` memaksa jumlah percobaan (mis. 1 untuk chat agar
        rantai fallback model cepat mengambil alih daripada menunggu retry lama).

        Raises:
            ProviderError: untuk kegagalan HTTP final / jaringan.
        """
        url = self._url(path)
        headers = self._headers()
        payload_kwargs: dict[str, Any] = {}
        if files is not None:
            payload_kwargs["files"] = files
            if data:
                payload_kwargs["data"] = data
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
            payload_kwargs["data"] = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        attempts = (
            max(1, attempts_override)
            if attempts_override is not None
            else ((self.max_retries + 1) if retry else 1)
        )
        last_error: Optional[ProviderError] = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(self.connect_timeout_s, timeout_s or self.timeout_s),
                    **payload_kwargs,
                )
                elapsed = time.perf_counter() - started
                status = int(getattr(response, "status_code", 0))
                headers_in = {str(k): str(v) for k, v in dict(getattr(response, "headers", {}) or {}).items()}
                content = getattr(response, "content", b"") or b""

                if status >= 400:
                    message, kind = self._extract_error(status, content)
                    if status in _GATEWAY_TIMEOUT_STATUS:
                        # Pesan dari 9Router/upstream biasanya "Provider request failed
                        # with HTTP 524"; tambahkan konteks agar mudah didiagnosis.
                        kind = "gateway_timeout"
                        message = (
                            f"{message} — upstream/gateway timeout (HTTP {status}); "
                            "provider tidak menjawab tepat waktu"
                        )
                    message = self.redactor.redact(message)
                    retryable = status in _RETRYABLE_STATUS
                    last_error = ProviderError(
                        "9router", message, kind=kind, status=status, retryable=retryable
                    )
                    self._log(
                        "warning",
                        f"{method} {path} -> HTTP {status} ({kind}, attempt {attempt}/{attempts})",
                    )
                    if retryable and attempt < attempts:
                        self._sleep_backoff(attempt, headers_in, status=status)
                        continue
                    raise last_error

                return HttpResponse(
                    status=status, headers=headers_in, content=content, elapsed_s=elapsed
                )

            except ProviderError:
                raise
            except requests.exceptions.RequestException as exc:  # jaringan/timeout
                elapsed = time.perf_counter() - started
                last_error = ProviderError(
                    "9router",
                    f"kegagalan jaringan ke {self.base_url}: {exc.__class__.__name__}",
                    kind="network",
                    retryable=True,
                )
                self._log(
                    "warning",
                    f"{method} {path} jaringan gagal (attempt {attempt}/{attempts}, {elapsed:.1f}s)",
                )
                if attempt < attempts:
                    self._sleep_backoff(attempt, {})
                    continue
                raise last_error from exc

        raise last_error or ProviderError("9router", "request gagal tanpa detail", kind="unknown")

    def _sleep_backoff(
        self, attempt: int, headers: dict[str, str], *, status: Optional[int] = None
    ) -> None:
        delay = min(self.backoff_base ** attempt, self.max_backoff_s)
        if status in _GATEWAY_TIMEOUT_STATUS:
            # Timeout origin: jangan menunggu lama sebelum mencoba model/jalur lain.
            delay = min(delay, _GATEWAY_TIMEOUT_BACKOFF_S)
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                delay = min(float(retry_after), self.max_backoff_s)
                if status in _GATEWAY_TIMEOUT_STATUS:
                    delay = min(delay, _GATEWAY_TIMEOUT_BACKOFF_S)
            except ValueError:
                pass
        time.sleep(delay)

    @staticmethod
    def _extract_error(status: int, content: bytes) -> tuple[str, str]:
        """Ambil pesan error 9Router: {"error": {"message": ..., "type": ...}}."""
        text = content.decode("utf-8", "replace") if content else ""
        if not text:
            return f"HTTP {status} tanpa body", "http_error"
        try:
            parsed = json.loads(text)
            error = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("detail") or text)
                kind = str(error.get("code") or error.get("type") or "http_error")
                return message, kind
            return str(error), "http_error"
        except json.JSONDecodeError:
            return text[:300], "http_error"

    def download(self, url: str, *, max_bytes: int = 0) -> bytes:
        """Unduh URL hasil provider (mis. gambar) dengan batas ukuran."""
        response = self.request("GET", url, retry=True) if url.startswith(self.base_url) else None
        if response is None:
            try:
                raw = self._session.get(
                    url, timeout=(self.connect_timeout_s, self.timeout_s), stream=True
                )
                raw.raise_for_status()
                chunks = bytearray()
                for chunk in raw.iter_content(64 * 1024):
                    chunks += chunk
                    if max_bytes and len(chunks) > max_bytes:
                        raise ProviderError(
                            "9router",
                            f"aset melebihi batas {max_bytes} byte",
                            kind="asset_too_large",
                            retryable=False,
                        )
                return bytes(chunks)
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    "9router", f"gagal mengunduh aset: {exc.__class__.__name__}", kind="download"
                ) from exc
        return response.content

    # -- endpoint terverifikasi ---------------------------------------

    def health(self) -> dict[str, Any]:
        response = self.request("GET", "/api/health", retry=True)
        return response.json() if response.content else {}

    def list_models(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/v1/models", retry=True)
        payload = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        return [m for m in models if isinstance(m, dict) and m.get("id")]

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
        attempts: Optional[int] = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        started = time.perf_counter()
        response = self.request(
            "POST",
            "/v1/chat/completions",
            json_body=body,
            timeout_s=timeout_s,
            attempts_override=attempts,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = response.text()
        content_type = response.content_type.lower()
        if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
            parsed = parse_sse_chat(text)
            parsed.latency_ms = latency_ms
            parsed.model = parsed.model or model
            return parsed
        payload = json.loads(text) if text else {}
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ChatResponse(
            text=str(message.get("content") or ""),
            model=str(payload.get("model") or model),
            usage=dict(payload.get("usage") or {}),
            transport="json",
            latency_ms=latency_ms,
            raw=payload if isinstance(payload, dict) else None,
        )

    def speech(self, text: str, model: str, *, voice: Optional[str] = None) -> bytes:
        """TTS. Format model terverifikasi: `edge-tts/<voice>` atau `google-tts/<lang>`."""
        body: dict[str, Any] = {"model": model, "input": text}
        if voice:
            body["voice"] = voice
        response = self.request("POST", "/v1/audio/speech", json_body=body)
        content = response.content
        if not content:
            raise ProviderError("9router", "TTS mengembalikan audio kosong", kind="empty_audio")
        if not content.startswith(_AUDIO_MAGIC_PREFIXES):
            raise ProviderError(
                "9router",
                f"TTS mengembalikan data tak dikenal (content-type={response.content_type})",
                kind="bad_audio",
            )
        return content

    def transcribe(self, model: str, file_path: Any) -> dict[str, Any]:
        from pathlib import Path as _Path

        with open(file_path, "rb") as handle:
            files = {"file": (_Path(file_path).name, handle, "audio/mpeg")}
            response = self.request(
                "POST",
                "/v1/audio/transcriptions",
                files=files,
                data={"model": model},
            )
        return response.json() if response.content else {}

    def images(
        self,
        prompt: str,
        model: str,
        *,
        size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        reference_images: Optional[Sequence[str]] = None,
    ) -> ImagePayload:
        """Image generation.

        `reference_images` dan `aspect_ratio` telah diverifikasi live pada
        `ag/gemini-3.1-flash-image` (referensi menjaga konsistensi palet/subjek).
        """
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        if size:
            body["size"] = size
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if reference_images:
            body["reference_images"] = list(reference_images)
        response = self.request("POST", "/v1/images/generations", json_body=body, timeout_s=300)
        payload = response.json()
        items = payload.get("data") if isinstance(payload, dict) else None
        if not items or not isinstance(items, list):
            raise ProviderError("9router", "respons image tanpa data", kind="bad_response")
        first = items[0]
        import base64

        if first.get("b64_json"):
            raw = base64.b64decode(first["b64_json"])
            return ImagePayload(
                data=raw, mime=_guess_image_mime(raw), model=model, source="b64_json"
            )
        if first.get("url"):
            raw = self.download(str(first["url"]))
            return ImagePayload(data=raw, mime=_guess_image_mime(raw), model=model, source="url")
        raise ProviderError("9router", "respons image tanpa b64_json/url", kind="bad_response")

    def video(self, model: str, prompt: str, *, seconds: int = 5) -> dict[str, Any]:
        """Video generation (membutuhkan provider video yang terkonfigurasi).

        Catatan audit: pada instalasi ini belum ada provider video -> jalur ini
        belum teruji dengan provider nyata. Implementasi menolak dengan jelas
        bila respons berbentuk tidak dikenal (tidak menebak-nebak).
        """
        response = self.request(
            "POST",
            "/v1/videos/generations",
            json_body={"model": model, "prompt": prompt, "seconds": seconds},
            timeout_s=300,
        )
        return response.json() if response.content else {}

    def video_status(self, video_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/v1/videos/{video_id}", retry=False)
        return response.json() if response.content else {}

    def search(self, provider: str, query: str, *, max_results: int = 5) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/v1/search",
            json_body={"provider": provider, "query": query, "max_results": max_results},
        )
        return response.json() if response.content else {}

    def web_fetch(self, provider: str, url: str, *, max_chars: Optional[int] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"provider": provider, "url": url}
        if max_chars:
            body["max_characters"] = max_chars
        response = self.request("POST", "/v1/web/fetch", json_body=body)
        return response.json() if response.content else {}


def _guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def parse_sse_chat(text: str) -> ChatResponse:
    """Gabungkan chunk SSE `data:` menjadi ChatResponse.

    Ditangani karena audit menemukan respons SSE meski `stream=false`
    (provider gemini via 9Router).
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    model = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload_text = line[len("data:"):].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            chunk = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict):
            model = str(chunk.get("model") or model)
            if chunk.get("usage"):
                usage = dict(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                message = choice.get("message") or {}
                piece = delta.get("content") or message.get("content")
                if piece:
                    content_parts.append(str(piece))
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
    text_out = "".join(content_parts).strip()
    if reasoning_parts:
        usage.setdefault("reasoning_chars", len("".join(reasoning_parts)))
    return ChatResponse(text=text_out, model=model, usage=usage, transport="sse")


def ensure_configured(value: str, provider_name: str, capability: str) -> str:
    """Helper untuk adapter: tolak dengan NotConfiguredError bila nilai kosong."""
    if not value:
        raise NotConfiguredError(provider_name, capability)
    return value
