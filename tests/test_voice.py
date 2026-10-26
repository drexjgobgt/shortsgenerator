"""Test voice acting engine: klausa, prosody, penekanan, fallback argumen."""
from __future__ import annotations

from pathlib import Path

from shortsgen.media.voice import (
    EMOTION_PRESETS,
    choose_voice_profile,
    plan_scene_segments,
    split_clauses,
    synthesize_scene,
)
from shortsgen.providers.base import TTSResult


class FakeTTS:
    name = "fake-tts"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def synthesize(self, text: str, out_path: Path, *, voice=None, rate="+0%", pitch="+0Hz", volume="+0%"):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-audio")
        self.calls.append({"text": text, "voice": voice, "rate": rate, "pitch": pitch, "volume": volume})
        return TTSResult(path=out_path, provider=self.name, model="fake", bytes_written=10)


class FakeFFmpeg:
    def duration(self, path: Path) -> float:
        return 0.8

    def pad_audio_to(self, src: Path, out_path: Path, seconds: float, *, sample_rate: int = 48000) -> Path:
        Path(out_path).write_bytes(Path(src).read_bytes())
        return Path(out_path)

    def concat_audio(self, files, out_path: Path, *, sample_rate: int = 48000) -> Path:
        Path(out_path).write_bytes(b"".join(Path(f).read_bytes() for f in files))
        return Path(out_path)


def test_split_clauses_splits_on_punctuation_and_keeps_short_phrases() -> None:
    clauses = split_clauses("Pesawat itu berat. Tapi sayapnya membuat udara bergerak lebih cepat, dan itu kuncinya!")
    assert len(clauses) >= 3
    assert all(len(clause.split()) <= 12 for clause in clauses)
    assert clauses[0].startswith("Pesawat")


def test_plan_scene_segments_changes_prosody_by_emotion() -> None:
    profile = choose_voice_profile("teknologi", "technology")
    excited = plan_scene_segments(
        {"narration": "Ini bagian paling mengejutkan!", "emotion": "excited", "intensity": 5}, profile
    )
    suspense = plan_scene_segments(
        {"narration": "Ini bagian paling mengejutkan!", "emotion": "suspenseful", "intensity": 5}, profile
    )
    assert excited[0]["rate"] != suspense[0]["rate"]
    assert excited[0]["pitch"] != suspense[0]["pitch"]
    assert int(excited[0]["rate"].rstrip("%")) > int(suspense[0]["rate"].rstrip("%"))


def test_plan_scene_segments_marks_emphasis() -> None:
    profile = choose_voice_profile("gurita", "animals")
    segments = plan_scene_segments(
        {
            "narration": "Gurita punya tiga jantung.",
            "emotion": "curious",
            "intensity": 4,
            "subtitle_emphasis": ["tiga"],
        },
        profile,
    )
    assert segments[0]["emphasis_hit"] is True
    assert "penekanan" in segments[0]["reason"]


def test_plan_scene_segments_neutral_when_disabled() -> None:
    profile = choose_voice_profile("gurita", "animals")
    segments = plan_scene_segments(
        {"narration": "Gurita punya tiga jantung.", "emotion": "excited", "intensity": 5},
        profile,
        emotion_enabled=False,
    )
    assert segments[0]["rate"] == f"{profile.base_rate:+d}%"
    assert segments[0]["pitch"] == f"{profile.base_pitch:+d}Hz"


def test_synthesize_scene_passes_prosody_and_joins_wav(tmp_path: Path) -> None:
    provider = FakeTTS()
    ffmpeg = FakeFFmpeg()
    profile = choose_voice_profile("gurita", "animals")
    segments = plan_scene_segments(
        {"narration": "Gurita punya tiga jantung. Darahnya biru.", "emotion": "curious", "intensity": 3},
        profile,
    )
    out, measured, results = synthesize_scene(
        provider,
        segments,
        tmp_path / "scene.mp3",
        voice=profile.voice,
        ffmpeg=ffmpeg,
    )
    assert out.suffix == ".wav"
    assert len(results) == len(segments) >= 2
    assert provider.calls[0]["rate"].endswith("%")
    assert all(call["voice"] == profile.voice for call in provider.calls)
    assert all(item["duration_s"] == 0.8 for item in measured)


def test_emotion_presets_cover_all_used_emotions() -> None:
    assert set(EMOTION_PRESETS) == {
        "curious", "excited", "suspenseful", "surprised", "conversational", "comedic", "serious"
    }
