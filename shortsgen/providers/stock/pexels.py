"""Adapter Pexels Videos (gratis dengan API key; https://www.pexels.com/api/).

STATUS: implementasi ini BELUM diverifikasi terhadap API live karena belum ada
API key di mesin ini (lihat docs/9router-compatibility-report.md). Parser dibuat
defensif dan `health()` melaporkan `not_configured` tanpa key.

Lisensi: Pexels License (gratis untuk penggunaan komersial, tanpa atribusi wajib,
tetapi atribusi tetap dicatat sebagai praktik baik).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import requests

from ..base import HealthStatus, ProviderHealth
from .base import StockClip

API_URL = "https://api.pexels.com/videos/search"
LICENSE = "Pexels License (gratis untuk penggunaan komersial)"
ATTRIBUTION_HINT = "Video oleh {author} di Pexels"


class PexelsStockProvider:
    name = "pexels"

    def __init__(self, api_key: str, *, session: Optional[Any] = None, timeout_s: int = 30):
        self.api_key = api_key
        self._session = session or requests.Session()
        self.timeout_s = timeout_s

    # -- API -----------------------------------------------------------

    def search(self, query: str, *, max_results: int = 3) -> list[StockClip]:
        if not self.api_key:
            from ...errors import NotConfiguredError

            raise NotConfiguredError(self.name, "stock", "PEXELS_API_KEY kosong")
        response = self._session.get(
            API_URL,
            params={"query": query, "per_page": max(1, min(max_results, 15)), "orientation": "portrait"},
            headers={"Authorization": self.api_key},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        clips: list[StockClip] = []
        for item in payload.get("videos") or []:
            clip = self._to_clip(item, query)
            if clip is not None:
                clips.append(clip)
        return clips

    @staticmethod
    def _to_clip(item: dict[str, Any], query: str) -> Optional[StockClip]:
        files = item.get("video_files") or []
        if not files:
            return None
        # pilih file portrait kualitas menengah (lebih ringan & cocok 9:16)
        portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
        candidates = portrait or files
        candidates = sorted(
            candidates,
            key=lambda f: abs((f.get("height") or 0) - 1920) + abs((f.get("width") or 0) - 1080),
        )
        best = candidates[0]
        link = best.get("link")
        if not link:
            return None
        author = str((item.get("user") or {}).get("name") or "Pexels contributor")
        return StockClip(
            provider="pexels",
            clip_id=str(item.get("id") or ""),
            download_url=str(link),
            page_url=str(item.get("url") or ""),
            duration_s=float(item.get("duration") or 0.0),
            width=int(best.get("width") or 0),
            height=int(best.get("height") or 0),
            license=LICENSE,
            attribution=ATTRIBUTION_HINT.format(author=author),
            usage_allowed=True,
            query=query,
            meta={"quality": best.get("quality"), "author": author},
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
                        from ...errors import ProviderError

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
                detail="PEXELS_API_KEY kosong (API gratis; daftar di pexels.com/api)",
            )
        return ProviderHealth(
            provider=self.name,
            capability="stock",
            status=HealthStatus.UNAVAILABLE,
            detail="API key ada; integrasi belum diverifikasi terhadap API live",
        )

    def probe(self) -> ProviderHealth:
        """Probe nyata: satu pencarian kecil (dipakai doctor --deep)."""
        base = self.health()
        if base.status == HealthStatus.NOT_CONFIGURED:
            return base
        import time as _time

        started = _time.perf_counter()
        try:
            clips = self.search("nature background", max_results=3)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.ERROR,
                detail=f"probe gagal: {exc}",
            )
        latency = int((_time.perf_counter() - started) * 1000)
        return ProviderHealth(
            provider=self.name,
            capability="stock",
            status=HealthStatus.AVAILABLE if clips else HealthStatus.UNAVAILABLE,
            detail=f"probe ok: {len(clips)} klip ditemukan (integrasi kini terverifikasi)",
            latency_ms=latency,
        )
