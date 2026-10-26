"""Optional local ASR check of the imported cartoon narration (no paid API)."""
from __future__ import annotations

import argparse
import json
import re

from faster_whisper import WhisperModel

from shortsgen.config import load_config


def word_edits(expected: str, transcript: str) -> tuple[int, int]:
    expected_words = re.findall(r"[a-z]+", expected.lower())
    heard_words = re.findall(r"[a-z]+", transcript.lower())
    previous = list(range(len(heard_words) + 1))
    for i, first in enumerate(expected_words, 1):
        current = [i]
        for j, second in enumerate(heard_words, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (first != second)))
        previous = current
    return previous[-1], len(expected_words)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("base", "small"), default="base")
    parser.add_argument("--job", required=True, action="append")
    parser.add_argument("--vocabulary", default="Bebek Kayuh. Balon cegukan. Kura-kura. Bek bek bek. Topi merah.")
    args = parser.parse_args()
    config = load_config()
    model = WhisperModel(
        args.model, device="cpu", compute_type="int8", cpu_threads=4,
        download_root=str(config.data_dir / "models"),
    )
    for job_id in args.job:
        if not job_id.startswith("job_") or not job_id.replace("_", "").isalnum():
            raise ValueError("Invalid job ID")
        root = config.jobs_dir / job_id
        directory = root / "voice" / "overrides"
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        results = []
        for entry in manifest["scenes"]:
            segments, info = model.transcribe(
                str(directory / entry["file"]), language="id", word_timestamps=True,
                condition_on_previous_text=False, beam_size=5, vad_filter=False,
                initial_prompt=args.vocabulary,
            )
            words = [
                {"word": word.word.strip(), "start": round(word.start, 3), "end": round(word.end, 3)}
                for segment in segments for word in (segment.words or [])
            ]
            transcript = " ".join(word["word"] for word in words)
            result = {"scene_index": entry["scene_index"], "expected": entry["text"],
                      "transcript": transcript, "words": words}
            results.append(result)
            print(job_id, result["scene_index"], "EXPECTED:", entry["text"], "ASR:", transcript, flush=True)
        path = root / "artifacts" / "voice_asr_review.json"
        edits = [word_edits(row["expected"], row["transcript"]) for row in results]
        total_edits, expected_words = sum(e[0] for e in edits), sum(e[1] for e in edits)
        path.write_text(json.dumps({"model": f"faster-whisper/{args.model}/int8", "scenes": results,
                                   "normalized_wer": {"edits": total_edits, "expected_words": expected_words,
                                                      "rate": round(total_edits / max(expected_words, 1), 4),
                                                      "note": "ASR is an imperfect check, not human listening."}},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
