"""Laporan evaluasi otomatis per video (bukti, bukan klaim estetika).

Laporan menyatukan:
- struktur cerita (arc, hook, klaim fakta)
- konsistensi visual (style bible, palet, reference chain)
- relevansi visual-narasi (heuristik/vision)
- editing (shot, cuts/min, transisi, camera moves)
- audio (profil suara, prosody, loudness, peak)
- caption (karaoke, overlay)
- lisensi aset + pemakaian provider
- daftar `needs_human_review`

Batas: skor relevansi/palet adalah heuristik yang bisa diperiksa, bukan
penilaian objektif. Laporan tidak menaikkan keyakinan fakta.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from ..models import utcnow_iso
from ..story.style_bible import style_consistency_report

EVALUATION_VERSION = 1


def evaluate_job(
    ctx: Any,
    config: Config,
    *,
    qa_report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Baca artefak job lalu susun evaluation.json + evaluation.md."""
    artifacts = ctx.artifacts_dir
    script = _load_optional(artifacts / "script.json")
    storyboard = _load_optional(artifacts / "storyboard.json")
    timings = _load_optional(artifacts / "timings.json")
    render = _load_optional(artifacts / "render.json")
    assets = _load_optional(artifacts / "assets.json")
    relevance = _load_optional(artifacts / "relevance.json")
    subtitles = _load_optional(artifacts / "subtitles.json")
    qa = qa_report or _load_optional(artifacts / "qa.json")
    usage = {}
    try:
        usage = ctx.repo.usage_summary(ctx.job.id)
    except Exception:
        usage = {}
    evaluation = build_evaluation(
        config=config,
        topic=getattr(ctx.job, "topic", ""),
        script=script,
        storyboard=storyboard,
        timings=timings,
        render=render,
        assets=assets,
        relevance=relevance,
        subtitles=subtitles,
        qa=qa,
        usage=usage,
    )
    write_evaluation(evaluation, artifacts)
    return evaluation


def build_evaluation(
    *,
    config: Config,
    topic: str,
    script: dict[str, Any],
    storyboard: dict[str, Any],
    timings: dict[str, Any],
    render: dict[str, Any],
    assets: dict[str, Any],
    relevance: dict[str, Any],
    subtitles: dict[str, Any],
    qa: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    narrative = script.get("narrative") or {}
    stats = script.get("stats") or {}
    beats = script.get("beats") or []
    style = storyboard.get("style_bible") or {}
    scenes = storyboard.get("scenes") or []
    duration = float((render.get("duration_s") or 0.0))
    shots = render.get("shots") or []

    claim_counts = narrative.get("claim_type_counts") or stats.get("claim_types") or {}
    verified = int(claim_counts.get("verified") or 0)
    theory = int(claim_counts.get("theory") or 0)
    speculation = int(claim_counts.get("speculation") or 0)
    dramatization = int(claim_counts.get("dramatization") or 0)

    relevance_score = relevance.get("average_score")
    below = relevance.get("below_threshold") or []
    human_review: list[dict[str, str]] = []
    if relevance.get("needs_human_review"):
        human_review.append(
            {
                "code": "visual_relevance",
                "message": "scene dengan relevansi rendah: "
                + ", ".join(str(i) for i in relevance["needs_human_review"]),
            }
        )
    if below:
        human_review.append(
            {"code": "visual_below_threshold", "message": f"{len(below)} scene di bawah ambang relevansi"}
        )
    if speculation or dramatization:
        human_review.append(
            {
                "code": "claim_mix",
                "message": f"{speculation} beat spekulasi, {dramatization} dramatisasi — pastikan tidak disajikan sebagai fakta",
            }
        )
    if qa.get("issues"):
        high = [issue for issue in qa["issues"] if issue.get("severity") == "high"]
        if high:
            human_review.append({"code": "qa_blocking", "message": "; ".join(i["message"] for i in high[:3])})
    if timings.get("prosody_supported") is False:
        human_review.append(
            {
                "code": "prosody_fallback",
                "message": "provider TTS tidak mendukung prosody; voice-over memakai fallback datar",
            }
        )
    missing_license = [
        item for item in (assets.get("assets") or []) if not item.get("license") or not item.get("usage_allowed")
    ]
    if missing_license:
        human_review.append(
            {"code": "license", "message": f"{len(missing_license)} aset tanpa lisensi/penggunaan yang jelas"}
        )

    asset_paths = [
        Path(str(item.get("path")))
        for item in (assets.get("assets") or [])
        if item.get("kind") in ("generated_image", "procedural") and item.get("path")
    ]
    palette_report = style_consistency_report(style, asset_paths) if asset_paths else None

    editing = {
        "engine": render.get("engine", "legacy"),
        "shot_count": len(shots),
        "max_shot_s": render.get("max_shot_s"),
        "cuts_per_minute": round(len(shots) / max(duration, 1e-6) * 60, 1) if shots else 0,
        "transitions": render.get("transitions") or [],
        "camera_moves": render.get("camera_moves") or [],
        "camera_move_variety": len(set(render.get("camera_moves") or [])),
        "motion_intensity": render.get("motion_intensity"),
        "music_included": bool(render.get("music")),
        "sfx_included": bool(render.get("sfx_included")),
    }
    qa_audio = (qa.get("measurements") or {}).get("audio") or {}
    final_loudness = qa_audio.get("loudness") or {}
    voice_loudness = timings.get("loudness") or {}
    audio_loudness = final_loudness or voice_loudness
    audio_source = "final_mix" if final_loudness else "voiceover"
    audio = {
        "voice": timings.get("voice"),
        "voice_profile": timings.get("voice_profile"),
        "engine": timings.get("tts_engine"),
        "model": timings.get("tts_model"),
        "delivery_control": timings.get("delivery_control", "provider_prosody"),
        "numeric_prosody_controls": timings.get("numeric_prosody_controls"),
        "caption_alignment_source": timings.get("caption_alignment_source", "measured_clauses"),
        "prosody_supported": timings.get("prosody_supported"),
        "emotion_variety": stats.get("emotion_variety") or narrative.get("emotion_variety"),
        "loudness": audio_loudness,
        "integrated_lufs": audio_loudness.get("input_i"),
        "true_peak_dbtp": audio_loudness.get("input_tp"),
        "peak_dbfs": qa_audio.get("peak_dbfs", timings.get("peak_dbfs")),
        "measurement_source": audio_source,
        "duration_s": timings.get("audio_duration_s"),
    }
    visual = {
        "style_id": style.get("id"),
        "style_name": style.get("name"),
        "palette": style.get("palette") or [],
        "reference_chain": bool((style.get("continuity") or {}).get("reference_chain")),
        "scene_count": len(scenes),
        "strategies": (storyboard.get("visual_plan") or {}).get("strategies") or [],
        "relevance_average": relevance_score,
        "relevance_threshold": relevance.get("threshold"),
        "palette_consistency": palette_report,
    }
    facts = {
        "sources": script.get("sources") or [],
        "facts_used": script.get("facts_used") or [],
        "unknown_fact_refs": script.get("unknown_fact_refs") or [],
        "claim_type_counts": claim_counts,
        "verified_claims": verified,
        "theory_claims": theory,
        "speculation_claims": speculation,
        "dramatization_claims": dramatization,
    }
    licensing = {
        "assets": [
            {
                "scene_index": item.get("scene_index"),
                "kind": item.get("kind"),
                "source": item.get("source"),
                "license": item.get("license"),
                "usage_allowed": item.get("usage_allowed"),
            }
            for item in (assets.get("assets") or [])
        ],
        "all_usage_allowed": all(item.get("usage_allowed") for item in (assets.get("assets") or [])),
    }
    overall = "pass"
    if not qa.get("passed", True):
        overall = "fail"
    elif human_review:
        overall = "needs_review"

    return {
        "version": EVALUATION_VERSION,
        "topic": topic,
        "title": script.get("title") or storyboard.get("title") or "",
        "status": overall,
        "narrative": {
            "hook": script.get("hook"),
            "hook_pattern": narrative.get("hook_pattern"),
            "archetype": narrative.get("archetype"),
            "beat_count": len(beats),
            "arc_roles": narrative.get("arc_roles") or [],
            "emotion_variety": narrative.get("emotion_variety"),
            "word_count": stats.get("total_words"),
            "estimated_duration_s": stats.get("estimated_duration_s"),
            "actual_duration_s": duration,
            "loop_line": script.get("loop_line"),
        },
        "visual": visual,
        "audio": audio,
        "editing": editing,
        "captions": {
            "style": subtitles.get("style") or config.captions_style,
            "karaoke_chunks": subtitles.get("karaoke_chunks"),
            "overlay_count": subtitles.get("overlay_count"),
            "hook_overlay": subtitles.get("hook_overlay"),
            "karaoke_source": subtitles.get("karaoke_source"),
        },
        "facts": facts,
        "licensing": licensing,
        "usage": usage,
        "qa": {
            "passed": qa.get("passed"),
            "blocking_count": qa.get("blocking_count"),
            "issues": qa.get("issues") or [],
        },
        "needs_human_review": human_review,
        "caveats": [
            "Skor relevansi/palet adalah heuristik, bukan penilaian estetika objektif.",
            "Tanpa video AI, gerakan adalah kamera 2.5D pada gambar/klip, bukan animasi 3D kompleks.",
            "Voice acting memakai prosody TTS; bukan akting manusia.",
            "Klaim 'verified' hanya bermakna bila sumber benar-benar terverifikasi di research.json.",
        ],
        "created_at": utcnow_iso(),
    }


def write_evaluation(evaluation: dict[str, Any], artifacts_dir: Path) -> tuple[Path, Path]:
    artifacts_dir = Path(artifacts_dir)
    json_path = artifacts_dir / "evaluation.json"
    md_path = artifacts_dir / "evaluation.md"
    json_path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_evaluation_markdown(evaluation), encoding="utf-8")
    return json_path, md_path


def render_evaluation_markdown(evaluation: dict[str, Any]) -> str:
    narrative = evaluation.get("narrative") or {}
    visual = evaluation.get("visual") or {}
    audio = evaluation.get("audio") or {}
    editing = evaluation.get("editing") or {}
    captions = evaluation.get("captions") or {}
    facts = evaluation.get("facts") or {}
    licensing = evaluation.get("licensing") or {}
    qa = evaluation.get("qa") or {}
    audio_source_label = "final mix" if audio.get("measurement_source") == "final_mix" else "voice-over"
    lines = [
        f"# Evaluasi: {evaluation.get('title') or evaluation.get('topic')}",
        "",
        f"Status: **{evaluation.get('status', 'unknown').upper()}**",
        "",
        "## Cerita",
        f"- Hook: {narrative.get('hook') or '-'}",
        f"- Pola hook: {narrative.get('hook_pattern') or '-'} | arketipe: {narrative.get('archetype') or '-'}",
        f"- Beat: {narrative.get('beat_count')} | variasi emosi: {narrative.get('emotion_variety')}",
        f"- Arc: {' → '.join(narrative.get('arc_roles') or []) or '-'}",
        f"- Durasi: {narrative.get('actual_duration_s')}s (estimasi {narrative.get('estimated_duration_s')}s)",
        "",
        "## Visual",
        f"- Style: {visual.get('style_name') or '-'} (`{visual.get('style_id') or '-'}`)",
        f"- Scene: {visual.get('scene_count')} | strategi: {visual.get('strategies')}",
        f"- Reference chain: {visual.get('reference_chain')}",
        f"- Relevansi rata-rata: {visual.get('relevance_average')} / 10 "
        f"(ambang {visual.get('relevance_threshold')})",
        f"- Konsistensi palet: {((visual.get('palette_consistency') or {}).get('score'))} / 100 "
        f"(heuristik, {((visual.get('palette_consistency') or {}).get('images'))} gambar)",
        "",
        "## Audio",
        f"- Suara: {audio.get('voice')} | profil: {audio.get('voice_profile')} | engine: {audio.get('engine')}",
        f"- Model: {audio.get('model')} | kontrol: {audio.get('delivery_control')}",
        f"- Alignment caption: {audio.get('caption_alignment_source')}",
        f"- Prosody didukung: {audio.get('prosody_supported')} | variasi emosi: {audio.get('emotion_variety')}",
        f"- Loudness ({audio_source_label}): "
        f"{audio.get('integrated_lufs')} LUFS | true peak: {audio.get('true_peak_dbtp')} dBTP | "
        f"peak file: {audio.get('peak_dbfs')} dBFS",
        "",
        "## Editing",
        f"- Engine: {editing.get('engine')} | shot: {editing.get('shot_count')} | "
        f"maks {editing.get('max_shot_s')}s | {editing.get('cuts_per_minute')} potongan/menit",
        f"- Variasi camera move: {editing.get('camera_move_variety')} | transisi: {len(editing.get('transitions') or [])}",
        f"- Musik: {editing.get('music_included')} | SFX: {editing.get('sfx_included')}",
        "",
        "## Caption",
        f"- Gaya: {captions.get('style')} | chunk karaoke: {captions.get('karaoke_chunks')} | "
        f"overlay fakta: {captions.get('overlay_count')}",
        f"- Sumber timing karaoke: {captions.get('karaoke_source')}",
        "",
        "## Fakta & sumber",
        f"- Klaim: verified {facts.get('verified_claims')}, theory {facts.get('theory_claims')}, "
        f"speculation {facts.get('speculation_claims')}, dramatization {facts.get('dramatization_claims')}",
        f"- Fakta dirujuk: {', '.join(facts.get('facts_used') or []) or '-'}",
    ]
    for source in facts.get("sources") or []:
        lines.append(
            f"  - {source.get('id')} ({source.get('confidence')}, {source.get('verification')}): "
            f"{source.get('claim')} — {source.get('source_url') or source.get('source_title') or 'tanpa URL'}"
        )
    lines += [
        "",
        "## Lisensi aset",
        f"- Semua aset boleh dipakai: {licensing.get('all_usage_allowed')}",
    ]
    for item in licensing.get("assets") or []:
        lines.append(
            f"  - scene {item.get('scene_index')}: {item.get('kind')} | {item.get('license')} | "
            f"{item.get('source')}"
        )
    lines += [
        "",
        "## QA",
        f"- Lulus: {qa.get('passed')} | masalah berat: {qa.get('blocking_count')}",
    ]
    for issue in qa.get("issues") or []:
        lines.append(f"  - [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
    lines += ["", "## Perlu tinjauan manusia"]
    if evaluation.get("needs_human_review"):
        for item in evaluation["needs_human_review"]:
            lines.append(f"- {item.get('code')}: {item.get('message')}")
    else:
        lines.append("- tidak ada")
    lines += ["", "## Catatan batas"]
    for caveat in evaluation.get("caveats") or []:
        lines.append(f"- {caveat}")
    return "\n".join(lines) + "\n"


def _load_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
