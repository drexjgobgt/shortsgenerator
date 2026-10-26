"""Import completed TTS audio without issuing a provider request.

Each scene is bound to its narration and SHA-256 in voice/overrides/manifest.json.
Missing, stale or corrupt overrides fail closed instead of silently using another
voice. Imported timing labels distinguish ASR alignment from proportional timing.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from ..models import utcnow_iso
from ..story.storyboard import retime_storyboard
from .voice import postprocess_voice


def validate_voice_manifest(directory: Path, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for key in ("provider", "model", "voice", "profile", "delivery_control"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"voice override: metadata {key} wajib diisi")
    entries: dict[int, dict[str, Any]] = {}
    for entry in manifest.get("scenes", []):
        index = int(entry["scene_index"])
        if index in entries:
            raise ValueError("voice override: scene duplikat")
        entries[index] = entry
    expected = {int(scene["index"]) for scene in scenes if scene.get("narration")}
    if set(entries) != expected:
        raise ValueError("voice override: semua scene harus tersedia tepat sekali")
    for scene in scenes:
        if not scene.get("narration"):
            continue
        entry = entries[int(scene["index"])]
        if entry.get("text") != scene["narration"]:
            raise ValueError("voice override: narasi berubah; sintesis/alignment harus diperbarui")
        path = (directory / entry["file"]).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError("voice override: file wajib berada di folder overrides")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            raise ValueError("voice override: SHA-256 tidak cocok")
        duration = float(entry.get("duration_s", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("voice override: durasi tidak valid")
        previous_end = 0.0
        for cue in entry.get("clauses", []):
            start = float(cue["start_offset_s"])
            end = start + float(cue["duration_s"])
            if not all(math.isfinite(t) for t in (start, end)) or not (
                previous_end <= start < end <= duration + 0.02
            ):
                raise ValueError("voice override: alignment tidak monoton/di luar audio")
            if not str(cue.get("text") or "").strip():
                raise ValueError("voice override: teks alignment kosong")
            previous_end = end
        if entry.get("clauses"):
            joined = " ".join(cue["text"] for cue in entry["clauses"])
            if joined.split() != scene["narration"].split():
                raise ValueError("voice override: alignment tidak mencakup seluruh narasi")
        entry["path"] = str(path)
    manifest["entries"] = entries
    return manifest


def import_voiceover(ctx: Any, config: Any, ffmpeg: Any, storyboard: dict[str, Any]) -> dict[str, Any]:
    scenes = storyboard.get("scenes") or []
    voice_dir = ctx.artifacts_dir.parent / "voice"
    manifest = validate_voice_manifest(voice_dir / "overrides", scenes)
    measured: list[dict[str, Any]] = []
    tracks: list[Path] = []
    cursor = 0.0
    for scene in scenes:
        if not scene.get("narration"):
            continue
        index = int(scene["index"])
        entry = manifest["entries"][index]
        source = Path(entry["path"])
        seconds = ffmpeg.duration(source)
        if abs(seconds - float(entry["duration_s"])) > 0.05:
            raise ValueError("voice override: durasi terukur tidak cocok dengan manifest")
        duration = round(max(1.0, seconds + 0.15), 3)
        padded = voice_dir / f"imported_{index:02d}_pad.wav"
        ffmpeg.pad_audio_to(source, padded, duration, sample_rate=48000)
        tracks.append(padded)
        measured.append({
            "scene_index": index,
            "start_s": cursor,
            "end_s": round(cursor + duration, 3),
            "duration_s": duration,
            "segment_duration_s": round(seconds, 3),
            "segment_path": str(source),
            "clauses": entry.get("clauses") or [],
        })
        cursor = round(cursor + duration, 3)
        ctx.repo.record_usage(
            ctx.job.id, provider=manifest["provider"], model=manifest["model"], kind="tts_import",
            units={"scenes": 1, "seconds": seconds},
            meta={"scene": index, "voice": manifest["voice"], "network_generation": False},
        )
    raw = ffmpeg.concat_audio(tracks, voice_dir / "voiceover_raw.wav", sample_rate=48000)
    voiceover = postprocess_voice(ffmpeg, raw, voice_dir / "voiceover.wav", target_lufs=-16)
    backup = ctx.artifact_path("storyboard_planned.json")
    if not backup.exists():
        backup.write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
    retime_storyboard(storyboard, measured, max_shot_s=float(config.max_scene_seconds))
    # Prompt-directed delivery is not numeric rate/pitch/volume control.
    directed = manifest["delivery_control"] == "natural_language_prompt"
    storyboard["timeline"].update({"voice_profile": manifest["profile"], "prosody": directed})
    ctx.artifact_path("storyboard.json").write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    alignment = manifest.get("alignment_source", "scene_duration_proportional")
    segment_path = ctx.artifact_path("voice_segments.json")
    segment_path.write_text(json.dumps({
        "voice": manifest["voice"], "voice_profile": manifest["profile"],
        "delivery_control": manifest["delivery_control"], "alignment_source": alignment,
        "scenes": measured,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {
        "voice": manifest["voice"], "voice_profile": manifest["profile"],
        "tts_model": manifest["model"], "tts_engine": manifest["provider"],
        "prosody_supported": directed, "delivery_control": manifest["delivery_control"],
        "numeric_prosody_controls": [], "caption_alignment_source": alignment,
        "voiceover_path": str(voiceover), "total_seconds": cursor,
        "audio_duration_s": round(ffmpeg.duration(voiceover), 3),
        "loudness": ffmpeg.measure_loudness(voiceover),
        "peak_dbfs": ffmpeg.measure_peak(voiceover), "scenes": measured,
        "voice_segments": str(segment_path), "created_at": utcnow_iso(),
    }
    path = ctx.artifact_path("timings.json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "timings": str(path), "voice_segments": str(segment_path),
        "voiceover": str(voiceover), "voice": manifest["voice"],
        "voice_profile": manifest["profile"], "prosody_supported": directed,
        "total_seconds": cursor, "audio_duration_s": payload["audio_duration_s"],
        "peak_dbfs": payload["peak_dbfs"],
    }
