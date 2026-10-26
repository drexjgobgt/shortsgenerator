"""Stage QA: pemeriksaan kualitas hasil render (berbasis pengukuran, bukan asumsi).

Pemeriksaan:
- integritas file (kodek video+audio, bisa didekode penuh)
- resolusi, fps, rasio vertikal
- durasi vs total voiceover & target naskah
- loudness (LUFS) dan clipping (peak dBFS)
- konsistensi subtitle (cue monoton, di dalam durasi media)

Temuan berat -> stage GAGAL (tidak boleh diklaim lolos tanpa bukti).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...media.ffmpeg import FFmpeg
from ...media.subtitles import Cue, validate_cues
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ...qa.evaluation import evaluate_job
from ..orchestrator import StageContext
from .render import effective_size

LOUDNESS_MIN_LUFS = -24.0
LOUDNESS_MAX_LUFS = -10.0
CLIP_PEAK_DBFS = -0.1
DURATION_TOLERANCE_S = 1.0


class QAStage:
    stage = "qa"

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
        render = _load_json(ctx, "render.json", ["render"])
        timings = _load_json(ctx, "timings.json", ["tts"])
        video_path = Path(str(render.get("output") or ""))
        if not video_path.exists():
            raise RuntimeError(f"file video hasil render tidak ditemukan: {video_path}")

        issues: list[dict[str, str]] = []
        measurements: dict[str, Any] = {}

        def add(severity: str, code: str, message: str) -> None:
            issues.append({"severity": severity, "code": code, "message": message})

        info = self.ffmpeg.probe(video_path)
        measurements["video"] = {
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 3),
            "duration_s": round(info.duration_s, 3),
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "size_bytes": info.size_bytes,
        }

        expected_width, expected_height = effective_size(self.config)
        if (info.width, info.height) != (expected_width, expected_height):
            add("high", "resolution", f"resolusi {info.width}x{info.height} != {expected_width}x{expected_height}")
        if info.height <= info.width:
            add("high", "orientation", "video bukan format vertikal")
        if info.fps < 24:
            add("high", "fps", f"fps {info.fps:.2f} < 24")
        if not info.has_audio:
            add("high", "no_audio", "tidak ada audio pada hasil render")
        if info.video_codec not in ("h264", "hevc"):
            add("medium", "video_codec", f"kodek video {info.video_codec} (disarankan h264)")
        if info.audio_codec not in ("aac", "mp3"):
            add("medium", "audio_codec", f"kodek audio {info.audio_codec} (disarankan aac)")

        # durasi vs voiceover & target naskah
        audio_expected = float(timings.get("audio_duration_s") or timings.get("total_seconds") or 0)
        if audio_expected and abs(info.duration_s - audio_expected) > DURATION_TOLERANCE_S:
            add(
                "high",
                "duration_mismatch",
                f"durasi video {info.duration_s:.2f}s menyimpang {abs(info.duration_s - audio_expected):.2f}s "
                f"dari voiceover {audio_expected:.2f}s",
            )
        target = float(self.config.target_duration_s)
        if not (target * 0.55 <= info.duration_s <= target * 1.5):
            add(
                "medium",
                "duration_vs_target",
                f"durasi {info.duration_s:.1f}s jauh dari target {target:.0f}s",
            )

        # loudness & clipping pada hasil akhir
        loudness = self.ffmpeg.measure_loudness(video_path)
        peak = self.ffmpeg.measure_peak(video_path)
        measurements["audio"] = {"loudness": loudness, "peak_dbfs": peak}
        integrity = loudness.get("input_i")
        if integrity is not None and not (LOUDNESS_MIN_LUFS <= integrity <= LOUDNESS_MAX_LUFS):
            add("medium", "loudness", f"loudness {integrity:.1f} LUFS di luar rentang {LOUDNESS_MIN_LUFS}..{LOUDNESS_MAX_LUFS}")
        if peak is not None and peak >= CLIP_PEAK_DBFS:
            add("high", "clipping", f"peak {peak:.2f} dBFS mengindikasikan clipping")

        # integritas dekode
        try:
            self.ffmpeg.decode_check(video_path)
        except Exception as exc:
            add("high", "decode", f"video tidak bisa didekode penuh: {exc}")

        # subtitle
        srt_path = _find_file(ctx, "subtitles", "srt", "subtitle.srt")
        if srt_path is None:
            add("medium", "subtitles_missing", "file subtitle SRT tidak ditemukan")
        else:
            cues = _parse_srt(srt_path)
            subtitle_issues = validate_cues(cues, media_duration_s=info.duration_s)
            measurements["subtitles"] = {"cue_count": len(cues), "issues": subtitle_issues}
            for issue in subtitle_issues:
                add("high", "subtitle_timing", issue)

        # 6. retensi & editing (Fase E)
        shots = render.get("shots") or []
        max_shot = float(render.get("max_shot_s") or 0.0)
        allowed_shot = float(self.config.max_scene_seconds) + 0.35
        if shots and max_shot > allowed_shot:
            add(
                "medium",
                "shots_too_long",
                f"shot terpanjang {max_shot:.2f}s > batas {allowed_shot:.2f}s "
                "(pergantian visual disarankan tiap 2-4 dtk)",
            )
        if shots:
            cut_rate = len(shots) / max(info.duration_s, 1e-6) * 60
            measurements["editing"] = {
                "shot_count": len(shots),
                "max_shot_s": round(max_shot, 3),
                "cuts_per_minute": round(cut_rate, 1),
            }
            if cut_rate < 6:
                add("low", "few_cuts", f"hanya {cut_rate:.1f} potongan/menit (video terasa lambat)")

        if self.config.music_mode != "off" and not render.get("music"):
            add(
                "medium",
                "music_missing",
                f"MUSIC_MODE={self.config.music_mode} tetapi musik tidak ikut terpasang",
            )

        subtitles_report = _load_optional_json(ctx, "subtitles.json", ["subtitles"])
        if subtitles_report:
            if self.config.captions_style == "dynamic":
                chunks = int(subtitles_report.get("karaoke_chunks") or 0)
                rate = chunks / max(info.duration_s, 1e-6)
                measurements["captions"] = {"karaoke_chunks": chunks, "chunks_per_second": round(rate, 2)}
                if rate < 0.25:
                    add("low", "caption_pace_slow", f"caption terlalu jarang ({rate:.2f} potongan/dtk)")
                elif rate > 1.4:
                    add("low", "caption_pace_fast", f"caption terlalu rapat ({rate:.2f} potongan/dtk)")
            if self.config.hook_overlay and not subtitles_report.get("hook_overlay"):
                add("low", "hook_overlay_missing", "overlay hook tidak terpasang pada caption")

        blocking = [i for i in issues if i["severity"] == "high"]
        report = {
            "passed": not blocking,
            "issues": issues,
            "blocking_count": len(blocking),
            "measurements": measurements,
            "created_at": utcnow_iso(),
        }
        report_path = ctx.artifact_path("qa.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("qa.md")).write_text(_render_markdown(report), encoding="utf-8")

        evaluation_paths: tuple[Path, Path] | None = None
        if self.config.evaluation_report:
            try:
                evaluation = evaluate_job(ctx, self.config, qa_report=report)
                evaluation_paths = (
                    ctx.artifact_path("evaluation.json"),
                    ctx.artifact_path("evaluation.md"),
                )
                measurements["evaluation_status"] = evaluation.get("status")
            except Exception as exc:  # laporan tidak boleh menyembunyikan kegagalan QA
                if self.logger:
                    self.logger.warning(f"laporan evaluasi gagal dibuat: {exc}")

        if blocking:
            summary = "; ".join(f"{i['code']}: {i['message']}" for i in blocking[:3])
            raise RuntimeError(f"QA menemukan masalah berat: {summary}")

        result = {
            "qa": str(report_path),
            "passed": True,
            "issues": len(issues),
            "duration_s": round(info.duration_s, 3),
        }
        if evaluation_paths:
            result["evaluation"] = str(evaluation_paths[0])
            result["evaluation_md"] = str(evaluation_paths[1])
        return result


def _parse_srt(path: Path) -> list[Cue]:
    """Parser SRT sederhana untuk validasi ulang timing."""
    text = path.read_text(encoding="utf-8")
    cues: list[Cue] = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0].strip())
            start_raw, _, end_raw = lines[1].partition("-->")
            cues.append(
                Cue(
                    index=index,
                    start_s=_srt_to_seconds(start_raw.strip()),
                    end_s=_srt_to_seconds(end_raw.strip()),
                    text=" ".join(lines[2:]),
                )
            )
        except (ValueError, IndexError):
            continue
    return cues


def _srt_to_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, _, millis = rest.partition(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis or 0) / 1000


def _find_file(ctx: StageContext, stage: str, key: str, fallback_name: str) -> Optional[Path]:
    record = ctx.repo.get_stage(ctx.job.id, stage)
    if record and record.artifacts.get(key):
        path = Path(record.artifacts[key])
        if path.exists():
            return path
    fallback = ctx.artifacts_dir / fallback_name
    return fallback if fallback.exists() else None


def _load_optional_json(ctx: StageContext, filename: str, stages: list[str]) -> dict[str, Any]:
    """Muat JSON artefak bila ada; kembalikan {} bila tidak (tidak melempar)."""
    try:
        return _load_json(ctx, filename, stages)
    except RuntimeError:
        return {}


def _load_json(ctx: StageContext, filename: str, stages: list[str]) -> dict[str, Any]:
    for stage in stages:
        record = ctx.repo.get_stage(ctx.job.id, stage)
        if not record:
            continue
        for value in (record.artifacts or {}).values():
            path = Path(str(value))
            if path.name == filename and path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    fallback = ctx.artifacts_dir / filename
    if fallback.exists():
        return json.loads(fallback.read_text(encoding="utf-8"))
    raise RuntimeError(f"artefak '{filename}' tidak ditemukan")


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Pemeriksaan kualitas (QA)",
        "",
        f"Status: **{'LULUS' if report['passed'] else 'GAGAL'}** "
        f"({report['blocking_count']} masalah berat, {len(report['issues'])} temuan)",
        "",
    ]
    video = (report.get("measurements") or {}).get("video") or {}
    if video:
        lines += [
            "## Pengukuran video",
            f"- {video['width']}x{video['height']} @ {video['fps']} fps, {video['duration_s']} dtk",
            f"- kodek: {video['video_codec']} / {video['audio_codec']}, "
            f"{video['size_bytes']/1024/1024:.1f} MB",
            "",
        ]
    audio = (report.get("measurements") or {}).get("audio") or {}
    if audio:
        lines += [
            "## Pengukuran audio",
            f"- loudness: {audio.get('loudness', {})}",
            f"- peak: {audio.get('peak_dbfs')} dBFS",
            "",
        ]
    lines.append("## Temuan")
    if not report["issues"]:
        lines.append("- (tidak ada temuan)")
    for issue in report["issues"]:
        lines.append(f"- [{issue['severity']}] {issue['code']}: {issue['message']}")
    return "\n".join(lines) + "\n"
