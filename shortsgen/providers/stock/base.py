"""Kontrak provider stok video.

Aturan lisensi yang ditegakkan di sini:
- Setiap klip membawa metadata sumber, lisensi, dan atribusi.
- `usage_allowed=False` berarti klip TIDAK boleh dipakai di video final.
- Tidak ada pengambilan dari YouTube atau sumber tanpa izin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ..base import ProviderHealth


@dataclass
class StockClip:
    provider: str
    clip_id: str
    download_url: str
    page_url: str = ""
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    license: str = "unknown"
    attribution: str = ""
    usage_allowed: bool = False
    query: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "clip_id": self.clip_id,
            "download_url": self.download_url,
            "page_url": self.page_url,
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "license": self.license,
            "attribution": self.attribution,
            "usage_allowed": self.usage_allowed,
            "query": self.query,
            "meta": self.meta,
        }


@runtime_checkable
class StockProvider(Protocol):
    name: str

    def search(self, query: str, *, max_results: int = 3) -> list[StockClip]: ...

    def download(self, clip: StockClip, out_path: Path, *, max_bytes: int) -> Path: ...

    def health(self) -> ProviderHealth: ...
