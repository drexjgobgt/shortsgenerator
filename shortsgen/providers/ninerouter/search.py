"""Adapter web search -> 9Router /v1/search.

STATUS AUDIT: tidak ada provider search yang siap pakai (provider cloud tanpa
kredensial; `searxng` noAuth tetapi diblokir guard internal-host 9Router).
Adapter hanya aktif bila SEARCH_PROVIDER diisi; bentuk respons belum terverifikasi
sehingga parser bersifat defensif dan gagal eksplisit bila tidak dikenali.
"""
from __future__ import annotations

from typing import Any, Optional

from ...errors import NotConfiguredError, ProviderError
from ...models import SearchHit
from ..base import HealthStatus, ProviderHealth, SearchProvider
from .client import NineRouterClient


class SearchAdapter:
    name = "9router"

    def __init__(self, client: NineRouterClient, provider: str):
        self.client = client
        self.provider = provider

    def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        if not self.provider:
            raise NotConfiguredError(
                self.name, "search", "SEARCH_PROVIDER kosong (riset memakai pengetahuan LLM + tandai belum terverifikasi)"
            )
        payload = self.client.search(self.provider, query, max_results=max_results)
        return self._parse(payload, max_results)

    @staticmethod
    def _parse(payload: dict[str, Any], max_results: int) -> list[SearchHit]:
        items: Optional[list[Any]] = None
        for key in ("results", "data", "items", "hits"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list):
                items = value
                break
        if items is None:
            raise ProviderError(
                "9router",
                f"bentuk respons search tidak dikenali: keys={sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}",
                kind="bad_response",
            )
        hits: list[SearchHit] = []
        for item in items[:max_results]:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or item.get("name") or ""),
                    url=str(item.get("url") or item.get("link") or ""),
                    snippet=str(item.get("snippet") or item.get("content") or item.get("description") or ""),
                    provider=str(item.get("provider") or "9router"),
                    published=item.get("published") or item.get("published_at"),
                )
            )
        return hits

    def health(self) -> ProviderHealth:
        if not self.provider:
            return ProviderHealth(
                provider=self.name,
                capability="search",
                status=HealthStatus.NOT_CONFIGURED,
                detail="SEARCH_PROVIDER kosong",
            )
        return ProviderHealth(
            provider=self.name,
            capability="search",
            status=HealthStatus.UNAVAILABLE,
            detail=f"provider '{self.provider}' dikonfigurasi tetapi belum terverifikasi berfungsi",
            meta={"provider": self.provider},
        )

    def probe(self) -> ProviderHealth:
        """Probe 1 query kecil untuk memverifikasi provider search."""
        base = self.health()
        if base.status == HealthStatus.NOT_CONFIGURED:
            return base
        try:
            hits = self.search("what is the capital of Indonesia", max_results=1)
            return ProviderHealth(
                provider=self.name,
                capability="search",
                status=HealthStatus.AVAILABLE,
                detail=f"probe ok: {len(hits)} hasil",
            )
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name,
                capability="search",
                status=HealthStatus.ERROR,
                detail=f"probe gagal: {exc}",
            )
