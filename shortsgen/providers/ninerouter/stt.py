"""Adapter STT -> 9Router /v1/audio/transcriptions (multipart, format OpenAI).

STATUS AUDIT: `not_configured` pada instalasi ini (OpenAI tanpa kredensial,
provider kios chat-only, Deepgram/ElevenLabs tanpa kunci, local-device tidak ada).
Pipeline subtitle TIDAK bergantung pada STT - timing diturunkan dari skrip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...errors import NotConfiguredError, ProviderError
from ..base import HealthStatus, ProviderHealth, STTProvider, TranscriptionResult
from .client import NineRouterClient


class STTAdapter:
    name = "9router"

    def __init__(self, client: NineRouterClient, model: str):
        self.client = client
        self.model = model

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if not self.model:
            raise NotConfiguredError(self.name, "stt", "STT_MODEL kosong")
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ProviderError(self.name, f"file audio tidak ditemukan: {audio_path}", kind="bad_input")
        payload: dict[str, Any] = self.client.transcribe(self.model, audio_path)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProviderError(
                self.name, "respons transkripsi tanpa teks", kind="empty_response", model=self.model
            )
        return TranscriptionResult(text=text, provider=self.name, model=self.model, meta=payload)

    def health(self) -> ProviderHealth:
        if not self.model:
            return ProviderHealth(
                provider=self.name,
                capability="stt",
                status=HealthStatus.NOT_CONFIGURED,
                detail="STT_MODEL kosong (subtitle memakai timing skrip, bukan STT)",
            )
        return ProviderHealth(
            provider=self.name,
            capability="stt",
            status=HealthStatus.UNAVAILABLE,
            detail="model dikonfigurasi tetapi belum terverifikasi berfungsi",
        )
