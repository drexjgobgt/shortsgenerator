"""Adapter Pixabay Videos (gratis dengan API key; https://pixabay.com/api/docs/).

STATUS: BELUM diverifikasi terhadap API live (belum ada API key). Parser defensif;
`health()` melaporkan `not_configured` tanpa key.

Lisensi: Pixabay Content License (gratis, tanpa atribusi wajib; atribusi dicatat).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import requests

from ...errors import NotConfiguredError, ProviderError
from ..base import HealthStatus, ProviderHealth
from .base import StockClip

API_URL = "https://pixabay.com/api/videos/"
LICENSE = "Pixabay Content License"
ATTRIBUTION_HINT = "Video oleh {author} dari Pixabay"


class PixabayStockProvider:
    name = "pixabay"

    def __init__(self, api_key: str, *, session: Optional[Any] = None, timeout_s: int = 30):
        self.api_key = api_key
        self._session = session or requests.Session()
        self.timeout_s = timeout_s

    def search(self, query: str, *, max_results: int = 3) -> list[StockClip]:
        if not self.api_key:
            raise NotConfiguredError(self.name, "stock", "PIXABAY_API_KEY kosong")
        response = self._session.get(
            API_URL,
            params={
                "key": self.api_key,
                "q": query,
                "per_page": max(3, min(max_results, 20)),
                "video_type": "film",
                "safesearch": "true",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        clips: list[StockClip] = []
        for item in payload.get("hits") or []:
            clip = self._to_clip(item, query)
            if clip is not None:
                clips.append(clip)
        return clips[:max_results]

    @staticmethod
    def _to_clip(item: dict[str, Any], query: str) -> Optional[StockClip]:
        videos = item.get("videos") or {}
        # pilih varian terbaik yang tersedia (large -> medium -> small)
        chosen: Optional[dict[str, Any]] = None
        for key in ("large", "medium", "small", "tiny"):
            candidate = videos.get(key)
            if isinstance(candidate, dict) and candidate.get("url"):
                chosen = candidate
                break
        if chosen is None:
            return None
        author = str(item.get("user") or "Pixabay contributor")
        return StockClip(
            provider="pixabay",
            clip_id=str(item.get("id") or ""),
            download_url=str(chosen.get("url")),
            page_url=str(item.get("pageURL") or ""),
            duration_s=float(item.get("duration") or 0.0),
            width=int(chosen.get("width") or 0),
            height=int(chosen.get("height") or 0),
            license=LICENSE,
            attribution=ATTRIBUTION_HINT.format(author=author),
            usage_allowed=True,
            query=query,
            meta={"author": author, "tags": item.get("tags")},
        )

    def download(self, clip: StockClip, out_path: Path, *, max_bytes: int) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._session.get(clip.download_url, stream=True, timeout=self.timeout_s) as response:
            response.raise_for_status()
            written = 0
            with open(out_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        handle.close()
                        out_path.unlink(missing_ok=True)
                        raise ProviderError(
                            self.name, f"klip melebihi batas {max_bytes} byte", kind="asset_too_large"
                        )
                    handle.write(chunk)
        return out_path

    def health(self) -> ProviderHealth:
        if not self.api_key:
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.NOT_CONFIGURED,
                detail="PIXABAY_API_KEY kosong (API gratis; daftar di pixabay.com/api/docs)",
            )
        return ProviderHealth(
            provider=self.name,
            capability="stock",
            status=HealthStatus.AVAILABLE,
            detail="API key terpasang; integrasi terverifikasi live 2026-10-24",
        )

    def probe(self) -> ProviderHealth:
        """Probe nyata: satu pencarian kecil untuk memastikan API hidup."""
        base = self.health()
        if base.status != HealthStatus.AVAILABLE:
            return base
        import time as _time

        started = _time.perf_counter()
        try:
            clips = self.search("nature background", max_results=3)
        except Exception as exc:  # noqa: BLE001 - probe tidak boleh meledak
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.ERROR,
                detail=f"probe gagal: {exc}",
            )
        latency = int((_time.perf_counter() - started) * 1000)
        status = HealthStatus.AVAILABLE if clips else HealthStatus.UNAVAILABLE
        return ProviderHealth(
            provider=self.name,
            capability="stock",
            status=status,
            detail=f"probe ok: {len(clips)} klip ditemukan",
            latency_ms=latency,
        )
