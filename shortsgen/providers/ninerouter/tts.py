"""Adapter TTS -> 9Router /v1/audio/speech.

Format model terverifikasi:
  - `edge-tts/<voice>`  (mis. edge-tts/id-ID-ArdiNeural) - gratis, noAuth
  - `google-tts/<lang>` (mis. google-tts/id)             - gratis, noAuth
Parameter `voice` yang diberikan eksplisit menggantikan bagian voice/lang.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...errors import NotConfiguredError, ProviderError
from ..base import HealthStatus, ProviderHealth, TTSProvider, TTSResult
from .client import NineRouterClient


class TTSAdapter:
    name = "9router"

    def __init__(
        self,
        client: NineRouterClient,
        model: str,
        *,
        default_voice: Optional[str] = None,
        cache: Optional[object] = None,
    ):
        self.client = client
        self.model = model
        self.default_voice = default_voice
        self.cache = cache

    # -- helpers ------------------------------------------------------

    def model_for(self, voice: Optional[str]) -> str:
        chosen = voice or self.default_voice
        if not chosen:
            return self.model
        base = (self.model or "").split("/", 1)[0]
        if base in ("edge-tts", "google-tts"):
            return f"{base}/{chosen}"
        if "/" in self.model:
            return f"{self.model.split('/', 1)[0]}/{chosen}"
        return chosen

    def synthesize(self, text: str, out_path: Path, *, voice: Optional[str] = None) -> TTSResult:
        if not self.model and not voice:
            raise NotConfiguredError(self.name, "tts", "TTS_MODEL kosong")
        model = self.model_for(voice)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import time

        cache_key = {"text": text, "model": model}
        if self.cache is not None:
            cached = self.cache.get_bytes(cache_key)
            if cached and len(cached) >= 512:
                out_path.write_bytes(cached)
                return TTSResult(
                    path=out_path,
                    provider=self.name,
                    model=model,
                    bytes_written=out_path.stat().st_size,
                    latency_ms=0,
                    meta={"cached": True, "voice": model.split("/", 1)[1] if "/" in model else None},
                )
        started = time.perf_counter()
        audio = self.client.speech(text, model)
        latency_ms = int((time.perf_counter() - started) * 1000)
        out_path.write_bytes(audio)
        if out_path.stat().st_size < 512:
            raise ProviderError(
                self.name, "audio TTS terlalu kecil, kemungkinan rusak", kind="bad_audio", model=model
            )
        if self.cache is not None:
            self.cache.set_bytes(cache_key, audio)
        return TTSResult(
            path=out_path,
            provider=self.name,
            model=model,
            bytes_written=out_path.stat().st_size,
            latency_ms=latency_ms,
            meta={"voice": model.split("/", 1)[1] if "/" in model else None},
        )

    def health(self) -> ProviderHealth:
        if not self.model:
            return ProviderHealth(
                provider=self.name,
                capability="tts",
                status=HealthStatus.NOT_CONFIGURED,
                detail="TTS_MODEL kosong",
            )
        return ProviderHealth(
            provider=self.name,
            capability="tts",
            status=HealthStatus.AVAILABLE,
            detail=f"model: {self.model}",
        )

    def probe(self) -> ProviderHealth:
        """Probe murah: uji sintesis kata pendek ke file temporer."""
        import tempfile

        base = self.health()
        if base.status != HealthStatus.AVAILABLE:
            return base
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = self.synthesize("ok", Path(tmp) / "probe.mp3")
            return ProviderHealth(
                provider=self.name,
                capability="tts",
                status=HealthStatus.AVAILABLE,
                detail=f"probe ok ({result.bytes_written} byte, {result.model})",
                latency_ms=result.latency_ms,
            )
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name,
                capability="tts",
                status=HealthStatus.ERROR,
                detail=f"probe gagal: {exc}",
            )
