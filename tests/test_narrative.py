"""Test narrative engine: arc, hook, claim type, emphasis, validasi."""
from __future__ import annotations

from shortsgen.story.narrative import (
    HOOK_PATTERNS,
    classify_topic,
    normalize_narrative,
    select_hook_pattern,
    validate_narrative,
)

FACTS = [
    {"claim": "Gurita memiliki tiga jantung.", "confidence": "high", "verification": "fetched_supported"},
    {"claim": "Darah gurita berwarna biru karena hemocyanin.", "confidence": "high", "verification": "no_source"},
    {"claim": "Jantung sistemik gurita berhenti saat berenang.", "confidence": "medium", "verification": "no_source"},
]


def _beats() -> list[dict]:
    return [
        {"role": "hook", "narration": "Gurita punya tiga jantung, dan satu berhenti saat berenang.",
         "fact_refs": ["F1"], "claim_type": "verified", "emotion": "curious", "intensity": 4,
         "emphasis": ["tiga", "berhenti"], "visual_seed": "octopus swimming deep ocean", "sfx_cue": "impact",
         "transition": "fade"},
        {"role": "context", "narration": "Hewan ini bukan ikan, tapi moluska laut.",
         "fact_refs": ["F1"], "claim_type": "theory", "emotion": "conversational", "intensity": 2,
         "visual_seed": "octopus on coral reef", "sfx_cue": "whoosh", "transition": "smoothleft"},
        {"role": "escalation", "narration": "Dua jantung kecil memompa darah ke insang.",
         "fact_refs": ["F1"], "claim_type": "theory", "emotion": "curious", "intensity": 3,
         "visual_seed": "close up octopus gills underwater", "sfx_cue": "whoosh", "transition": "smoothleft"},
        {"role": "escalation", "narration": "Satu jantung besar memompa ke seluruh tubuh.",
         "fact_refs": ["F1"], "claim_type": "theory", "emotion": "curious", "intensity": 3,
         "visual_seed": "octopus anatomy heart diagram", "sfx_cue": "none", "transition": "cut"},
        {"role": "evidence", "narration": "Darahnya biru karena memakai tembaga, bukan besi.",
         "fact_refs": ["F2"], "claim_type": "theory", "emotion": "serious", "intensity": 3,
         "emphasis": ["biru", "tembaga"], "visual_seed": "blue blood microscope macro", "sfx_cue": "none",
         "transition": "cut"},
        {"role": "evidence", "narration": "Saat berenang, jantung utamanya berhenti sebentar.",
         "fact_refs": ["F3"], "claim_type": "theory", "emotion": "suspenseful", "intensity": 4,
         "visual_seed": "octopus jet propulsion swimming", "sfx_cue": "heartbeat", "transition": "cut"},
        {"role": "twist", "narration": "Itu sebabnya gurita lebih suka merangkak di dasar laut.",
         "fact_refs": ["F3"], "claim_type": "theory", "emotion": "surprised", "intensity": 4,
         "visual_seed": "octopus crawling on seafloor", "sfx_cue": "riser", "transition": "zoomin"},
        {"role": "payoff", "narration": "Tiga jantung, darah biru, dan satu tubuh yang sangat efisien.",
         "fact_refs": ["F1", "F2"], "claim_type": "theory", "emotion": "excited", "intensity": 5,
         "visual_seed": "majestic octopus silhouette blue water", "sfx_cue": "impact", "transition": "circleopen"},
        {"role": "closing", "narration": "Jadi, gurita bukan sekadar hewan laut biasa.",
         "fact_refs": [], "claim_type": "theory", "emotion": "comedic", "intensity": 3,
         "visual_seed": "octopus looking at camera", "sfx_cue": "sparkle", "transition": "fade"},
    ]


def test_classify_topic() -> None:
    assert classify_topic("Mengapa gurita punya tiga jantung?") == "animals"
    assert classify_topic("Bagaimana pelangi terbentuk?") == "nature"
    assert classify_topic("Sejarah kerajaan Majapahit") == "history"
    assert classify_topic("Sesuatu yang tidak dikenal") == "trivia"


def test_select_hook_pattern_is_deterministic() -> None:
    first = select_hook_pattern("gurita", "animals")
    second = select_hook_pattern("gurita", "animals")
    assert first.id == second.id
    assert first.id in {pattern.id for pattern in HOOK_PATTERNS}


def test_normalize_narrative_builds_beats_and_blocks() -> None:
    script = normalize_narrative(
        {
            "title": "Gurita punya tiga jantung",
            "hook": "Gurita punya tiga jantung, dan satu berhenti saat berenang.",
            "hook_variants": ["Gurita punya tiga jantung.", "Darah gurita biru. Ini alasannya."],
            "beats": _beats(),
            "closing": "Jadi, gurita bukan sekadar hewan laut biasa.",
            "description": "Fakta gurita.",
            "tags": ["gurita", "laut", "biologi", "fakta", "hewan"],
            "facts_used": ["F1", "F2", "F3"],
        },
        FACTS,
        topic="gurita",
        target_duration=30,
    )
    assert len(script["beats"]) == 9
    assert len(script["blocks"]) == 9
    assert script["beats"][0]["role"] == "hook"
    assert script["beats"][-1]["role"] == "closing"
    assert script["stats"]["beat_count"] == 9
    assert script["narrative"]["arc_roles"][2] == "escalation"
    assert script["narrative"]["claim_type_counts"]["verified"] >= 1
    # emphasis hanya kata yang ada di narasi
    for beat in script["beats"]:
        for word in beat["emphasis"]:
            assert word.lower() in beat["narration"].lower()
    # blocks tetap kompatibel dengan stage lama
    assert script["blocks"][0]["kind"] == "hook"
    assert script["blocks"][-1]["kind"] == "closing"
    assert script["blocks"][0]["visual_prompt"]
    assert script["sources"][0]["id"] == "F1"


def test_normalize_narrative_accepts_legacy_scenes() -> None:
    script = normalize_narrative(
        {
            "title": "Legacy",
            "hook": "Ternyata ini bukan seperti yang kita kira.",
            "scenes": [
                {"narration": "Konteks pertama.", "visual_prompt": "a lab"},
                {"narration": "Eskalasi kedua.", "visual_prompt": "a microscope"},
            ],
            "closing": "Jadi begitulah ceritanya.",
            "facts_used": ["F1"],
        },
        FACTS,
        topic="legacy",
        target_duration=30,
    )
    assert len(script["beats"]) >= 4
    assert any(beat["role"] == "context" for beat in script["beats"])
    assert any(beat["role"] == "closing" for beat in script["beats"])


def test_validate_narrative_blocks_generic_opening() -> None:
    script = normalize_narrative(
        {
            "title": "Test",
            "hook": "Tahukah kamu bahwa gurita punya tiga jantung?",
            "beats": _beats(),
            "closing": "Jadi, gurita bukan sekadar hewan laut biasa.",
            "tags": ["a", "b", "c", "d", "e"],
        },
        FACTS,
        topic="gurita",
        target_duration=30,
    )
    issues = validate_narrative(script)
    assert any(issue["code"] == "generic_opening" and issue["severity"] == "high" for issue in issues)


def test_validate_narrative_blocks_unverified_verified_claim() -> None:
    beats = _beats()
    beats[0]["claim_type"] = "verified"
    script = normalize_narrative(
        {
            "title": "Test",
            "hook": "Gurita punya tiga jantung, dan satu berhenti saat berenang.",
            "beats": beats,
            "closing": "Jadi, gurita bukan sekadar hewan laut biasa.",
            "tags": ["a", "b", "c", "d", "e"],
        },
        [{"claim": "Gurita punya tiga jantung.", "confidence": "high", "verification": "no_source"}],
        topic="gurita",
        target_duration=30,
    )
    issues = validate_narrative(script)
    # normalisasi menurunkan claim verified menjadi theory/speculation
    assert all(beat["claim_type"] != "verified" for beat in script["beats"])


def test_validate_narrative_flags_flat_emotion_and_arc() -> None:
    beats = [
        {"role": "hook", "narration": "Sesuatu yang aneh terjadi.", "emotion": "conversational"},
        {"role": "context", "narration": "Kita mulai dari awal.", "emotion": "conversational"},
        {"role": "closing", "narration": "Begitulah.", "emotion": "conversational"},
    ]
    script = normalize_narrative(
        {
            "title": "Test",
            "hook": "Sesuatu yang aneh terjadi.",
            "beats": beats,
            "closing": "Begitulah.",
            "tags": ["a", "b", "c", "d", "e"],
        },
        [],
        topic="test",
        target_duration=30,
    )
    codes = {issue["code"] for issue in validate_narrative(script)}
    assert "too_few_beats" in codes
    assert "missing_evidence" in codes
    assert "emotion_flat" in codes
