"""Provider stok dari folder lokal milik pengguna.

Berguna bila pengguna sudah punya klip berlisensi sendiri (mis. rekaman pribadi
atau aset berlisensi CC). Lisensi dibaca dari `licenses.json` di folder yang
sama; klip TANPA entri lisensi dianggap **tidak boleh dipakai** (usage_allowed=False)
sehingga pipeline tidak akan memakainya tanpa konfirmasi.

Format licenses.json:
{
  "klip-aurora.mp4": {
    "license": "CC BY 4.0",
    "attribution": "Nama Pembuat (sumber URL)",
    "source": "https://contoh/sumber",
    "usage_allowed": true
  }
}
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..base import HealthStatus, ProviderHealth
from .base import StockClip

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")


class LocalFolderStockProvider:
    name = "local-stock"

    def __init__(self, folder: Path | str, *, licenses_file: Optional[Path | str] = None):
        self.folder = Path(folder).expanduser() if folder else None
        self.licenses_file = Path(licenses_file) if licenses_file else None

    # -- helpers -------------------------------------------------------

    def _licenses(self) -> dict[str, Any]:
        if self.licenses_file and self.licenses_file.exists():
            try:
                return json.loads(self.licenses_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        if self.folder:
            candidate = self.folder / "licenses.json"
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return {}
        return {}

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 2}

    def _iter_files(self) -> list[Path]:
        if not self.folder or not self.folder.exists():
            return []
        return sorted(
            path
            for path in self.folder.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        )

    # -- API -----------------------------------------------------------

    def search(self, query: str, *, max_results: int = 3) -> list[StockClip]:
        from ...errors import NotConfiguredError

        if not self.folder:
            raise NotConfiguredError(self.name, "stock", "STOCK_LOCAL_DIR belum diatur")
        query_tokens = self._tokens(query)
        licenses = self._licenses()
        scored: list[tuple[int, Path]] = []
        for path in self._iter_files():
            file_tokens = self._tokens(path.stem)
            score = len(query_tokens & file_tokens)
            if score > 0 or not query_tokens:
                scored.append((score, path))
        scored.sort(key=lambda item: (-item[0], str(item[1])))

        clips: list[StockClip] = []
        for _, path in scored[:max_results]:
            entry = licenses.get(path.name) or {}
            usage_allowed = bool(entry.get("usage_allowed", False))
            clips.append(
                StockClip(
                    provider=self.name,
                    clip_id=path.name,
                    download_url=str(path),          # sudah lokal
                    page_url=str(entry.get("source") or ""),
                    duration_s=float(entry.get("duration_s") or 0.0),
                    width=int(entry.get("width") or 0),
                    height=int(entry.get("height") or 0),
                    license=str(entry.get("license") or "unknown (belum ada entri di licenses.json)"),
                    attribution=str(entry.get("attribution") or ""),
                    usage_allowed=usage_allowed,
                    query=query,
                    meta={"local_path": str(path)},
                )
            )
        return clips

    def download(self, clip: StockClip, out_path: Path, *, max_bytes: int) -> Path:
        source = Path(clip.download_url)
        if not source.exists():
            from ...errors import ProviderError

            raise ProviderError(self.name, f"file lokal tidak ditemukan: {source}", kind="bad_input")
        if source.stat().st_size > max_bytes:
            from ...errors import ProviderError

            raise ProviderError(self.name, "klip melebihi batas ukuran", kind="asset_too_large")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(source.read_bytes())
        return out_path

    def health(self) -> ProviderHealth:
        if not self.folder:
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.NOT_CONFIGURED,
                detail="STOCK_LOCAL_DIR belum diatur",
            )
        if not self.folder.exists():
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.ERROR,
                detail=f"folder tidak ditemukan: {self.folder}",
            )
        total = len(self._iter_files())
        licensed = sum(
            1
            for path in self._iter_files()
            if (self._licenses().get(path.name) or {}).get("usage_allowed")
        )
        if total == 0:
            return ProviderHealth(
                provider=self.name,
                capability="stock",
                status=HealthStatus.UNAVAILABLE,
                detail=f"folder kosong (tidak ada file video): {self.folder}",
            )
        return ProviderHealth(
            provider=self.name,
            capability="stock",
            status=HealthStatus.AVAILABLE if licensed else HealthStatus.UNAVAILABLE,
            detail=f"{licensed}/{total} klip punya entri lisensi yang mengizinkan pemakaian",
            meta={"folder": str(self.folder)},
        )
