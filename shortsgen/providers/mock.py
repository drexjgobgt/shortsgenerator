"""Mock providers: sepenuhnya offline, deterministik, tanpa dependensi eksternal.

Dipakai oleh `APP_MODE=mock` dan seluruh unit test:
- MockLLM     : balasan deterministik; bisa mengembalikan JSON siap-parse
- MockTTS     : menulis WAV sine wave valid (stdlib `wave`)
- MockImage   : menulis PNG valid (encoder minimal stdlib zlib+struct)
- MockVideo   : SELALU NotConfiguredError -> melatih jalur fallback
- MockSearch  : hasil pencarian palsu yang jelas bertanda mock
- MockFetch   : dokumen palsu bertanda mock
- MockSTT     : transkrip palsu

Prinsip kejujuran: setiap keluaran mock diberi label "mock" agar tidak
tercampur dengan data nyata pada laporan.
"""
from __future__ import annotations

import json
import math
import struct
import time
import wave
import zlib
from pathlib import Path
from typing import Any, Optional, Sequence

from ..errors import ProviderError
from ..models import SearchHit
from .base import (
    ChatMessage,
    FetchResult,
    HealthStatus,
    ImageResult,
    LLMResult,
    ProviderHealth,
    TranscriptionResult,
    TTSResult,
    VideoResult,
)

MOCK_PROVIDER = "mock"


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class MockLLM:
    """LLM palsu. Deterministik berdasarkan pesan terakhir.

    Mengembalikan payload yang SESUAI SKEMA berdasarkan penanda di prompt:
    - prompt riset (berisi '"facts"' + '"search_queries"')  -> payload riset
    - prompt naskah (berisi '"scenes"' + '"hook"')          -> payload naskah
    - prompt JSON lain                                       -> payload generik
    `responses` (antrean) memungkinkan skenario berurutan dalam pengujian.
    """

    name = MOCK_PROVIDER

    def __init__(self, fixed_response: Optional[str] = None, responses: Optional[list[str]] = None):
        self.fixed_response = fixed_response
        self._queue: list[str] = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        # Untuk pengujian kegagalan: set N>0 agar N panggilan berikutnya gagal.
        self.fail_times: int = 0
        self.fail_retryable: bool = True

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        start = time.perf_counter()
        self.calls.append(
            {"messages": list(messages), "model": model, "temperature": temperature}
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError(
                MOCK_PROVIDER,
                "kegagalan mock yang disengaja",
                kind="mock_failure",
                retryable=self.fail_retryable,
            )
        prompt = " ".join(m.get("content", "") for m in messages)
        if self._queue:
            text = self._queue.pop(0)
        elif self.fixed_response is not None:
            text = self.fixed_response
        elif '"facts"' in prompt and '"search_queries"' in prompt:
            text = json.dumps(MOCK_RESEARCH_RESPONSE, ensure_ascii=False)
        elif ('"scenes"' in prompt or '"beats"' in prompt) and '"hook"' in prompt:
            text = json.dumps(MOCK_SCRIPT_RESPONSE, ensure_ascii=False)
        elif "json" in prompt.lower():
            text = json.dumps(
                {
                    "mock": True,
                    "title": "Contoh naskah mock",
                    "hook": "Tahukah kamu? Ini contoh hook dari mode mock.",
                    "scenes": [
                        {"narration": "Scene satu dari mode mock.", "visual": "ilustrasi mock"},
                        {"narration": "Scene dua dari mode mock.", "visual": "ilustrasi mock"},
                    ],
                },
                ensure_ascii=False,
            )
        else:
            text = "Balasan mock: konten ini dibuat tanpa memanggil jaringan apa pun."
        return LLMResult(
            text=text,
            model=model or "mock-llm",
            provider=MOCK_PROVIDER,
            usage={"input_tokens": len(prompt.split()), "output_tokens": len(text.split())},
            transport="json",
            latency_ms=_ms(start),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER,
            capability="llm",
            status=HealthStatus.AVAILABLE,
            detail="mock offline (tanpa jaringan)",
        )


# Payload mock yang menyerupai keluaran skema nyata (dipakai stage Fase C).
MOCK_RESEARCH_RESPONSE: dict[str, Any] = {
    "topic_summary": "Warna langit ditentukan oleh komposisi atmosfer dan cara cahaya dihamburkan.",
    "facts": [
        {
            "claim": "Langit Bumi tampak biru karena hamburan Rayleigh cahaya matahari oleh molekul udara.",
            "detail": "Panjang gelombang biru dihamburkan lebih kuat daripada merah.",
            "source_name": None,
            "source_url": None,
            "confidence": "high",
            "notes": "Fakta fisika dasar.",
        },
        {
            "claim": "Atmosfer Mars jauh lebih tipis, sekitar satu persen tekanan atmosfer Bumi.",
            "source_name": None,
            "source_url": None,
            "confidence": "high",
        },
        {
            "claim": "Debu halus mengandung oksida besi yang memberi warna kemerahan pada Mars.",
            "confidence": "medium",
            "source_url": None,
        },
        {
            "claim": "Debu di atmosfer Mars menghamburkan cahaya merah dan oranye sehingga langit siang tampak kekuningan.",
            "confidence": "medium",
            "source_url": None,
        },
        {
            "claim": "Saat matahari terbenam di Mars, langit di sekitar matahari dapat tampak kebiruan.",
            "confidence": "low",
            "source_url": None,
        },
    ],
    "uncertainties": ["Sumber resmi perlu diverifikasi manual karena search/fetch belum dikonfigurasi."],
    "search_queries": ["warna langit Mars", "hamburan cahaya atmosfer Mars", "debu oksida besi Mars"],
}

MOCK_SCRIPT_RESPONSE: dict[str, Any] = {
    "title": "Kenapa Langit Mars Merah, Tapi Senjanya Biru?",
    "hook": "Langit Mars berwarna merah, tapi matahari terbenamnya justru biru.",
    "hook_variants": [
        "Langit Mars berwarna merah, tapi matahari terbenamnya justru biru.",
        "Senja di Mars berwarna biru. Kok bisa?",
        "Bumi punya langit biru, Mars punya senja biru.",
    ],
    "scenes": [
        {
            "narration": "Dan di akhir, ada satu fakta yang jarang dibahas.",
            "on_screen_text": "Fakta terakhir",
            "visual_prompt": "mysterious red planet horizon, subtle glow, cinematic",
            "stock_query": "mars surface panorama",
        },
        {
            "narration": "Di Bumi, molekul udara menghamburkan cahaya biru ke segala arah.",
            "on_screen_text": "Langit biru Bumi",
            "visual_prompt": "clear blue sky above green field, bright sunlight",
            "stock_query": "blue sky clouds",
        },
        {
            "narration": "Mars hanya punya satu persen tekanan udara Bumi.",
            "on_screen_text": "Atmosfer tipis",
            "visual_prompt": "thin atmosphere cross section diagram, red planet",
            "stock_query": "planet surface aerial",
        },
        {
            "narration": "Debu oksida besi memenuhi atmosfer tipis itu.",
            "on_screen_text": "Debu besi",
            "visual_prompt": "microscopic dust particles, iron oxide red, science",
            "stock_query": "red sand closeup",
        },
        {
            "narration": "Debu inilah yang menghamburkan warna merah dan oranye.",
            "on_screen_text": "Hamburan merah",
            "visual_prompt": "sunlight scattering through dusty air, orange glow",
            "stock_query": "sunset sunlight sky",
        },
        {
            "narration": "Pernahkah kamu membayangkan berdiri di permukaannya?",
            "on_screen_text": "Bayangkan",
            "visual_prompt": "astronaut footprint on red dusty surface, no face",
            "stock_query": "space exploration",
        },
        {
            "narration": "Saat senja, debu menghamburkan cahaya biru ke arah berlawanan.",
            "on_screen_text": "Senja biru",
            "visual_prompt": "sunset on dusty planet, blue glow around sun",
            "stock_query": "blue sunset sky",
        },
    ],
    "closing": "Jadi langit merah, senja biru: kebalikan dari Bumi.",
    "description": "Langit Mars merah karena debu oksida besi, tapi senjanya justru biru. "
    "Penjelasan singkat dalam 30 detik. #mars #sains #antariksa #faktaunik",
    "tags": [
        "mars", "sains", "antariksa", "fakta unik", "edukasi", "langit", "atmosfer",
        "planet", "astronomi", "belajar", "indonesia",
    ],
    "facts_used": ["F1", "F2", "F3", "F4", "F5"],
    "disclosure": "Video memakai narasi suara sintetis dan visual yang dibuat/diproses secara digital.",
}


class MockTTS:
    """TTS palsu: menulis WAV sine wave yang bisa diputar/dibaca ffprobe."""

    name = MOCK_PROVIDER
    SAMPLE_RATE = 22050

    def __init__(self, words_per_minute: int = 120):
        self.words_per_minute = words_per_minute

    def synthesize(self, text: str, out_path: Path, *, voice: Optional[str] = None) -> TTSResult:
        start = time.perf_counter()
        words = max(1, len(text.split()))
        duration_s = words / (self.words_per_minute / 60.0)
        frames = int(duration_s * self.SAMPLE_RATE)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.SAMPLE_RATE)
            freq = 220.0  # nada konstan; isi tidak penting untuk mock
            amplitude = 8000
            buf = bytearray()
            for i in range(frames):
                sample = int(amplitude * math.sin(2 * math.pi * freq * i / self.SAMPLE_RATE))
                buf += struct.pack("<h", sample)
            wav.writeframes(bytes(buf))
        return TTSResult(
            path=out_path,
            provider=MOCK_PROVIDER,
            model=f"mock-tts/{voice or 'default'}",
            bytes_written=out_path.stat().st_size,
            latency_ms=_ms(start),
            meta={"duration_s": round(duration_s, 3), "mock": True},
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER,
            capability="tts",
            status=HealthStatus.AVAILABLE,
            detail="mock offline (WAV sine)",
        )


class MockSTT:
    name = MOCK_PROVIDER

    def __init__(self, text: str = "[transkrip mock] teks contoh hasil mock STT"):
        self.text = text

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        start = time.perf_counter()
        if not Path(audio_path).exists():
            raise ProviderError(MOCK_PROVIDER, f"file audio tidak ada: {audio_path}", kind="bad_input")
        return TranscriptionResult(
            text=self.text,
            provider=MOCK_PROVIDER,
            model="mock-stt",
            latency_ms=_ms(start),
            meta={"mock": True},
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER, capability="stt", status=HealthStatus.AVAILABLE, detail="mock"
        )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_solid_png(path: Path, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    """Encoder PNG minimal (RGB8, tanpa filter) - cukup untuk mock/test."""
    r, g, b = rgb
    raw = bytearray()
    row_bytes = bytes([r, g, b]) * width
    for _ in range(height):
        raw.append(0)          # filter type 0
        raw += row_bytes
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


class MockImage:
    """Gambar palsu: PNG bergradien + teks prompt (representatif untuk pratinjau).

    Tetap deterministik dan tanpa jaringan. Bila Pillow tidak tersedia,
    jatuh kembali ke encoder PNG stdlib (warna solid).
    """

    name = MOCK_PROVIDER

    def __init__(self, width: int = 1024, height: int = 1024):
        self.width = width
        self.height = height

    def generate(
        self,
        prompt: str,
        out_path: Path,
        *,
        size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        reference_image: Optional[Path] = None,
    ) -> ImageResult:
        start = time.perf_counter()
        out = Path(out_path)
        width, height = self.width, self.height
        if size and "x" in size:
            try:
                width, height = (int(v) for v in size.lower().split("x", 1))
            except ValueError:
                pass
        elif aspect_ratio in ("9:16", "9/16"):
            width, height = 720, 1280
        try:
            from ..media.graphics import create_background

            create_background(
                out,
                width=width,
                height=height,
                seed_text=prompt,
                title="(mock) " + (prompt[:70] or "visual"),
                text_safe_scale=0.44 if width <= height else 0.9,
                text_vertical=0.5 if width <= height else 0.34,
            )
        except Exception:
            # fallback stdlib: warna solid deterministik dari prompt
            digest = zlib.crc32(prompt.encode("utf-8")) if prompt else 0
            rgb = ((digest >> 16) & 0xFF, (digest >> 8) & 0xFF, digest & 0xFF)
            write_solid_png(out, width, height, rgb)
        return ImageResult(
            path=out,
            provider=MOCK_PROVIDER,
            model="mock-image",
            mime="image/png",
            bytes_written=out.stat().st_size,
            latency_ms=_ms(start),
            meta={"mock": True, "prompt": prompt[:200]},
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER, capability="image", status=HealthStatus.AVAILABLE, detail="mock"
        )


class MockVideo:
    """Video AI mock = selalu not_configured (melatih fallback ke animasi FFmpeg)."""

    name = MOCK_PROVIDER

    def generate(self, prompt: str, out_path: Path, *, seconds: int = 5) -> VideoResult:
        from ..errors import NotConfiguredError

        raise NotConfiguredError(
            MOCK_PROVIDER, "video", "video AI tidak tersedia di mode mock (fallback: gambar + FFmpeg)"
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER,
            capability="video",
            status=HealthStatus.NOT_CONFIGURED,
            detail="mock: video selalu not_configured (uji fallback)",
        )


class MockSearch:
    name = MOCK_PROVIDER

    def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        return [
            SearchHit(
                title=f"[MOCK] Hasil {i + 1} untuk: {query}",
                url=f"https://example.invalid/mock/{i + 1}",
                snippet="Hasil mock - bukan sumber nyata; jangan dipakai untuk verifikasi fakta.",
                provider=MOCK_PROVIDER,
            )
            for i in range(min(max_results, 3))
        ]

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER, capability="search", status=HealthStatus.AVAILABLE, detail="mock"
        )


class MockFetch:
    name = MOCK_PROVIDER

    def fetch(self, url: str, *, max_chars: Optional[int] = None) -> FetchResult:
        text = (
            "[MOCK] Dokumen contoh yang diambil secara lokal tanpa jaringan. "
            f"URL yang diminta: {url}. Konten ini tidak berasal dari internet."
        )
        truncated = False
        if max_chars and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return FetchResult(url=url, text=text, provider=MOCK_PROVIDER, truncated=truncated)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=MOCK_PROVIDER, capability="fetch", status=HealthStatus.AVAILABLE, detail="mock"
        )
