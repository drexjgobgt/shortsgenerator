"""Adapter video generation -> 9Router /v1/videos/generations.

STATUS AUDIT: tidak ada provider video yang terkonfigurasi pada instalasi ini
(xai tanpa kredensial; gemini/kios tidak mendukung). Adapter dibuat lengkap
tetapi jalur async-nya BELUM teruji dengan provider nyata dan tidak boleh
diklaim berfungsi. Bila respons tidak dikenali, error dilempar eksplisit.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

from ...errors import NotConfiguredError, ProviderError
from ..base import HealthStatus, ProviderHealth, VideoProvider, VideoResult
from .client import NineRouterClient


class VideoAdapter:
    name = "9router"

    def __init__(
        self,
        client: NineRouterClient,
        model: str,
        *,
        poll_interval_s: float = 5.0,
        max_polls: int = 24,
        max_bytes: int = 500 * 1024 * 1024,
    ):
        self.client = client
        self.model = model
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self.max_bytes = max_bytes

    def generate(self, prompt: str, out_path: Path, *, seconds: int = 5) -> VideoResult:
        if not self.model:
            raise NotConfiguredError(
                self.name, "video", "VIDEO_MODEL kosong (fallback: gambar AI + animasi FFmpeg)"
            )
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        payload = self.client.video(self.model, prompt, seconds=seconds)
        data_bytes, source = self._extract_video_bytes(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if len(data_bytes) > self.max_bytes:
            raise ProviderError(self.name, "video melebihi batas ukuran", kind="asset_too_large")
        out_path.write_bytes(data_bytes)
        return VideoResult(
            path=out_path,
            provider=self.name,
            model=self.model,
            bytes_written=out_path.stat().st_size,
            latency_ms=latency_ms,
            meta={"source": source},
        )

    def _extract_video_bytes(self, payload: dict) -> tuple[bytes, str]:
        """Ambil byte video dari respons; tangani bentuk sync (b64/url) dan async (id)."""
        if not isinstance(payload, dict):
            raise ProviderError(self.name, "respons video bukan objek JSON", kind="bad_response")
        items = payload.get("data") or payload.get("videos") or []
        first = items[0] if isinstance(items, list) and items else {}
        if isinstance(first, dict):
            if first.get("b64_json"):
                return base64.b64decode(first["b64_json"]), "b64_json"
            if first.get("url"):
                return self.client.download(str(first["url"])), "url"
        video_id = payload.get("id") or first.get("id")
        if video_id:
            for _ in range(self.max_polls):
                time.sleep(self.poll_interval_s)
                status = self.client.video_status(str(video_id))
                state = str(status.get("status", "")).lower()
                if state in ("succeeded", "completed", "done"):
                    return self._extract_video_bytes(status), "poll"
                if state in ("failed", "error", "canceled"):
                    raise ProviderError(
                        self.name,
                        f"video generation gagal di provider: {status.get('error') or state}",
                        kind="generation_failed",
                    )
            raise ProviderError(
                self.name, "video generation melewati batas polling", kind="timeout", retryable=True
            )
        raise ProviderError(
            self.name,
            "bentuk respons video tidak dikenali (provider video belum terverifikasi)",
            kind="bad_response",
        )

    def health(self) -> ProviderHealth:
        if not self.model:
            return ProviderHealth(
                provider=self.name,
                capability="video",
                status=HealthStatus.NOT_CONFIGURED,
                detail="VIDEO_MODEL kosong -> fallback animasi FFmpeg",
            )
        return ProviderHealth(
            provider=self.name,
            capability="video",
            status=HealthStatus.UNAVAILABLE,
            detail="model dikonfigurasi tetapi provider video belum diverifikasi di instalasi ini",
            meta={"model": self.model},
        )
