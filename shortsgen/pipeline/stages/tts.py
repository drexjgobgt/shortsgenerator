"""Stage TTS: voice acting per klausa + pengukuran durasi nyata + retiming shot.

Alur:
1. Pilih profil suara dari arketipe topik (atau TTS_VOICE_PROFILE eksplisit).
2. Pecah narasi tiap scene menjadi klausa; tiap klausa disintesis dengan
   prosody sesuai emosi/intensitas (hanya parameter yang didukung provider).
3. Gabung klausa dengan jeda nyata, ukur durasi dengan ffprobe.
4. Padding tiap scene ke durasi timeline; gabung + post-process + loudnorm.
5. Perbarui storyboard (durasi scene + shot plan) dari hasil pengukuran.

Catatan: STT tidak dipakai; timing berasal dari durasi audio nyata.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...errors import ProviderError, ShortsGenError
from ...media.ffmpeg import FFmpeg
from ...media.voice import (
    choose_voice_profile,
    plan_scene_segments,
    postprocess_voice,
    synthesize_scene,
)
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ...story.narrative import classify_topic
from ...story.storyboard import retime_storyboard
from ..orchestrator import StageContext

SCENE_GAP_S = 0.25
MIN_SCENE_S = 1.0
SAMPLE_RATE = 48000

MALE_VOICE = "id-ID-ArdiNeural"
FEMALE_VOICE = "id-ID-GadisNeural"

# Heuristik sederhana untuk mode "auto" (dapat ditimpa lewat TTS_VOICE_MODE).
FEMININE_HINTS = (
    "kuliner", "masak", "resep", "fashion", "gaya", "kecantikan", "skincare",
    "psikologi", "perasaan", "kisah", "cerita", "kehidupan", "parenting", "anak",
    "kesehatan", "diet", "yoga", "dekor", "tanaman", "seni", "musik",
)
MASCULINE_HINTS = (
    "mars", "planet", "antariksa", "angkasa", "fisika", "kimia", "matematika",
    "teknologi", "komputer", "mesin", "kode", "sejarah", "perang", "otomotif",
    "listrik", "energi", "geologi", "gunung", "cuaca", "astronomi", "sains",
)


def choose_voice(config: Config, text: str) -> str:
    """Tentukan id suara berdasarkan TTS_VOICE_MODE (auto/male/female/id eksplisit).

    Dipertahankan untuk kompatibilitas; stage kini memakai profil suara.
    """
    raw = (config.tts_voice_mode or "auto").strip()
    mode = raw.lower()
    if mode == "male":
        return MALE_VOICE
    if mode == "female":
        return FEMALE_VOICE
    if mode != "auto":  # id suara eksplisit (dengan atau tanpa prefix provider)
        return raw.split("/", 1)[-1] if "/" in raw else raw
    lowered = text.lower()
    feminine = sum(1 for hint in FEMININE_HINTS if hint in lowered)
    masculine = sum(1 for hint in MASCULINE_HINTS if hint in lowered)
    return FEMALE_VOICE if feminine > masculine else MALE_VOICE


class TTSStage:
    stage = "tts"

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
        storyboard = _load_storyboard(ctx)
        scenes = storyboard.get("scenes") or []
        if not scenes:
            raise RuntimeError("storyboard.json tidak memuat scene")

        overrides = ctx.artifacts_dir.parent / "voice" / "overrides" / "manifest.json"
        if overrides.exists():
            from ...media.imported_voice import import_voiceover

            return import_voiceover(ctx, self.config, self.ffmpeg, storyboard)
        if ctx.providers.tts is None:
            raise RuntimeError("provider TTS tidak dikonfigurasi")

        script = _load_script(ctx)
        archetype = (script.get("narrative") or {}).get("archetype") or classify_topic(ctx.job.topic)
        profile = choose_voice_profile(
            ctx.job.topic,
            archetype,
            requested=self.config.tts_voice_profile,
            fallback_voice_mode=self.config.tts_voice_mode,
        )
        voice = profile.voice
        voice_dir = ctx.artifacts_dir.parent / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".wav" if ctx.config.is_mock else ".mp3"

        segments: list[dict[str, Any]] = []
        voice_segments: list[dict[str, Any]] = []
        prosody_supported: Optional[bool] = None
        for scene in scenes:
            index = int(scene.get("index", 0))
            text = str(scene.get("narration") or "").strip()
            if not text:
                continue
            planned = plan_scene_segments(
                scene, profile, emotion_enabled=self.config.tts_emotion
            )
            raw_path = voice_dir / f"scene_{index:02d}{suffix}"
            try:
                raw_path, measured_clauses, results = synthesize_scene(
                    ctx.providers.tts,
                    planned,
                    raw_path,
                    voice=voice,
                    ffmpeg=self.ffmpeg,
                    sample_rate=SAMPLE_RATE,
                )
            except (ProviderError, ValueError) as exc:
                # fallback terakhir: satu panggilan untuk seluruh scene (tanpa prosody)
                if self.logger is not None:
                    self.logger.warning(f"voice per klausa gagal untuk scene {index}: {exc}; fallback satu panggilan")
                try:
                    result = ctx.providers.tts.synthesize(text, raw_path, voice=voice)
                except ProviderError:
                    raise
                measured_clauses = [
                    {
                        "index": 0,
                        "text": text,
                        "duration_s": round(self.ffmpeg.duration(result.path), 3),
                        "rate": "+0%",
                        "pitch": "+0Hz",
                        "volume": "+0%",
                        "emotion": scene.get("emotion"),
                        "emphasis_hit": False,
                        "raw_path": str(result.path),
                        "fallback": True,
                    }
                ]
                results = [result]
                raw_path = Path(result.path)
            for clause, result in zip(measured_clauses, results):
                meta = result.meta or {}
                if "prosody" in meta:
                    prosody_supported = bool(meta["prosody"]) if prosody_supported is None else (
                        prosody_supported and bool(meta["prosody"])
                    )
                ctx.repo.record_usage(
                    ctx.job.id,
                    provider=result.provider,
                    model=result.model,
                    kind="tts",
                    units={
                        "segments": 1,
                        "characters": float(len(str(clause.get("text") or ""))),
                        "bytes": float(result.bytes_written),
                    },
                    meta={
                        "scene": index,
                        "voice": voice,
                        "latency_ms": result.latency_ms,
                        "prosody": meta.get("prosody"),
                    },
                )
            segments.append(
                {
                    "scene_index": index,
                    "raw_path": raw_path,
                    "text": text,
                    "clauses": measured_clauses,
                }
            )
            voice_segments.append(
                {
                    "scene_index": index,
                    "scene_id": scene.get("id"),
                    "emotion": scene.get("emotion"),
                    "intensity": scene.get("intensity"),
                    "voice_direction": scene.get("voice_direction"),
                    "clauses": measured_clauses,
                }
            )

        if not segments:
            raise RuntimeError("tidak ada narasi yang bisa disintesis")

        # 3-4: ukur durasi nyata + padding ke timeline
        padded_paths: list[Path] = []
        measured: list[dict[str, Any]] = []
        cursor = 0.0
        total = 0.0
        for scene, segment in zip([s for s in scenes if str(s.get("narration") or "").strip()], segments):
            seg_duration = self.ffmpeg.duration(segment["raw_path"])
            if seg_duration <= 0:
                raise ShortsGenError(f"durasi audio segmen tidak terukur: {segment['raw_path']}")
            scene_duration = max(MIN_SCENE_S, round(seg_duration + SCENE_GAP_S, 3))
            padded = voice_dir / f"scene_{segment['scene_index']:02d}_pad.wav"
            self.ffmpeg.pad_audio_to(segment["raw_path"], padded, scene_duration, sample_rate=SAMPLE_RATE)
            padded_paths.append(padded)

            scene["start_s"] = round(cursor, 3)
            scene["duration_s"] = scene_duration
            scene["end_s"] = round(cursor + scene_duration, 3)
            measured.append(
                {
                    "scene_index": segment["scene_index"],
                    "start_s": scene["start_s"],
                    "end_s": scene["end_s"],
                    "duration_s": scene_duration,
                    "segment_duration_s": round(seg_duration, 3),
                    "segment_path": str(segment["raw_path"]),
                    "clauses": segment["clauses"],
                }
            )
            cursor = round(cursor + scene_duration, 3)
            total = cursor

        raw_voiceover = self.ffmpeg.concat_audio(padded_paths, voice_dir / "voiceover_raw.wav", sample_rate=SAMPLE_RATE)
        if self.config.tts_postprocess:
            voiceover = postprocess_voice(
                self.ffmpeg,
                raw_voiceover,
                voice_dir / "voiceover.wav",
                target_lufs=-16.0,
            )
        else:
            voiceover = self.ffmpeg.normalize_loudness(
                raw_voiceover, voice_dir / "voiceover.wav", target_lufs=-16.0
            )
        final_duration = self.ffmpeg.duration(voiceover)
        loudness = self.ffmpeg.measure_loudness(voiceover)
        peak = self.ffmpeg.measure_peak(voiceover)

        # 5: perbarui storyboard dengan timing hasil pengukuran + shot plan
        planned_backup = ctx.artifact_path("storyboard_planned.json")
        if not planned_backup.exists():
            planned_backup.write_text(
                json.dumps(_load_storyboard(ctx), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        retime_storyboard(
            storyboard,
            measured,
            max_shot_s=float(self.config.max_scene_seconds),
        )
        storyboard["timeline"]["voice_profile"] = profile.id
        storyboard["timeline"]["prosody"] = prosody_supported
        ctx.artifact_path("storyboard.json").write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        segments_path = ctx.artifact_path("voice_segments.json")
        segments_path.write_text(
            json.dumps(
                {
                    "voice_profile": profile.id,
                    "voice_profile_name": profile.name,
                    "voice": voice,
                    "prosody_supported": prosody_supported,
                    "emotion_enabled": self.config.tts_emotion,
                    "scenes": voice_segments,
                    "created_at": utcnow_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        timings = {
            "voice": voice,
            "voice_profile": profile.id,
            "tts_model": ctx.config.tts_model,
            "tts_engine": getattr(ctx.providers.tts, "name", "unknown"),
            "prosody_supported": prosody_supported,
            "voiceover_path": str(voiceover),
            "total_seconds": total,
            "audio_duration_s": round(final_duration, 3),
            "loudness": loudness,
            "peak_dbfs": peak,
            "scenes": measured,
            "voice_segments": str(segments_path),
            "created_at": utcnow_iso(),
        }
        timings_path = ctx.artifact_path("timings.json")
        timings_path.write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "timings": str(timings_path),
            "voice_segments": str(segments_path),
            "voiceover": str(voiceover),
            "voice": voice,
            "voice_profile": profile.id,
            "prosody_supported": prosody_supported,
            "total_seconds": total,
            "audio_duration_s": round(final_duration, 3),
            "peak_dbfs": peak,
        }


def _load_storyboard(ctx: StageContext) -> dict[str, Any]:
    record = ctx.repo.get_stage(ctx.job.id, "assets")
    candidate: Optional[Path] = None
    if record:
        for value in (record.artifacts or {}).values():
            path = Path(str(value))
            if path.name == "storyboard.json" and path.exists():
                candidate = path
                break
    if candidate is None:
        fallback = ctx.artifacts_dir / "storyboard.json"
        candidate = fallback if fallback.exists() else None
    if candidate is None:
        raise RuntimeError("storyboard.json tidak ditemukan; jalankan stage assets lebih dulu")
    return json.loads(candidate.read_text(encoding="utf-8"))


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
        return {}
    return json.loads(candidate.read_text(encoding="utf-8"))
