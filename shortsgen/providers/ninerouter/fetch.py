"""Adapter web fetch -> 9Router /v1/web/fetch.

STATUS AUDIT: provider fetch yang didukung (exa/firecrawl/jina/tavily/tinyfish)
semuanya memerlukan API key dan belum terhubung. Adapter aktif hanya bila
FETCH_PROVIDER diisi; parser defensif (bentuk respons belum terverifikasi).
"""
from __future__ import annotations

from typing import Any, Optional

from ...errors import NotConfiguredError, ProviderError
from ..base import FetchProvider, FetchResult, HealthStatus, ProviderHealth
from .client import NineRouterClient


class FetchAdapter:
    name = "9router"

    def __init__(self, client: NineRouterClient, provider: str, *, max_chars: int = 20_000):
        self.client = client
        self.provider = provider
        self.max_chars = max_chars

    def fetch(self, url: str, *, max_chars: Optional[int] = None) -> FetchResult:
        if not self.provider:
            raise NotConfiguredError(
                self.name, "fetch", "FETCH_PROVIDER kosong (tidak bisa mengambil halaman sumber)"
            )
        limit = max_chars or self.max_chars
        payload = self.client.web_fetch(self.provider, url, max_chars=limit)
        text = self._extract_text(payload, url)
        truncated = len(text) > limit
        return FetchResult(
            url=url,
            text=text[:limit],
            provider=self.provider,
            truncated=truncated,
            meta={"raw_keys": sorted(payload)[:8] if isinstance(payload, dict) else None},
        )

    @staticmethod
    def _extract_text(payload: dict[str, Any], url: str) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            raise ProviderError("9router", "respons fetch bukan objek", kind="bad_response")
        for key in ("text", "content", "markdown", "data", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                for inner in ("text", "content", "markdown"):
                    if isinstance(value.get(inner), str) and value[inner].strip():
                        return value[inner]
        raise ProviderError(
            "9router",
            f"bentuk respons fetch tidak dikenali untuk {url}: keys={sorted(payload)[:8]}",
            kind="bad_response",
        )

    def health(self) -> ProviderHealth:
        if not self.provider:
            return ProviderHealth(
                provider=self.name,
                capability="fetch",
                status=HealthStatus.NOT_CONFIGURED,
                detail="FETCH_PROVIDER kosong",
            )
        return ProviderHealth(
            provider=self.name,
            capability="fetch",
            status=HealthStatus.UNAVAILABLE,
            detail=f"provider '{self.provider}' dikonfigurasi tetapi belum terverifikasi berfungsi",
        )
