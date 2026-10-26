"""Test style bible, storyboard v2, relevance gate, dan perencanaan render sinematik."""
from __future__ import annotations

from pathlib import Path

from shortsgen.config import Config
from shortsgen.media.cinematic import (
    build_xfade_filter,
    camera_filter,
    plan_render_shots,
    sfx_cues_for_shots,
)
from shortsgen.story.narrative import normalize_narrative
from shortsgen.story.relevance import (
    score_visual_plan,
    summarize_relevance,
    vision_check,
)
from shortsgen.story.storyboard import build_storyboard, retime_storyboard
from shortsgen.story.style_bible import build_style_bible, style_consistency_report, style_prompt

FACTS = [
    {"claim": "Gurita memiliki tiga jantung.", "confidence": "high", "verification": "fetched_supported"},
    {"claim": "Darah gurita berwarna biru.", "confidence": "high", "verification": "no_source"},
]


def _script() -> dict:
    beats = [
        {"role": "hook", "narration": "Gurita punya tiga jantung, satu berhenti saat berenang.",
         "emotion": "curious", "intensity": 4, "fact_refs": ["F1"], "visual_seed": "octopus swimming deep ocean",
         "sfx_cue": "impact", "transition": "fade", "emphasis": ["tiga"], "on_screen_text": "3 jantung"},
        {"role": "context", "narration": "Hewan ini hidup di dasar laut yang gelap.",
         "emotion": "conversational", "intensity": 2, "fact_refs": ["F1"],
         "visual_seed": "octopus crawling on seafloor", "sfx_cue": "whoosh", "transition": "smoothleft"},
        {"role": "escalation", "narration": "Dua jantung kecil memompa darah ke insang.",
         "emotion": "curious", "intensity": 3, "fact_refs": ["F1"],
         "visual_seed": "close up octopus gills underwater", "sfx_cue": "whoosh", "transition": "smoothleft"},
        {"role": "evidence", "narration": "Darahnya biru karena memakai tembaga.",
         "emotion": "serious", "intensity": 3, "fact_refs": ["F2"], "visual_seed": "blue blood microscope macro",
         "sfx_cue": "none", "transition": "cut", "emphasis": ["biru"]},
        {"role": "twist", "narration": "Saat berenang, jantung utamanya berhenti sebentar.",
         "emotion": "surprised", "intensity": 4, "fact_refs": ["F1"], "visual_seed": "octopus jet propulsion",
         "sfx_cue": "riser", "transition": "zoomin"},
        {"role": "payoff", "narration": "Tiga jantung membuat gurita sangat efisien.",
         "emotion": "excited", "intensity": 5, "fact_refs": ["F1", "F2"],
         "visual_seed": "majestic octopus silhouette blue water", "sfx_cue": "impact", "transition": "circleopen"},
        {"role": "closing", "narration": "Jadi, gurita bukan hewan laut biasa.",
         "emotion": "comedic", "intensity": 3, "visual_seed": "octopus looking at camera",
         "sfx_cue": "sparkle", "transition": "fade"},
    ]
    return normalize_narrative(
        {
            "title": "Gurita tiga jantung",
            "hook": "Gurita punya tiga jantung, satu berhenti saat berenang.",
            "beats": beats,
            "closing": "Jadi, gurita bukan hewan laut biasa.",
            "tags": ["gurita", "laut", "biologi", "fakta", "hewan"],
        },
        FACTS,
        topic="gurita",
        target_duration=30,
    )


def test_style_bible_is_deterministic_and_has_continuity() -> None:
    first = build_style_bible("gurita", "animals")
    second = build_style_bible("gurita", "animals")
    assert first == second
    assert first["palette"]
    assert first["continuity"]["hero_object"]
    assert "same color palette" in first["continuity"]["anchors"]
    assert first["negative_prompt"]


def test_style_prompt_includes_style_and_continuity() -> None:
    style = build_style_bible("gurita", "animals")
    scene = {
        "subject": "an octopus with three hearts",
        "action": "swimming through deep water",
        "location": "deep ocean",
        "composition": "centered hero composition",
        "camera_move": "push_in",
        "lighting": "volumetric light",
    }
    prompt = style_prompt(style, scene)
    assert style["style_prefix"][:20] in prompt
    assert "an octopus with three hearts" in prompt
    assert "Negative prompt" in prompt


def test_build_storyboard_has_all_required_fields_and_shots_sum() -> None:
    script = _script()
    style = build_style_bible("gurita", "animals")
    config = Config(
        mode="mock", data_dir=Path("."), logs_dir=Path("."),
        nine_router_base_url="http://localhost", nine_router_api_key="",
        nine_router_api_key_from_local_db=False, llm_model="m", llm_model_fallbacks=(),
        tts_model="t", image_model="i", video_model="", stt_model="",
        search_provider="", fetch_provider="", http_timeout_s=30, http_max_retries=1,
        max_video_seconds=120, max_asset_mb=50, max_parallel_jobs=1, disk_quota_mb=100,
        target_duration_s=30, script_max_scenes=10, review_llm_pass=False,
        block_unverified_claims=False, max_ai_images_per_job=8, tts_voice_mode="auto",
        tts_engine="auto", tts_voice_profile="auto", tts_emotion=True, tts_postprocess=True,
        max_scene_seconds=4.0, captions_style="dynamic", hook_overlay=True,
        music_mode="off", music_dir="", music_volume_db=-18.0, music_bpm=120,
        sfx_enabled=False, sfx_mode="narrative", render_preset="fast",
        render_engine="cinematic", motion_intensity="medium", transitions_enabled=True,
        music_sections=True, render_width=720, render_height=1280, render_fps=30,
        series_character="", reference_chain=True, visual_style="", stock_policy="ai_first",
        stock_max_clips_per_job=2, visual_relevance_check="heuristic", visual_relevance_min=6.0,
        vision_model="m", evaluation_report=True, pexels_api_key="", pixabay_api_key="",
        stock_local_dir="", stock_prefer="pixabay", allow_paid_providers=False,
        upload_visibility="private", require_approval=True, ffmpeg_path="ffmpeg",
        cache_enabled=False, cache_dir=Path("cache"), cache_ttl_hours=0.0,
    )
    board = build_storyboard(script | {"topic": "gurita"}, style, config, stock_available=False, image_status="available")
    required = {
        "id", "duration_s", "role", "narration", "subject", "action", "location",
        "visual_type", "composition", "camera_move", "camera_reason", "lighting",
        "palette", "transition_in", "voice_direction", "subtitle_emphasis", "sfx_cue",
        "continuity_ref", "narrative_purpose", "shot_plan", "visual_prompt",
    }
    for scene in board["scenes"]:
        assert required <= set(scene)
        assert scene["shot_plan"]
        assert scene["camera_reason"]
        total = sum(shot["duration_s"] for shot in scene["shot_plan"])
        assert abs(total - scene["duration_s"]) < 0.05
        assert all(shot["camera_reason"] for shot in scene["shot_plan"])
    assert board["visual_plan"]["reference_chain"] is True


def test_retime_storyboard_resplits_long_shots() -> None:
    script = _script()
    style = build_style_bible("gurita", "animals")
    board = build_storyboard(script | {"topic": "gurita"}, style, _config(), stock_available=False, image_status="available")
    measured = [
        {"scene_index": scene["index"], "start_s": scene["start_s"], "duration_s": 9.5}
        for scene in board["scenes"]
    ]
    retime_storyboard(board, measured, max_shot_s=4.0)
    for scene in board["scenes"]:
        assert max(shot["duration_s"] for shot in scene["shot_plan"]) <= 4.05


def test_relevance_heuristic_prefers_concrete_match() -> None:
    good = score_visual_plan(
        "Gurita punya tiga jantung.",
        "octopus swimming in deep ocean",
        subject="octopus",
    )
    generic = score_visual_plan(
        "Gurita punya tiga jantung.",
        "cinematic beautiful mysterious abstract background",
    )
    assert good["score"] > generic["score"]
    assert generic["needs_human_review"] is True


def test_relevance_heuristic_matches_indonesian_jellyfish_topic() -> None:
    result = score_visual_plan(
        "Ada satu trik curang ubur-ubur saat menua.",
        "translucent jellyfish floating slowly downwards",
        subject="translucent jellyfish",
        topic="Mengapa ubur-ubur Turritopsis bisa kembali menjadi bayi?",
    )
    assert result["score"] >= 6
    assert result["needs_human_review"] is False


class FakeLLM:
    def __init__(self, text: str):
        self.text = text

    def chat(self, messages, **kwargs):
        class Result:
            model = "fake-vision"
            def __init__(self, text):
                self.text = text
        return Result(self.text)


def test_vision_check_parses_score(tmp_path: Path) -> None:
    image = tmp_path / "scene.png"
    image.write_bytes(b"fake-image")
    llm = FakeLLM('{"score": 8, "reason": "subjek terlihat", "better_prompt": ""}')
    result = vision_check(llm, image, narration="gurita", visual_seed="octopus")
    assert result["score"] == 8.0
    assert result["method"] == "vision"
    assert result["needs_human_review"] is False


def test_summarize_relevance_flags_low_scores() -> None:
    summary = summarize_relevance(
        [
            {"scene_index": 0, "relevance": {"score": 8, "method": "heuristic", "needs_human_review": False}},
            {"scene_index": 1, "relevance": {"score": 3, "method": "heuristic", "needs_human_review": True}},
            {"scene_index": 2, "relevance": None},
        ],
        threshold=6.0,
    )
    assert summary["scenes_scored"] == 2
    assert summary["average_score"] == 5.5
    assert summary["below_threshold"] == [{"scene_index": 1, "score": 3}]
    assert summary["needs_human_review"] == [1]


def test_plan_render_shots_uses_storyboard_shot_plan() -> None:
    board = build_storyboard(_script() | {"topic": "gurita"}, build_style_bible("gurita", "animals"), _config(), stock_available=False, image_status="available")
    for scene in board["scenes"]:
        scene["asset"] = {"path": "asset.png"}
    shots = plan_render_shots(board["scenes"], max_shot_s=4.0)
    assert len(shots) == sum(len(scene["shot_plan"]) for scene in board["scenes"])
    assert all(shot["camera_reason"] for shot in shots)
    assert all(shot["asset"] == "asset.png" for shot in shots)


def test_camera_filters_differ_by_move_and_are_not_linear() -> None:
    push = camera_filter("push_in", width=720, height=1280, fps=30, frames=60)
    pan = camera_filter("pan_left", width=720, height=1280, fps=30, frames=60)
    assert "zoompan" in push and "pow(on/60" in push
    assert push != pan
    assert "sin" in camera_filter("orbit", width=720, height=1280, fps=30, frames=60)


def test_video_pan_filter_has_numeric_duration() -> None:
    # Regresi: filter pan video sempat memuat literal "{dur}" dan membuat FFmpeg
    # gagal ("Undefined constant ... '{dur})'").
    pan = camera_filter("pan_left", width=1080, height=1920, fps=30, frames=90, is_image=False)
    assert "{dur}" not in pan
    assert "t/3.000" in pan
    right = camera_filter("pan_right", width=1080, height=1920, fps=30, frames=90, is_image=False)
    assert "t/3.000" in right


def test_xfade_offsets_keep_total_duration() -> None:
    shots = [
        {"duration_s": 2.0, "transition_in": "fade"},
        {"duration_s": 3.0, "transition_in": "smoothleft"},
        {"duration_s": 2.5, "transition_in": "circleopen"},
    ]
    filter_complex, label = build_xfade_filter(shots, duration_s=0.25)
    assert label == "[x2]"
    assert "offset=1.750" in filter_complex
    assert "offset=4.750" in filter_complex


def test_sfx_cues_include_narrative_and_transition() -> None:
    shots = [
        {"start_s": 0.0, "sfx_cue": "impact", "transition_in": "fade"},
        {"start_s": 2.0, "sfx_cue": "riser", "transition_in": "smoothleft"},
    ]
    cues = sfx_cues_for_shots(shots)
    kinds = {kind for _, kind in cues}
    assert "riser" in kinds
    assert "whoosh" in kinds


def test_evaluation_prefers_final_mix_measurements() -> None:
    from shortsgen.qa.evaluation import build_evaluation, render_evaluation_markdown

    evaluation = build_evaluation(
        config=_config(),
        topic="topik uji",
        script={},
        storyboard={},
        timings={"loudness": {"input_i": -18.0, "input_tp": -1.5}, "peak_dbfs": -1.5},
        render={"duration_s": 30.0, "shots": []},
        assets={"assets": []},
        relevance={},
        subtitles={},
        qa={
            "measurements": {
                "audio": {"loudness": {"input_i": -16.0, "input_tp": -4.1}, "peak_dbfs": -4.1}
            }
        },
        usage={},
    )
    assert evaluation["audio"]["measurement_source"] == "final_mix"
    assert evaluation["audio"]["integrated_lufs"] == -16.0
    assert evaluation["audio"]["true_peak_dbtp"] == -4.1
    assert "Loudness (final mix)" in render_evaluation_markdown(evaluation)


def _config() -> Config:
    return Config(
        mode="mock", data_dir=Path("."), logs_dir=Path("."),
        nine_router_base_url="http://localhost", nine_router_api_key="",
        nine_router_api_key_from_local_db=False, llm_model="m", llm_model_fallbacks=(),
        tts_model="t", image_model="i", video_model="", stt_model="",
        search_provider="", fetch_provider="", http_timeout_s=30, http_max_retries=1,
        max_video_seconds=120, max_asset_mb=50, max_parallel_jobs=1, disk_quota_mb=100,
        target_duration_s=30, script_max_scenes=10, review_llm_pass=False,
        block_unverified_claims=False, max_ai_images_per_job=8, tts_voice_mode="auto",
        tts_engine="auto", tts_voice_profile="auto", tts_emotion=True, tts_postprocess=True,
        max_scene_seconds=4.0, captions_style="dynamic", hook_overlay=True,
        music_mode="off", music_dir="", music_volume_db=-18.0, music_bpm=120,
        sfx_enabled=False, sfx_mode="narrative", render_preset="fast",
        render_engine="cinematic", motion_intensity="medium", transitions_enabled=True,
        music_sections=True, render_width=720, render_height=1280, render_fps=30,
        series_character="", reference_chain=True, visual_style="", stock_policy="ai_first",
        stock_max_clips_per_job=2, visual_relevance_check="heuristic", visual_relevance_min=6.0,
        vision_model="m", evaluation_report=True, pexels_api_key="", pixabay_api_key="",
        stock_local_dir="", stock_prefer="pixabay", allow_paid_providers=False,
        upload_visibility="private", require_approval=True, ffmpeg_path="ffmpeg",
        cache_enabled=False, cache_dir=Path("cache"), cache_ttl_hours=0.0,
    )
