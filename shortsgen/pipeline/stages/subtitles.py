"""Stage SUBTITLES: subtitle tersinkron dari timing audio nyata + validasi."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...media.subtitles import (
    build_cues_from_scenes,
    build_karaoke_cues,
    build_karaoke_cues_from_measured,
    build_overlay_cues,
    to_ass,
    to_ass_dynamic,
    validate_cues,
    write_subtitles,
)
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ..orchestrator import StageContext

HOOK_OVERLAY_MAX_WORDS = 12


def _short_hook(hook: str) -> str:
    """Versi pendek hook untuk overlay besar (maks 9 kata agar tetap terbaca)."""
    words = re.sub(r"\s+", " ", hook or "").strip().split()
    return " ".join(words[:HOOK_OVERLAY_MAX_WORDS])


class SubtitleStage:
    stage = "subtitles"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        storyboard = _load_json(ctx, "storyboard.json", ["tts", "assets", "scenes"])
        scenes = storyboard.get("scenes") or []
        if not scenes:
            raise RuntimeError("storyboard.json tidak memuat scene")

        timings = _load_json(ctx, "timings.json", ["tts"])
        audio_duration = float(timings.get("audio_duration_s") or storyboard.get("timeline", {}).get("total_seconds") or 0)

        # SRT/VTT memakai frasa (enak dibaca di YouTube); ASS burn-in memakai gaya karaoke.
        phrase_cues = build_cues_from_scenes(scenes)
        phrase_issues = validate_cues(phrase_cues, media_duration_s=audio_duration)
        if phrase_issues:
            raise RuntimeError("subtitle frasa tidak valid: " + "; ".join(phrase_issues[:4]))

        style = getattr(self.config, "captions_style", "static")
        if style == "dynamic":
            measured_scenes = timings.get("scenes") or []
            karaoke_cues = build_karaoke_cues_from_measured(scenes, measured_scenes)
            if not karaoke_cues:
                karaoke_cues = build_karaoke_cues(scenes)
        else:
            karaoke_cues = phrase_cues
        karaoke_issues = validate_cues(karaoke_cues, media_duration_s=audio_duration)
        if karaoke_issues:
            raise RuntimeError("caption karaoke tidak valid: " + "; ".join(karaoke_issues[:4]))

        overlays = build_overlay_cues(scenes) if style == "dynamic" else []

        hook_text = ""
        if getattr(self.config, "hook_overlay", True):
            script = _load_json(ctx, "script.json", ["review", "script"])
            hook_text = _short_hook(str(script.get("hook") or ""))

        paths = write_subtitles(
            phrase_cues,
            ctx.artifacts_dir,
            width=self.config.render_width,
            height=self.config.render_height,
            style="static",  # SRT/VTT/ASS dasar dari frasa
        )
        if style == "dynamic":
            # burn-in memakai potongan karaoke + overlay hook; simpan juga versi statis
            (ctx.artifacts_dir / "subtitle_static.ass").write_text(
                to_ass(phrase_cues, width=self.config.render_width, height=self.config.render_height),
                encoding="utf-8",
            )
            paths["ass"].write_text(
                to_ass_dynamic(
                    karaoke_cues,
                    width=self.config.render_width,
                    height=self.config.render_height,
                    hook_text=hook_text,
                    overlays=overlays,
                ),
                encoding="utf-8",
            )

        report = {
            "cue_count": len(phrase_cues),
            "karaoke_chunks": len(karaoke_cues),
            "overlay_count": len(overlays),
            "overlays": overlays,
            "karaoke_source": (
                timings.get("caption_alignment_source")
                or ("measured_clauses" if (timings.get("scenes") and style == "dynamic") else "proportional")
            ),
            "style": style,
            "hook_overlay": hook_text or None,
            "first_start_s": phrase_cues[0].start_s if phrase_cues else None,
            "last_end_s": phrase_cues[-1].end_s if phrase_cues else None,
            "audio_duration_s": audio_duration,
            "files": {key: str(value) for key, value in paths.items()},
            "created_at": utcnow_iso(),
        }
        report_path = ctx.artifact_path("subtitles.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "subtitles": str(report_path),
            "srt": str(paths["srt"]),
            "vtt": str(paths["vtt"]),
            "ass": str(paths["ass"]),
            "cue_count": len(phrase_cues),
            "karaoke_chunks": len(karaoke_cues),
            "style": style,
        }


def _load_json(ctx: StageContext, filename: str, stages: list[str]) -> dict[str, Any]:
    """Cari artefak JSON dari artifact stage (lebih dulu) lalu folder artifacts."""
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
