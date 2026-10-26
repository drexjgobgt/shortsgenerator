"""Provider TTS lokal berbasis edge-tts (prosody nyata, tanpa biaya API).

Kenapa lokal:
- 9Router `/v1/audio/speech` hanya meneruskan `input`, `language`, `style`
  (diverifikasi dari build 9Router v0.5.109). Parameter `rate/pitch/volume`
  TIDAK diteruskan ke edge-tts, sehingga emosi tidak bisa dikontrol.
- Paket `edge_tts` lokal mendukung `rate`, `pitch`, `volume` (diverifikasi live:
  F0 105-147 Hz antar preset emosi).

Batasan yang diakui:
- Ini TTS neural, bukan voice actor manusia. Emosi dibuat dari prosody
  (kecepatan, pitch, jeda), bukan akting penuh.
- Butuh koneksi internet ke layanan Edge TTS saat sintesis (bukan saat health).
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from typing import Any, Optional

from ..errors import NotConfiguredError, ProviderError
from .base import HealthStatus, ProviderHealth, TTSResult


def edge_tts_available() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


class EdgeTTSLocal:
    name = "edge-tts-local"

    def __init__(
        self,
        *,
        default_voice: str = "id-ID-ArdiNeural",
        timeout_s: int = 60,
        fallback: Optional[Any] = None,
        cache: Optional[Any] = None,
    ):
        self.default_voice = default_voice
        self.timeout_s = timeout_s
        self.fallback = fallback
        self.cache = cache

    def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        volume: str = "+0%",
    ) -> TTSResult:
        if not edge_tts_available():
            if self.fallback is not None:
                return self._fallback(text, out_path, voice, reason="edge-tts tidak terpasang")
            raise NotConfiguredError(
                self.name,
                "tts",
                "paket edge-tts tidak terpasang (pip install edge-tts)",
            )
        if not str(text).strip():
            raise ProviderError(self.name, "teks kosong", kind="bad_input")
        chosen = voice or self.default_voice
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cache_key = {
            "text": text,
            "voice": chosen,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
        }
        if self.cache is not None:
            cached = self.cache.get_bytes(cache_key)
            if cached and len(cached) >= 512:
                out_path.write_bytes(cached)
                return TTSResult(
                    path=out_path,
                    provider=self.name,
                    model=f"edge-tts/{chosen}",
                    bytes_written=out_path.stat().st_size,
                    latency_ms=0,
                    meta={
                        "voice": chosen,
                        "rate": rate,
                        "pitch": pitch,
                        "volume": volume,
                        "prosody": True,
                        "cached": True,
                    },
                )
        started = time.perf_counter()
        try:
            asyncio.run(self._save(text, out_path, chosen, rate, pitch, volume))
        except RuntimeError as exc:
            # asyncio.run di dalam event loop yang sudah berjalan
            if "asyncio.run() cannot be called" in str(exc):
                raise ProviderError(
                    self.name,
                    "sintesis dipanggil di dalam event loop aktif; gunakan thread terpisah",
                    kind="bad_context",
                ) from exc
            if self.fallback is not None:
                return self._fallback(text, out_path, voice, reason=str(exc))
            raise ProviderError(self.name, f"edge-tts gagal: {exc}", kind="tts_failed") from exc
        except Exception as exc:  # edge_tts melempar berbagai exception jaringan
            if self.fallback is not None:
                return self._fallback(text, out_path, voice, reason=f"{exc.__class__.__name__}: {exc}")
            raise ProviderError(
                self.name, f"edge-tts gagal: {exc.__class__.__name__}: {exc}", kind="tts_failed"
            ) from exc
        if not out_path.exists() or out_path.stat().st_size == 0:
            if self.fallback is not None:
                return self._fallback(text, out_path, voice, reason="berkas kosong")
            raise ProviderError(self.name, "edge-tts menghasilkan berkas kosong", kind="empty_audio")
        latency_ms = int((time.perf_counter() - started) * 1000)
        if self.cache is not None:
            self.cache.set_bytes(cache_key, out_path.read_bytes())
        return TTSResult(
            path=out_path,
            provider=self.name,
            model=f"edge-tts/{chosen}",
            bytes_written=out_path.stat().st_size,
            latency_ms=latency_ms,
            meta={"voice": chosen, "rate": rate, "pitch": pitch, "volume": volume, "prosody": True},
        )

    def _fallback(
        self, text: str, out_path: Path, voice: Optional[str], *, reason: str
    ) -> TTSResult:
        result = self.fallback.synthesize(text, out_path, voice=voice)
        result.meta = dict(result.meta or {})
        result.meta.update({"prosody": False, "fallback_reason": reason[:200]})
        return result

    async def _save(
        self,
        text: str,
        out_path: Path,
        voice: str,
        rate: str,
        pitch: str,
        volume: str,
    ) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
            connect_timeout=10,
            receive_timeout=self.timeout_s,
        )
        await communicate.save(str(out_path))

    def health(self) -> ProviderHealth:
        if not edge_tts_available():
            return ProviderHealth(
                provider=self.name,
                capability="tts",
                status=HealthStatus.NOT_CONFIGURED,
                detail="paket edge-tts tidak terpasang -> fallback 9Router/mock",
            )
        return ProviderHealth(
            provider=self.name,
            capability="tts",
            status=HealthStatus.AVAILABLE,
            detail=f"edge-tts lokal; voice default {self.default_voice}; prosody rate/pitch/volume",
            meta={"default_voice": self.default_voice, "prosody": True},
        )
