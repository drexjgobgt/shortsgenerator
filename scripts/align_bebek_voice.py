"""Use local ASR speech boundaries; word positions inside a scene stay estimated.

ASR output is retained verbatim for inspection. Original script text is used in
captions, never a possibly misrecognized transcription of a character name.
"""
from __future__ import annotations

import argparse
import json

from shortsgen.config import load_config
from shortsgen.media.imported_voice import validate_voice_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    config = load_config()
    if not args.job.startswith("job_") or not args.job.replace("_", "").isalnum():
        raise ValueError("Invalid job ID")
    root = config.jobs_dir / args.job
    artifacts = root / "artifacts"
    directory = root / "voice" / "overrides"
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    review = json.loads((artifacts / "voice_asr_review.json").read_text(encoding="utf-8"))
    by_index = {item["scene_index"]: item for item in review["scenes"]}
    for entry in manifest["scenes"]:
        words = by_index[entry["scene_index"]]["words"]
        if not words:
            raise ValueError("ASR tidak menemukan ucapan; tinjau audio sebelum render")
        start = max(0, float(words[0]["start"]))
        end = min(float(entry["duration_s"]), float(words[-1]["end"]))
        if end <= start:
            raise ValueError("Batas ucapan ASR tidak valid")
        entry["clauses"] = [{
            "text": entry["text"], "start_offset_s": round(start, 3),
            "duration_s": round(end - start, 3),
            "alignment_method": "asr_scene_boundaries_proportional_words",
        }]
        print(entry["scene_index"], start, end, by_index[entry["scene_index"]]["transcript"])
    manifest["alignment_source"] = "asr_scene_boundaries_proportional_words"
    manifest["asr_model"] = review["model"]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    board_path = artifacts / "storyboard.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    validate_voice_manifest(directory, board["scenes"])
    continuity = board["style_bible"]["continuity"]
    continuity["reference_chain"] = False
    continuity["reference_policy"] = "textual character description only; no reference image inputs"
    for scene in board["scenes"]:
        scene["visual_strategy"] = "ai_image"
    board["visual_plan"].update({"stock_clips_planned": 0, "strategies": ["ai_image"]})
    board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path = artifacts / "script.json"
    script = json.loads(script_path.read_text(encoding="utf-8"))
    script.setdefault("category_id", "24")
    script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
