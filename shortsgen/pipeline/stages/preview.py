"""Stage PREVIEW: paket pratinjau untuk ditinjau manusia sebelum approval.

Menghasilkan thumbnail, ringkasan preview.json + preview.md, serta metadata
YouTube yang akan dipakai pada Fase F (judul, deskripsi, tags, disclosure).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...media.ffmpeg import FFmpeg
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ..orchestrator import StageContext


class PreviewStage:
    stage = "preview"

    def __init__(
        self,
        providers: ProviderBundle,
        config: Config,
        *,
        logger: Any = None,
        ffmpeg: Optional[FFmpeg] = None,
    ):
        self.providers = providers
        self.config = config
        self.logger = logger
        self._ffmpeg = ffmpeg

    @property
    def ffmpeg(self) -> FFmpeg:
        if self._ffmpeg is None:
            self._ffmpeg = FFmpeg(self.config.ffmpeg_path)
        return self._ffmpeg

    def run(self, ctx: StageContext) -> dict[str, Any]:
        render = _load_json(ctx, "render.json", ["render"])
        script = _load_json(ctx, "script.json", ["review", "script"])
        research = _load_json(ctx, "research.json", ["research"])
        video_path = Path(str(render.get("output") or ""))
        if not video_path.exists():
            raise RuntimeError(f"video tidak ditemukan: {video_path}")

        thumbnail = self.ffmpeg.extract_frame(
            video_path, ctx.artifacts_dir.parent / "thumbnail.jpg", at_s=max(0.5, float(render.get("duration_s", 2)) * 0.1)
        )

        facts = research.get("facts") or []
        metadata = {
            "title": (script.get("title") or ctx.job.topic)[:100],
            "description": script.get("description", ""),
            "tags": script.get("tags", []),
            "category_id": str(script.get("category_id") or "27"),  # 24=Entertainment, 27=Education
            "default_language": "id",
            "disclosure": script.get("disclosure", ""),
            "made_for_kids": self.config.youtube_made_for_kids,  # wajib sesuai audiens; disetel via env
            "visibility_default": self.config.upload_visibility,
        }

        preview = {
            "job_id": ctx.job.id,
            "topic": ctx.job.topic,
            "video": str(video_path),
            "thumbnail": str(thumbnail),
            "duration_s": render.get("duration_s"),
            "resolution": f"{render.get('width')}x{render.get('height')}",
            "size_mb": round(float(render.get("size_bytes") or 0) / 1024 / 1024, 2),
            "metadata": metadata,
            "facts_used": [
                {"id": f"F{index}", "claim": fact.get("claim"), "confidence": fact.get("confidence"),
                 "verification": fact.get("verification")}
                for index, fact in enumerate(facts, start=1)
                if f"F{index}" in (script.get("facts_used") or [])
            ],
            "source_notes": research.get("search_notes", []) + research.get("fetch_notes", []),
            "created_at": utcnow_iso(),
        }
        preview_path = ctx.artifact_path("preview.json")
        preview_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("preview.md")).write_text(_render_markdown(preview), encoding="utf-8")

        ctx.repo.add_event(
            ctx.job.id,
            "info",
            f"pratinjau siap: {video_path} — menunggu persetujuan pengguna sebelum upload",
        )

        return {
            "preview": str(preview_path),
            "video": str(video_path),
            "thumbnail": str(thumbnail),
            "title": metadata["title"],
        }


def _load_json(ctx: StageContext, filename: str, stages: list[str]) -> dict[str, Any]:
    for stage in stages:
        record = ctx.repo.get_stage(ctx.job.id, stage)
        if not record:
            continue
        for value in (record.artifacts or {}).values():
            path = Path(str(value))
            if path.name == filename and path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    fallback = ctx.artifacts_dir / filename
    if fallback.exists():
        return json.loads(fallback.read_text(encoding="utf-8"))
    raise RuntimeError(f"artefak '{filename}' tidak ditemukan")


def _render_markdown(preview: dict[str, Any]) -> str:
    metadata = preview.get("metadata") or {}
    lines = [
        f"# Pratinjau: {metadata.get('title')}",
        "",
        f"- Video: `{preview['video']}`",
        f"- Thumbnail: `{preview['thumbnail']}`",
        f"- Durasi: {preview.get('duration_s')} dtk | {preview.get('resolution')} | {preview.get('size_mb')} MB",
        f"- Visibilitas default saat upload: {metadata.get('visibility_default')}",
        "",
        "## Metadata YouTube (draf)",
        f"- Judul: {metadata.get('title')}",
        f"- Deskripsi: {metadata.get('description')}",
        f"- Tags: {', '.join(metadata.get('tags') or []) or '-'}",
        f"- Disclosure: {metadata.get('disclosure') or '-'}",
        "",
        "## Fakta yang dipakai",
    ]
    facts = preview.get("facts_used") or []
    if not facts:
        lines.append("- (tidak ada rujukan fakta)")
    for fact in facts:
        lines.append(
            f"- {fact['id']} (keyakinan {fact.get('confidence')}, verifikasi {fact.get('verification')}): {fact.get('claim')}"
        )
    if preview.get("source_notes"):
        lines += ["", "## Catatan sumber"] + [f"- {note}" for note in preview["source_notes"]]
    return "\n".join(lines) + "\n"
