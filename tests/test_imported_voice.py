"""Imported audio must be bound to the exact scene text, file and timing."""
import hashlib
import json
from pathlib import Path

import pytest

from shortsgen.media.imported_voice import validate_voice_manifest
from shortsgen.media.subtitles import build_karaoke_cues_from_measured
from shortsgen.media.voice import choose_voice_profile


def make_manifest(directory: Path):
    directory.mkdir(exist_ok=True)
    content = b"completed-audio"
    (directory / "scene_00.wav").write_bytes(content)
    manifest = {
        "provider": "test", "model": "model", "voice": "Puck", "profile": "cartoon",
        "delivery_control": "natural_language_prompt",
        "scenes": [{"scene_index": 0, "text": "Bebek lewat!", "file": "scene_00.wav",
                    "duration_s": 2, "sha256": hashlib.sha256(content).hexdigest()}],
    }
    write_manifest(directory, manifest)
    return manifest


def write_manifest(directory, manifest):
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_imported_voice_validates_without_provider(tmp_path):
    make_manifest(tmp_path)
    manifest = validate_voice_manifest(tmp_path, [{"index": 0, "narration": "Bebek lewat!"}])
    assert manifest["entries"][0]["path"] == str((tmp_path / "scene_00.wav").resolve())


@pytest.mark.parametrize("fault", ["text", "hash", "duplicate", "missing", "escape", "alignment", "coverage"])
def test_imported_voice_rejects_stale_or_unsafe_assets(tmp_path, fault):
    manifest = make_manifest(tmp_path)
    entry = manifest["scenes"][0]
    if fault == "text":
        entry["text"] = "Narasi berubah"
    elif fault == "hash":
        entry["sha256"] = "wrong"
    elif fault == "duplicate":
        manifest["scenes"].append(dict(entry))
    elif fault == "missing":
        manifest["scenes"] = []
    elif fault == "escape":
        entry["file"] = "../outside.wav"
    elif fault == "alignment":
        entry["clauses"] = [{"text": "Bebek lewat!", "start_offset_s": 1, "duration_s": 4}]
    else:
        entry["clauses"] = [{"text": "Bebek", "start_offset_s": 0, "duration_s": 1}]
    write_manifest(tmp_path, manifest)
    with pytest.raises(ValueError):
        validate_voice_manifest(tmp_path, [{"index": 0, "narration": "Bebek lewat!"}])


def test_karaoke_respects_imported_alignment_offset():
    scenes = [{"index": 0, "start_s": 5, "end_s": 8, "narration": "Bebek lewat!"}]
    measured = [{"scene_index": 0, "clauses": [
        {"text": "Bebek lewat!", "start_offset_s": 0.6, "duration_s": 1.2}
    ]}]
    cues = build_karaoke_cues_from_measured(scenes, measured)
    assert cues[0].start_s == 5.6
    assert cues[-1].end_s == 6.8


def test_cheerful_kids_profile_uses_indonesian_female_voice():
    profile = choose_voice_profile("Bebek Kayuh", "kids", requested="cheerful_kids")
    assert profile.voice == "id-ID-GadisNeural"


def test_auto_voice_follows_cartoon_theme_and_explicit_profile_wins():
    assert choose_voice_profile("Kartun bebek lucu untuk anak", "trivia").id == "cheerful_kids"
    assert choose_voice_profile("Kartun bebek lucu", "trivia", requested="warm_curious").id == "warm_curious"


def test_turtle_and_hat_visual_plan_matches_indonesian_narration():
    from shortsgen.story.relevance import score_visual_plan

    result = score_visual_plan(
        "Topinya turun. Eh, ada kura-kura!", "red cap floating toward small green turtle",
        subject="red cap floating toward green turtle", topic="Bebek Kayuh: Topinya Kabur!",
    )
    assert "turtle" in result["shared_tokens"]
    assert result["score"] >= 6


def test_fingertip_and_water_plan_matches_indonesian_body_topic():
    from shortsgen.story.relevance import score_visual_plan

    result = score_visual_plan(
        "Jari keriput di air?", "wrinkled fingertip above water",
        subject="wrinkled fingertip", topic="Kenapa jari keriput di air?",
    )
    assert "fingertip" in result["shared_tokens"]
    assert "water" in result["shared_tokens"]
    assert result["score"] >= 6


def test_microwave_and_onion_plans_match_indonesian_narration():
    from shortsgen.story.relevance import score_visual_plan

    microwave = score_visual_plan(
        "Gelombang mikro memantul dan membentuk pola gelombang berdiri.",
        "standing wave pattern with hot and cold spots inside the microwave oven cavity",
        subject="standing wave pattern", topic="Kenapa microwave memanaskan tidak merata?",
    )
    assert "wave" in microwave["shared_tokens"]
    assert "standing" in microwave["shared_tokens"]
    assert microwave["score"] >= 6

    onion = score_visual_plan(
        "Begitu dipotong, sel-selnya pecah dan enzim bertemu senyawa belerang.",
        "onion cells breaking open after cutting, enzyme meets sulfur compounds",
        subject="onion cells breaking", topic="Kenapa bawang merah membuat menangis?",
    )
    assert "onion" in onion["shared_tokens"]
    assert "enzyme" in onion["shared_tokens"]
    assert onion["score"] >= 6


def test_pineapple_plan_matches_indonesian_narration():
    from shortsgen.story.relevance import score_visual_plan

    result = score_visual_plan(
        "Bromelain adalah campuran enzim pemecah protein dari nanas.",
        "fresh pineapple chunks rich in the bromelain protein digesting enzyme",
        subject="pineapple chunks", topic="Kenapa nanas membuat lidah perih?",
    )
    assert "pineapple" in result["shared_tokens"]
    assert "enzyme" in result["shared_tokens"]
    assert result["score"] >= 6
