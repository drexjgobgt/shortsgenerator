"""Adapter image generation -> 9Router /v1/images/generations.

Terverifikasi audit: `ag/gemini-3.1-flash-image` bekerja (JPEG base64,
kuota subscription Antigravity). `gemini/*-image*` kena kuota free-tier = 0.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ...errors import NotConfiguredError, ProviderError
from ..base import HealthStatus, ImageProvider, ImageResult, ProviderHealth
from .client import NineRouterClient


class ImageAdapter:
    name = "9router"

    def __init__(
        self,
        client: NineRouterClient,
        model: str,
        *,
        max_bytes: int = 20 * 1024 * 1024,
        cache: Optional[object] = None,
    ):
        self.client = client
        self.model = model
        self.max_bytes = max_bytes
        self.cache = cache

    def generate(
        self,
        prompt: str,
        out_path: Path,
        *,
        size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        reference_image: Optional[Path] = None,
    ) -> ImageResult:
        if not self.model:
            raise NotConfiguredError(
                self.name, "image", "IMAGE_MODEL kosong (fallback: grafis FFmpeg)"
            )
        if not prompt.strip():
            raise ProviderError(self.name, "prompt kosong", kind="bad_input")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        references: Optional[list[str]] = None
        reference_digest = ""
        if reference_image is not None:
            ref_path = Path(reference_image)
            if ref_path.exists():
                import base64
                import hashlib

                ref_bytes = ref_path.read_bytes()
                reference_digest = hashlib.sha256(ref_bytes).hexdigest()[:16]
                mime = "image/png" if ref_path.suffix.lower() == ".png" else "image/jpeg"
                encoded = base64.b64encode(ref_bytes).decode("ascii")
                references = [f"data:{mime};base64,{encoded}"]
        cache_key = {
            "prompt": prompt,
            "model": self.model,
            "size": size,
            "aspect_ratio": aspect_ratio,
            "reference": reference_digest,
        }
        if self.cache is not None:
            cached = self.cache.get_bytes(cache_key)
            if cached:
                out_path.write_bytes(cached)
                return ImageResult(
                    path=out_path,
                    provider=self.name,
                    model=self.model,
                    mime=_guess_mime(cached),
                    bytes_written=out_path.stat().st_size,
                    latency_ms=0,
                    meta={"cached": True, "prompt": prompt[:300]},
                )
        payload = self.client.images(
            prompt,
            self.model,
            size=size,
            aspect_ratio=aspect_ratio,
            reference_images=references,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if len(payload.data) > self.max_bytes:
            raise ProviderError(
                self.name,
                f"gambar melebihi batas {self.max_bytes} byte",
                kind="asset_too_large",
                model=self.model,
            )
        out_path.write_bytes(payload.data)
        if self.cache is not None:
            self.cache.set_bytes(cache_key, payload.data)
        return ImageResult(
            path=out_path,
            provider=self.name,
            model=payload.model,
            mime=payload.mime,
            bytes_written=out_path.stat().st_size,
            latency_ms=latency_ms,
            meta={"source": payload.source, "prompt": prompt[:300]},
        )

    def health(self) -> ProviderHealth:
        if not self.model:
            return ProviderHealth(
                provider=self.name,
                capability="image",
                status=HealthStatus.NOT_CONFIGURED,
                detail="IMAGE_MODEL kosong -> fallback grafis FFmpeg",
            )
        return ProviderHealth(
            provider=self.name,
            capability="image",
            status=HealthStatus.AVAILABLE,
            detail=f"model: {self.model} (kuota provider berlaku)",
        )


def _guess_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return "application/octet-stream"
