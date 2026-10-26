"""Kontrak provider (Protocol) + tipe hasil + status kesehatan.

Adapter pattern: pipeline hanya bergantung pada Protocol di modul ini,
sehingga provider nyata (9Router) dan mock bisa dipertukarkan tanpa mengubah
kode pipeline (dependency injection lewat ProviderBundle).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from ..models import SearchHit


class Capability:
    LLM = "llm"
    TTS = "tts"
    STT = "stt"
    IMAGE = "image"
    VIDEO = "video"
    SEARCH = "search"
    FETCH = "fetch"
    STOCK = "stock"


class HealthStatus:
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"
    ERROR = "error"


@dataclass
class ProviderHealth:
    provider: str
    capability: str
    status: str
    detail: str = ""
    latency_ms: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "meta": self.meta,
        }


# -- hasil operasi ----------------------------------------------------

@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)
    transport: str = "json"     # json | sse
    latency_ms: Optional[int] = None
    raw: Optional[dict[str, Any]] = None


@dataclass
class TTSResult:
    path: Path
    provider: str
    model: str
    bytes_written: int
    latency_ms: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageResult:
    path: Path
    provider: str
    model: str
    mime: str
    bytes_written: int
    latency_ms: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoResult:
    path: Path
    provider: str
    model: str
    bytes_written: int
    latency_ms: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    url: str
    text: str
    provider: str
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TranscriptionResult:
    text: str
    provider: str
    model: str
    latency_ms: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)


ChatMessage = dict[str, str]


# -- Protocol provider ------------------------------------------------

@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path, *, voice: Optional[str] = None) -> TTSResult: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class STTProvider(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class ImageProvider(Protocol):
    name: str

    def generate(
        self,
        prompt: str,
        out_path: Path,
        *,
        size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        reference_image: Optional[Path] = None,
    ) -> ImageResult: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class VideoProvider(Protocol):
    name: str

    def generate(
        self, prompt: str, out_path: Path, *, seconds: int = 5
    ) -> VideoResult: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class FetchProvider(Protocol):
    name: str

    def fetch(self, url: str, *, max_chars: Optional[int] = None) -> FetchResult: ...

    def health(self) -> ProviderHealth: ...


@dataclass
class ProviderBundle:
    """Kumpulan provider aktif untuk satu konfigurasi.

    Kapabilitas yang tidak dikonfigurasi bernilai None; pemanggil wajib
    memperlakukan None sebagai `not_configured` (bukan fallback diam-diam).
    """

    llm: Optional[LLMProvider] = None
    tts: Optional[TTSProvider] = None
    stt: Optional[STTProvider] = None
    image: Optional[ImageProvider] = None
    video: Optional[VideoProvider] = None
    search: Optional[SearchProvider] = None
    fetch: Optional[FetchProvider] = None
    stock: Optional[Any] = None      # adapter stok video (Fase D); Any agar tetap ringan
    mode: str = "mock"

    def cache_stats(self) -> dict[str, dict[str, Any]]:
        """Statistik cache per kapabilitas (untuk artefak job; tanpa rahasia)."""
        stats: dict[str, dict[str, Any]] = {}
        seen: set[int] = set()
        for capability in (
            Capability.LLM,
            Capability.TTS,
            Capability.STT,
            Capability.IMAGE,
            Capability.VIDEO,
            Capability.SEARCH,
            Capability.FETCH,
        ):
            provider = getattr(self, capability)
            cache = getattr(provider, "cache", None) if provider is not None else None
            if cache is None or id(cache) in seen or not hasattr(cache, "stats"):
                continue
            seen.add(id(cache))
            stats[capability] = cache.stats()
        return stats

    def health(self) -> list[ProviderHealth]:
        results: list[ProviderHealth] = []
        for capability in (
            Capability.LLM,
            Capability.TTS,
            Capability.STT,
            Capability.IMAGE,
            Capability.VIDEO,
            Capability.SEARCH,
            Capability.FETCH,
            Capability.STOCK,
        ):
            provider = getattr(self, capability)
            if provider is None:
                results.append(
                    ProviderHealth(
                        provider="-",
                        capability=capability,
                        status=HealthStatus.NOT_CONFIGURED,
                        detail="Tidak dikonfigurasi (lihat .env)",
                    )
                )
            else:
                try:
                    results.append(provider.health())
                except Exception as exc:  # health check tidak boleh meledak
                    results.append(
                        ProviderHealth(
                            provider=getattr(provider, "name", "?"),
                            capability=capability,
                            status=HealthStatus.ERROR,
                            detail=f"health check error: {exc}",
                        )
                    )
        return results
