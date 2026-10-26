"""Voice acting engine: profil suara + emosi per klausa + post-processing.

Kontrol yang dipakai hanya yang benar-benar didukung:
- edge-tts lokal: `rate`, `pitch`, `volume` (terverifikasi live).
- 9Router TTS: hanya teks/voice; prosody TIDAK dikirim (fallback datar, dicatat).

Emosi dibuat dari kombinasi kecepatan, pitch, volume, dan jeda nyata antar
klausa. Ini bukan akting manusia; klaim kualitas harus diukur (F0/rate/durasi).
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..providers.base import TTSResult

MIN_CLAUSE_WORDS = 3
MAX_CLAUSE_WORDS = 12


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    voice: str
    description: str
    base_rate: int          # persen, mis. +2
    base_pitch: int         # Hz, mis. 0
    archetypes: tuple[str, ...]


VOICE_PROFILES: dict[str, VoiceProfile] = {
    "warm_curious": VoiceProfile(
        id="warm_curious",
        name="Warm Curious",
        voice="id-ID-ArdiNeural",
        description="pria hangat, penasaran, cocok untuk sains/alam/sejarah",
        base_rate=2,
        base_pitch=0,
        archetypes=("science", "nature", "history", "animals", "space", "default"),
    ),
    "energetic_nerd": VoiceProfile(
        id="energetic_nerd",
        name="Energetic Nerd",
        voice="id-ID-ArdiNeural",
        description="pria cepat dan bersemangat, cocok untuk teknologi/trivia",
        base_rate=8,
        base_pitch=6,
        archetypes=("technology", "weird", "trivia", "pop_culture"),
    ),
    "calm_storyteller": VoiceProfile(
        id="calm_storyteller",
        name="Calm Storyteller",
        voice="id-ID-GadisNeural",
        description="perempuan tenang, cocok untuk psikologi/tubuh/kuliner",
        base_rate=-2,
        base_pitch=0,
        archetypes=("psychology", "body", "food"),
    ),
    "cheerful_kids": VoiceProfile(
        id="cheerful_kids",
        name="Cheerful Kids",
        voice="id-ID-GadisNeural",
        description="perempuan ceria dan ramah, cocok untuk hiburan anak",
        base_rate=10,
        base_pitch=8,
        archetypes=("kids", "trivia", "pop_culture", "weird"),
    ),
}

EMOTION_PRESETS: dict[str, dict[str, float]] = {
    # rate_delta (%), pitch_delta (Hz), volume_delta (%), pause_before_s, pause_after_s
    "curious": {"rate": 4, "pitch": 6, "volume": 0, "pause_before": 0.06, "pause_after": 0.10},
    "excited": {"rate": 14, "pitch": 18, "volume": 2, "pause_before": 0.0, "pause_after": 0.06},
    "suspenseful": {"rate": -12, "pitch": -10, "volume": -2, "pause_before": 0.14, "pause_after": 0.18},
    "surprised": {"rate": 16, "pitch": 26, "volume": 2, "pause_before": 0.0, "pause_after": 0.10},
    "conversational": {"rate": 0, "pitch": 0, "volume": 0, "pause_before": 0.04, "pause_after": 0.06},
    "comedic": {"rate": 8, "pitch": 10, "volume": 0, "pause_before": 0.05, "pause_after": 0.12},
    "serious": {"rate": -6, "pitch": -6, "volume": 0, "pause_before": 0.08, "pause_after": 0.10},
}

NEUTRAL_EMOTION = {
    "rate": 0,
    "pitch": 0,
    "volume": 0,
    "pause_before": 0.04,
    "pause_after": 0.06,
}

EMOTION_SAMPLE_TEXT = {
    "curious": "Tapi ada satu detail yang membuat semuanya jadi aneh.",
    "excited": "Dan inilah bagian yang paling mengejutkan!",
    "suspenseful": "Perlahan, sesuatu mulai bergerak di kegelapan.",
    "surprised": "Ternyata jawabannya sama sekali tidak terduga.",
    "conversational": "Oke, kita mulai dari hal yang paling sederhana.",
    "comedic": "Jadi, alam punya selera humor yang cukup aneh.",
    "serious": "Prinsip ini sudah terbukti lewat banyak pengamatan.",
}


def choose_voice_profile(
    topic: str,
    archetype: str,
    *,
    requested: str = "auto",
    fallback_voice_mode: str = "auto",
) -> VoiceProfile:
    """Pilih profil suara deterministik dari arketipe topik."""
    requested = (requested or "auto").strip().lower()
    if requested not in ("", "auto") and requested in VOICE_PROFILES:
        return VOICE_PROFILES[requested]
    topic_words = set(re.findall(r"[a-z]+", (topic or "").lower()))
    if topic_words & {"kartun", "brainrot", "anak", "kids", "cartoon"}:
        return VOICE_PROFILES["cheerful_kids"]
    for profile in VOICE_PROFILES.values():
        if archetype in profile.archetypes:
            return profile
    if (fallback_voice_mode or "").lower() == "female":
        return VOICE_PROFILES["calm_storyteller"]
    return VOICE_PROFILES["warm_curious"]


def _format_percent(value: float) -> str:
    rounded = int(round(value))
    return f"{rounded:+d}%"


def _format_hz(value: float) -> str:
    rounded = int(round(value))
    return f"{rounded:+d}Hz"


def split_clauses(text: str) -> list[str]:
    """Pecah narasi menjadi klausa pendek yang enak diucapkan.

    Utamakan tanda baca; bila tidak ada, pecah pada konjungsi agar tidak
    memotong frasa di tengah.
    """
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|(?<=[,;:])\s+|\s+[—–]\s+", cleaned)
    clauses: list[str] = []
    for part in parts:
        part = part.strip(" ,;:—–")
        if not part:
            continue
        words = part.split()
        if len(words) <= MAX_CLAUSE_WORDS:
            clauses.append(part)
            continue
        # pecah pada konjungsi terdekat di tengah
        chunks: list[str] = []
        current: list[str] = []
        for word in words:
            current.append(word)
            if (
                len(current) >= MIN_CLAUSE_WORDS
                and word.lower().strip(",.;:") in ("tapi", "karena", "dan", "lalu", "saat", "jadi")
                and len(current) >= 5
            ):
                chunks.append(" ".join(current))
                current = []
        if current:
            chunks.append(" ".join(current))
        for chunk in chunks:
            if len(chunk.split()) > MAX_CLAUSE_WORDS:
                words_chunk = chunk.split()
                for start in range(0, len(words_chunk), MAX_CLAUSE_WORDS):
                    clauses.append(" ".join(words_chunk[start : start + MAX_CLAUSE_WORDS]))
            else:
                clauses.append(chunk)
    return [clause for clause in clauses if clause]


def plan_scene_segments(
    scene: dict[str, Any],
    profile: VoiceProfile,
    *,
    emotion_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Rencana sintesis per klausa: teks + prosody + jeda + alasan."""
    emotion = str(scene.get("emotion") or "conversational").lower()
    preset = EMOTION_PRESETS.get(emotion, NEUTRAL_EMOTION) if emotion_enabled else NEUTRAL_EMOTION
    try:
        intensity = int(scene.get("intensity") or 3)
    except (TypeError, ValueError):
        intensity = 3
    intensity = max(1, min(5, intensity))
    # intensitas memperbesar delta prosody (1 -> 0.75x, 5 -> 1.35x)
    scale = 0.6 + 0.15 * intensity
    emphasis = [str(word).lower() for word in (scene.get("subtitle_emphasis") or scene.get("emphasis") or [])]
    clauses = split_clauses(str(scene.get("narration") or ""))
    segments: list[dict[str, Any]] = []
    for position, clause in enumerate(clauses):
        clause_lower = clause.lower()
        hit = any(word and word in clause_lower for word in emphasis)
        rate = profile.base_rate + preset["rate"] * scale
        pitch = profile.base_pitch + preset["pitch"] * scale
        volume = preset["volume"] * scale
        reason = f"emosi {emotion} intensitas {intensity}"
        if hit:
            rate -= 3
            pitch += 4
            volume += 3
            reason += " + penekanan kata kunci"
        segments.append(
            {
                "index": position,
                "text": clause,
                "emotion": emotion,
                "intensity": intensity,
                "emphasis_hit": hit,
                "rate": _format_percent(rate),
                "pitch": _format_hz(pitch),
                "volume": _format_percent(volume),
                "pause_before_s": round(preset["pause_before"] * (1.4 if position == 0 else 1.0), 3),
                "pause_after_s": round(
                    preset["pause_after"] * (1.35 if position == len(clauses) - 1 else 1.0), 3
                ),
                "reason": reason,
            }
        )
    return segments


def synthesize_scene(
    provider: Any,
    segments: list[dict[str, Any]],
    out_path: Path,
    *,
    voice: str,
    ffmpeg: Any,
    sample_rate: int = 48000,
) -> tuple[Path, list[dict[str, Any]], list[TTSResult]]:
    """Sintesis tiap klausa dengan prosody, gabung dengan jeda nyata.

    Hanya parameter yang didukung provider yang dikirim (inspect.signature).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        supported = set(inspect.signature(provider.synthesize).parameters)
    except (TypeError, ValueError):
        supported = {"text", "out_path", "voice"}
    results: list[TTSResult] = []
    padded: list[Path] = []
    measured: list[dict[str, Any]] = []
    for segment in segments:
        raw_path = out_path.with_name(f"{out_path.stem}_c{segment['index']:02d}{out_path.suffix}")
        kwargs: dict[str, Any] = {"voice": voice}
        if "rate" in supported:
            kwargs["rate"] = segment["rate"]
        if "pitch" in supported:
            kwargs["pitch"] = segment["pitch"]
        if "volume" in supported:
            kwargs["volume"] = segment["volume"]
        result = provider.synthesize(segment["text"], raw_path, **kwargs)
        results.append(result)
        duration = ffmpeg.duration(raw_path)
        pause_after = float(segment.get("pause_after_s") or 0.0)
        target = max(0.35, duration + pause_after)
        pad_path = out_path.with_name(f"{out_path.stem}_c{segment['index']:02d}_pad.wav")
        ffmpeg.pad_audio_to(raw_path, pad_path, target, sample_rate=sample_rate)
        padded.append(pad_path)
        measured.append(
            {
                "index": segment["index"],
                "text": segment["text"],
                "duration_s": round(duration, 3),
                "padded_s": round(target, 3),
                "rate": segment["rate"],
                "pitch": segment["pitch"],
                "volume": segment["volume"],
                "emotion": segment["emotion"],
                "emphasis_hit": segment["emphasis_hit"],
                "raw_path": str(raw_path),
            }
        )
    if not padded:
        raise ValueError("tidak ada klausa untuk disintesis")
    combined_path = out_path.with_suffix(".wav")
    ffmpeg.concat_audio(padded, combined_path, sample_rate=sample_rate)
    return combined_path, measured, results


def postprocess_voice(ffmpeg: Any, in_path: Path, out_path: Path, *, target_lufs: float = -16.0) -> Path:
    """Rapikan voice-over: high-pass, presence EQ, kompresi, limiter, loudnorm.

    Rantai ini didukung FFmpeg standar; tidak mengklaim studio-grade mastering.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    filter_chain = (
        "highpass=f=80,"
        "equalizer=f=2500:t=q:w=1.0:g=2,"
        "acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
        "alimiter=limit=0.95,"
        f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    )
    ffmpeg.run(
        [
            "-y",
            "-i",
            str(in_path),
            "-af",
            filter_chain,
            "-ar",
            "48000",
            "-ac",
            "1",
            str(out_path),
        ]
    )
    return out_path


def write_voice_sample(
    provider: Any,
    out_path: Path,
    *,
    voice: str,
    ffmpeg: Any,
    sample_rate: int = 48000,
) -> dict[str, Any]:
    """Buat sampel semua emosi untuk pemeriksaan manusia (bukan klaim kualitas)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    padded: list[Path] = []
    entries: list[dict[str, Any]] = []
    for emotion, text in EMOTION_SAMPLE_TEXT.items():
        scene = {
            "narration": text,
            "emotion": emotion,
            "intensity": 4,
            "subtitle_emphasis": [],
        }
        profile = VoiceProfile(
            id="sample",
            name="sample",
            voice=voice,
            description="sample",
            base_rate=0,
            base_pitch=0,
            archetypes=(),
        )
        segments = plan_scene_segments(scene, profile, emotion_enabled=True)
        raw_path = out_path.with_name(f"sample_{emotion}{out_path.suffix}")
        scene_path, measured, _ = synthesize_scene(
            provider,
            segments,
            raw_path,
            voice=voice,
            ffmpeg=ffmpeg,
            sample_rate=sample_rate,
        )
        padded.append(scene_path)
        entries.append({"emotion": emotion, "text": text, "segments": measured})
    final_path = out_path.with_suffix(".wav")
    ffmpeg.concat_audio(padded, final_path, sample_rate=sample_rate)
    return {"path": str(final_path), "voice": voice, "emotions": entries}
