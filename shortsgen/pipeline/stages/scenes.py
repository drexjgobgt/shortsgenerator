"""Stage SCENES: storyboard terstruktur + style bible + shot plan.

Perubahan refactor:
- Tidak lagi memilih stok untuk semua blok body. AI image menjadi utama untuk
  konsistensi sinematik; stok hanya bila kueri benar-benar spesifik.
- Setiap scene membawa subjek/aksi/lokasi/komposisi/kamera/lighting/palet/
  transisi/voice direction/emphasis/SFX/kontinuitas.
- Shot plan mengikuti durasi narasi (bukan interval tetap) dan punya alasan kamera.
- Menghasilkan contact sheet `storyboard_preview.png` untuk pemeriksaan manusia.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...models import utcnow_iso
from ...providers.base import HealthStatus, ProviderBundle
from ...story.narrative import classify_topic
from ...story.storyboard import (
    build_storyboard,
    render_storyboard_markdown,
    render_storyboard_preview,
)
from ...story.style_bible import build_style_bible
from ..orchestrator import StageContext


class ScenePlannerStage:
    stage = "scenes"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        script = _load_script(ctx)
        if not script.get("beats"):
            raise RuntimeError("script.json tidak memuat beats; jalankan stage naskah lebih dulu")

        stock_ok = _can_use_stock(ctx.providers)
        image_status = (
            ctx.providers.image.health().status if ctx.providers.image is not None else HealthStatus.NOT_CONFIGURED
        )
        archetype = (script.get("narrative") or {}).get("archetype") or classify_topic(ctx.job.topic)
        style_bible = build_style_bible(
            ctx.job.topic,
            archetype,
            series_character=str(getattr(self.config, "series_character", "") or ""),
            reference_chain=bool(getattr(self.config, "reference_chain", True)),
            visual_style_override=str(getattr(self.config, "visual_style", "") or ""),
        )
        storyboard = build_storyboard(
            script | {"topic": ctx.job.topic},
            style_bible,
            self.config,
            stock_available=stock_ok,
            image_status=image_status,
            max_shot_s=float(self.config.max_scene_seconds),
        )

        storyboard_path = ctx.artifact_path("storyboard.json")
        storyboard_path.write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("storyboard.md")).write_text(
            render_storyboard_markdown(storyboard), encoding="utf-8"
        )
        (ctx.artifact_path("style_bible.json")).write_text(
            json.dumps(style_bible, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        preview = render_storyboard_preview(
            storyboard,
            ctx.artifact_path("storyboard_preview.png"),
            title=storyboard.get("title") or ctx.job.topic,
        )

        ctx.repo.add_event(
            ctx.job.id,
            "info",
            f"storyboard siap: {len(storyboard['scenes'])} scene, "
            f"{len(storyboard['scenes'][0]['shot_plan']) if storyboard['scenes'] else 0}+ shot/scene, "
            f"style {style_bible['id']}",
        )

        return {
            "storyboard": str(storyboard_path),
            "storyboard_md": str(ctx.artifact_path("storyboard.md")),
            "style_bible": str(ctx.artifact_path("style_bible.json")),
            "storyboard_preview": str(preview),
            "scenes": len(storyboard["scenes"]),
            "total_seconds": storyboard["timeline"]["total_seconds"],
            "ai_images_planned": storyboard["visual_plan"]["ai_images_planned"],
            "stock_clips_planned": storyboard["visual_plan"]["stock_clips_planned"],
            "strategies": storyboard["visual_plan"]["strategies"],
            "style_id": style_bible["id"],
        }


def _can_use_stock(providers: ProviderBundle) -> bool:
    stock = getattr(providers, "stock", None)
    if stock is None:
        return False
    try:
        return stock.health().status == HealthStatus.AVAILABLE
    except Exception:
        return False


def _load_script(ctx: StageContext) -> dict[str, Any]:
    record = ctx.repo.get_stage(ctx.job.id, "script")
    candidate: Optional[Path] = None
    if record and record.artifacts.get("script"):
        path = Path(record.artifacts["script"])
        if path.exists():
            candidate = path
    if candidate is None:
        fallback = ctx.artifacts_dir / "script.json"
        candidate = fallback if fallback.exists() else None
    if candidate is None:
        raise RuntimeError("script.json tidak ditemukan; jalankan stage naskah lebih dulu")
    return json.loads(candidate.read_text(encoding="utf-8"))


def _utcnow() -> str:
    return utcnow_iso()
