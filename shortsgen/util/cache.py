"""Cache respons provider berbasis berkas (kontrol biaya & waktu).

Aman secara default:
- Hanya menyimpan hasil sukses; error tidak pernah di-cache.
- Key = hash dari namespace + payload lengkap (model, prompt, parameter).
- TTL opsional; entri kedaluwarsa diabaikan dan ditimpa saat ada hasil baru.
- Kegagalan cache (disk penuh/izin) tidak boleh menggagalkan pipeline.

Cache ini tidak menyimpan rahasia: payload adalah prompt/parameter, bukan API key.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional


class FileCache:
    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        ttl_hours: float = 168.0,
        namespace: str = "default",
    ):
        self.directory = Path(directory)
        self.enabled = bool(enabled)
        self.ttl_s = max(0.0, float(ttl_hours) * 3600.0)
        self.namespace = namespace
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # -- API ----------------------------------------------------------

    def get_bytes(self, key_data: Any) -> Optional[bytes]:
        path = self._path(key_data, ".bin")
        if path is None:
            return None
        try:
            if not path.exists() or self._expired(path):
                self.misses += 1
                return None
            self.hits += 1
            return path.read_bytes()
        except OSError:
            self.misses += 1
            return None

    def set_bytes(self, key_data: Any, data: bytes) -> None:
        path = self._path(key_data, ".bin")
        if path is None or not data:
            return
        self._write_atomic(path, data)

    def get_json(self, key_data: Any) -> Optional[dict[str, Any]]:
        path = self._path(key_data, ".json")
        if path is None:
            return None
        try:
            if not path.exists() or self._expired(path):
                self.misses += 1
                return None
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None

    def set_json(self, key_data: Any, payload: dict[str, Any]) -> None:
        path = self._path(key_data, ".json")
        if path is None:
            return
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError):
            return
        self._write_atomic(path, encoded)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "namespace": self.namespace,
            "ttl_hours": round(self.ttl_s / 3600.0, 2),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
        }

    # -- internal -----------------------------------------------------

    def _path(self, key_data: Any, suffix: str) -> Optional[Path]:
        if not self.enabled:
            return None
        try:
            payload = json.dumps(key_data, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        digest = hashlib.sha256(f"{self.namespace}:{payload}".encode("utf-8")).hexdigest()
        return self.directory / self.namespace / f"{digest[:2]}/{digest}{suffix}"

    def _expired(self, path: Path) -> bool:
        if self.ttl_s <= 0:
            return False
        try:
            return (time.time() - path.stat().st_mtime) > self.ttl_s
        except OSError:
            return True

    def _write_atomic(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + f".tmp{os.getpid()}")
            temp.write_bytes(data)
            shutil.move(str(temp), str(path))
            self.writes += 1
        except OSError:
            return


def build_cache(
    directory: Path,
    *,
    enabled: bool,
    ttl_hours: float,
    namespace: str,
) -> FileCache:
    return FileCache(directory, enabled=enabled, ttl_hours=ttl_hours, namespace=namespace)
