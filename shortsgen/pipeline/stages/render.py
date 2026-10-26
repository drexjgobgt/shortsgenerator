"""Stage RENDER: susun klip per shot, gabung, mux audio (voice+musik+SFX), bakar caption.

Peningkatan retensi yang diterapkan:
- **Pemecahan shot**: scene lebih panjang dari MAX_SCENE_SECONDS dipecah menjadi
  beberapa shot -> pergantian visual setiap ≤4 detik (standar Shorts).
- **Zoom bergantian** (masuk/keluar) agar setiap shot terasa berbeda.
- **Caption karaoke** + overlay hook (dibuat stage subtitles, dibakar di sini).
- **Musik latar** (prosedural bebas lisensi / folder pengguna) dengan *ducking*
  otomatis saat suara narator aktif, plus SFX whoosh pada pergantian scene.

Preset: `fast` (720x1280 ultrafast) dan `final` (1080x1920 veryfast).
Semua parameter diverifikasi ulang dengan ffprobe setelah render.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...errors import ShortsGenError
from ...media.cinematic import (
    IMAGE_SUFFIXES as CINEMATIC_IMAGE_SUFFIXES,
    TRANSITION_SECONDS,
    build_xfade_filter,
    camera_filter,
    grade_filter,
    plan_render_shots,
    sfx_cues_for_shots,
    transition_offsets,
)
from ...media.ffmpeg import FFmpeg, FFmpegError
from ...media.music import MusicTrack, build_music_for_job, generate_sfx_track
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ..orchestrator import StageContext

PRESETS = {
    "fast": {"max_width": 720, "max_height": 1280, "crf": 28, "preset": "ultrafast"},
    "final": {"max_width": None, "max_height": None, "crf": 20, "preset": "veryfast"},
}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def effective_size(config: Config) -> tuple[int, int]:
    """Ukuran render efektif (selalu genap agar aman untuk yuv420p)."""
    preset = PRESETS.get(config.render_preset, PRESETS["fast"])
    width, height = config.render_width, config.render_height
    if preset["max_width"]:
        width = min(width, int(preset["max_width"]))
    if preset["max_height"]:
        height = min(height, int(preset["max_height"]))
    return width - (width % 2), height - (height % 2)


class RenderStage:
    stage = "render"

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
        preset = PRESETS.get(self.config.render_preset, PRESETS["fast"])
        width, height = effective_size(self.config)
        fps = self.config.render_fps

        storyboard = _load_json(ctx, "storyboard.json", ["tts", "assets", "scenes"])
        timings = _load_json(ctx, "timings.json", ["tts"])
        scenes = storyboard.get("scenes") or []
        if not scenes:
            raise RuntimeError("storyboard.json tidak memuat scene")
        voiceover = Path(str(timings.get("voiceover_path") or ""))
        if not voiceover.exists():
            raise RuntimeError(f"voiceover tidak ditemukan: {voiceover}")

        work_dir = ctx.artifacts_dir.parent / "render"
        work_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()

        # 1) shot plan: dari storyboard (cinematic) atau pemecahan lama (legacy)
        engine = (self.config.render_engine or "cinematic").lower()
        if engine == "cinematic":
            shots = plan_render_shots(
                scenes,
                max_shot_s=float(self.config.max_scene_seconds),
                transitions_enabled=self.config.transitions_enabled,
            )
        else:
            shots = _plan_shots(scenes, max_shot_s=float(self.config.max_scene_seconds))
        if not shots:
            raise ShortsGenError(
                "tidak ada aset visual yang valid untuk dirender (jalankan stage assets lebih dulu)"
            )

        transitions_active = (
            engine == "cinematic"
            and self.config.transitions_enabled
            and len(shots) > 1
        )
        clips: list[Path] = []
        for position, shot in enumerate(shots):
            asset = Path(str(shot["asset"]))
            if not asset.exists():
                raise ShortsGenError(f"aset shot {shot['index']} tidak ditemukan: {asset}")
            clip = work_dir / f"shot_{shot['index']:03d}.mp4"
            if engine == "cinematic":
                render_duration = float(shot["duration_s"])
                if transitions_active:
                    # semua klip diberi ekor t; xfade memotong overlap sehingga
                    # total video tetap sama dengan total audio.
                    render_duration += TRANSITION_SECONDS
                self._render_shot_clip_cinematic(
                    asset,
                    clip,
                    duration=render_duration,
                    width=width,
                    height=height,
                    fps=fps,
                    preset=preset["preset"],
                    crf=preset["crf"],
                    camera_move=str(shot.get("camera_move") or "push_in"),
                    intensity=self.config.motion_intensity,
                )
            else:
                self._render_shot_clip(
                    asset,
                    clip,
                    duration=float(shot["duration_s"]),
                    width=width,
                    height=height,
                    fps=fps,
                    preset=preset["preset"],
                    crf=preset["crf"],
                    zoom_in=bool(shot.get("zoom_in", position % 2 == 0)),
                )
            clips.append(clip)

        if transitions_active:
            try:
                video_track = self._concat_with_xfade(
                    clips,
                    shots,
                    work_dir / "video_track.mp4",
                    preset=preset["preset"],
                    crf=preset["crf"],
                    fps=fps,
                )
            except FFmpegError as exc:
                if self.logger:
                    self.logger.warning(f"xfade gagal, fallback concat: {exc}")
                video_track = self._concat_clips(clips, work_dir / "video_track.mp4")
        else:
            video_track = self._concat_clips(clips, work_dir / "video_track.mp4")

        # 2) audio: musik latar + SFX (naratif bila engine cinematic)
        total_audio = float(timings.get("audio_duration_s") or timings.get("total_seconds") or 0)
        music: Optional[MusicTrack] = None
        sfx_path: Optional[Path] = None
        try:
            music = build_music_for_job(
                work_dir,
                duration_s=total_audio,
                mode=self.config.music_mode,
                music_dir=self.config.music_dir,
                bpm=self.config.music_bpm,
            )
            if self.config.sfx_enabled and len(shots) > 1:
                if engine == "cinematic" and self.config.sfx_mode == "narrative":
                    cues = sfx_cues_for_shots(shots)
                    sfx_path = generate_sfx_track(
                        work_dir / "sfx.wav",
                        duration_s=total_audio,
                        whoosh_offsets=transition_offsets(shots),
                        cues=cues,
                    )
                else:
                    offsets = [float(shot["start_s"]) for shot in shots[1:]]
                    sfx_path = generate_sfx_track(
                        work_dir / "sfx.wav", duration_s=total_audio, whoosh_offsets=offsets
                    )
        except Exception as exc:  # musik/SFX tidak boleh menggagalkan render video
            if self.logger:
                self.logger.warning(f"musik/SFX dilewati: {exc}")

        # 3) mux final + burn caption
        ass_path = _find_ass(ctx)
        final_path = ctx.artifacts_dir.parent / "final.mp4"
        self._mux(
            video_track=video_track,
            voiceover=voiceover,
            ass_path=ass_path,
            out_path=final_path,
            preset=preset["preset"],
            crf=preset["crf"],
            fps=fps,
            music=music,
            music_volume_db=float(self.config.music_volume_db),
            sfx_path=sfx_path,
            music_sections=bool(self.config.music_sections),
            total_seconds=total_audio,
        )

        info = self.ffmpeg.probe(final_path)
        if not info.has_video or not info.has_audio:
            raise ShortsGenError("hasil render tidak memuat video+audio yang valid")

        max_shot = max((s["duration_s"] for s in shots), default=0.0)
        render_report = {
            "output": str(final_path),
            "preset": self.config.render_preset,
            "engine": engine,
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 3),
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "duration_s": round(info.duration_s, 3),
            "size_bytes": info.size_bytes,
            "subtitles_burned": bool(ass_path),
            "captions_style": self.config.captions_style,
            "shots": shots,
            "shot_count": len(shots),
            "max_shot_s": round(max_shot, 3),
            "transitions": (
                [shot.get("transition_in") for shot in shots[1:]]
                if transitions_active
                else []
            ),
            "camera_moves": [shot.get("camera_move") for shot in shots],
            "motion_intensity": self.config.motion_intensity,
            "music": music.to_dict() if music else None,
            "music_volume_db": self.config.music_volume_db,
            "music_sections": bool(self.config.music_sections),
            "sfx_included": bool(sfx_path),
            "sfx_mode": self.config.sfx_mode,
            "render_seconds": round(time.perf_counter() - started, 2),
            "created_at": utcnow_iso(),
        }
        report_path = ctx.artifact_path("render.json")
        report_path.write_text(json.dumps(render_report, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.repo.add_event(
            ctx.job.id,
            "info",
            f"render selesai: {final_path.name} ({info.width}x{info.height}, {info.duration_s:.1f}s, "
            f"{len(shots)} shot, maks {max_shot:.1f}s/shot, musik={'ya' if music else 'tidak'}, "
            f"preset {self.config.render_preset})",
        )

        return {
            "render": str(report_path),
            "video": str(final_path),
            "duration_s": round(info.duration_s, 3),
            "size_bytes": info.size_bytes,
            "resolution": f"{info.width}x{info.height}",
            "shot_count": len(shots),
            "max_shot_s": round(max_shot, 3),
            "music_included": bool(music),
        }

    # -- internal -----------------------------------------------------

    def _render_shot_clip(
        self,
        asset: Path,
        out_path: Path,
        *,
        duration: float,
        width: int,
        height: int,
        fps: int,
        preset: str,
        crf: int,
        zoom_in: bool,
    ) -> None:
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
        if asset.suffix.lower() in IMAGE_SUFFIXES:
            frames = max(1, int(duration * fps))
            if zoom_in:
                zoom_expr = "min(1+0.0012*on,1.12)"
            else:
                zoom_expr = "max(1.12-0.0012*on,1.0)"
            zoom = (
                f"zoompan=z='{zoom_expr}':d={frames}:s={width}x{height}:fps={fps}"
            )
            filter_chain = f"{fit},{zoom},format=yuv420p"
            args = [
                "-loop", "1", "-i", str(asset),
                "-t", f"{duration:.3f}",
                "-vf", filter_chain,
                "-r", str(fps),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-an",
                str(out_path),
            ]
        else:
            filter_chain = f"{fit},fps={fps},format=yuv420p"
            args = [
                "-stream_loop", "-1", "-i", str(asset),
                "-t", f"{duration:.3f}",
                "-vf", filter_chain,
                "-r", str(fps),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-an",
                str(out_path),
            ]
        try:
            self.ffmpeg.run(args)
        except FFmpegError as exc:
            raise ShortsGenError(f"render shot gagal untuk {asset.name}: {exc}") from exc

    def _render_shot_clip_cinematic(
        self,
        asset: Path,
        out_path: Path,
        *,
        duration: float,
        width: int,
        height: int,
        fps: int,
        preset: str,
        crf: int,
        camera_move: str,
        intensity: str,
    ) -> None:
        """Render satu shot dengan camera move + grade + grain."""
        is_image = asset.suffix.lower() in CINEMATIC_IMAGE_SUFFIXES
        frames = max(2, int(round(duration * fps)))
        camera = camera_filter(
            camera_move,
            width=width,
            height=height,
            fps=fps,
            frames=frames,
            intensity=intensity,
            is_image=is_image,
        )
        filters = f"{camera},{grade_filter(intensity)}"
        args = (
            ["-loop", "1", "-i", str(asset)]
            if is_image
            else ["-stream_loop", "-1", "-i", str(asset)]
        )
        args += [
            "-t", f"{duration:.3f}",
            "-vf", filters,
            "-r", str(fps),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-an",
            str(out_path),
        ]
        try:
            self.ffmpeg.run(args)
        except FFmpegError as exc:
            raise ShortsGenError(f"render sinematik gagal untuk {asset.name}: {exc}") from exc

    def _concat_with_xfade(
        self,
        clips: list[Path],
        shots: list[dict[str, Any]],
        out_path: Path,
        *,
        preset: str,
        crf: int,
        fps: int,
    ) -> Path:
        """Gabung klip dengan transisi xfade terencana (fallback ke concat di pemanggil)."""
        filter_complex, label = build_xfade_filter(shots)
        if not filter_complex:
            return self._concat_clips(clips, out_path)
        args: list[str] = []
        for clip in clips:
            args += ["-i", str(clip)]
        args += [
            "-filter_complex", filter_complex,
            "-map", label,
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps), "-an",
            str(out_path),
        ]
        self.ffmpeg.run(args)
        return out_path

    def _concat_clips(self, clips: list[Path], out_path: Path) -> Path:
        list_file = out_path.with_suffix(".txt")
        list_file.write_text(
            "\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8"
        )
        try:
            self.ffmpeg.run(
                ["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)]
            )
        finally:
            list_file.unlink(missing_ok=True)
        return out_path

    def _mux(
        self,
        *,
        video_track: Path,
        voiceover: Path,
        ass_path: Optional[Path],
        out_path: Path,
        preset: str,
        crf: int,
        fps: int,
        music: Optional[MusicTrack],
        music_volume_db: float,
        sfx_path: Optional[Path],
        music_sections: bool = False,
        total_seconds: float = 0.0,
    ) -> None:
        base_args = ["-i", str(video_track), "-i", str(voiceover)]
        next_index = 2
        music_index = None
        sfx_index = None
        if music is not None:
            base_args += ["-i", str(music.path)]
            music_index = next_index
            next_index += 1
        if sfx_path is not None:
            base_args += ["-i", str(sfx_path)]
            sfx_index = next_index
            next_index += 1

        filter_complex, audio_map = _audio_graph(
            music_index=music_index,
            sfx_index=sfx_index,
            music_volume_db=music_volume_db,
            music_sections=music_sections,
            total_seconds=total_seconds,
        )

        video_filter: list[str] = []
        if ass_path is not None:
            escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
            video_filter = ["-vf", f"ass='{escaped}'"]

        args = list(base_args)
        if filter_complex:
            args += ["-filter_complex", filter_complex]
        args += video_filter
        args += ["-map", "0:v:0", "-map", audio_map]
        args += [
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest", "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            self.ffmpeg.run(args)
        except FFmpegError as exc:
            if music is None and sfx_path is None:
                raise ShortsGenError(f"mux final gagal: {exc}") from exc
            # fallback: campur sederhana tanpa ducking
            if self.logger:
                self.logger.warning(f"mux dengan filter kompleks gagal, memakai campuran sederhana: {exc}")
            fallback_args = list(base_args)
            simple_filters: list[str] = []
            labels = ["[1:a]"]
            if music_index is not None:
                simple_filters.append(f"[{music_index}:a]volume={music_volume_db}dB[m]")
                labels.append("[m]")
            if sfx_index is not None:
                simple_filters.append(f"[{sfx_index}:a]volume=-14dB[s]")
                labels.append("[s]")
            simple_filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
            )
            fallback_args += ["-filter_complex", ";".join(simple_filters)]
            fallback_args += video_filter
            fallback_args += ["-map", "0:v:0", "-map", "[aout]"]
            fallback_args += [
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-shortest", "-movflags", "+faststart",
                str(out_path),
            ]
            try:
                self.ffmpeg.run(fallback_args)
            except FFmpegError as exc2:
                raise ShortsGenError(f"mux final gagal (termasuk fallback): {exc2}") from exc2


def _plan_shots(scenes: list[dict[str, Any]], *, max_shot_s: float) -> list[dict[str, Any]]:
    """Pecah tiap scene menjadi shot <= max_shot_s dengan zoom bergantian."""
    shots: list[dict[str, Any]] = []
    index = 0
    for scene in scenes:
        asset = (scene.get("asset") or {}).get("path")
        start = float(scene.get("start_s") or 0.0)
        duration = float(scene.get("duration_s") or 0.0)
        if not asset or duration <= 0:
            continue
        parts = max(1, int(math.ceil(duration / max_shot_s - 1e-6)))
        part_duration = duration / parts
        cursor = start
        for part in range(parts):
            shots.append(
                {
                    "index": index,
                    "scene_index": int(scene.get("index", index)),
                    "start_s": round(cursor, 3),
                    "duration_s": round(part_duration, 3),
                    "asset": asset,
                    "zoom_in": (index % 2 == 0),
                }
            )
            index += 1
            cursor += part_duration
    return shots


def _audio_graph(
    *,
    music_index: Optional[int],
    sfx_index: Optional[int],
    music_volume_db: float,
    music_sections: bool = False,
    total_seconds: float = 0.0,
) -> tuple[str, str]:
    """Bangun filter_complex audio: musik + ducking sidechain + SFX.

    Returns:
        (filter_complex, label peta audio) - filter kosong berarti pakai voice apa adanya.
    """
    if music_index is None and sfx_index is None:
        return "", "1:a:0"

    parts: list[str] = []
    voice_label = "[1:a]"
    mix_inputs: list[str] = [voice_label]

    if music_index is not None:
        music_chain = f"[{music_index}:a]volume={music_volume_db}dB"
        if music_sections and total_seconds > 6:
            intro = 3.0
            build = max(intro + 1.0, total_seconds * 0.60)
            climax = max(build + 1.0, total_seconds * 0.85)
            expression = (
                f"if(lt(t,{intro:.2f}),0.62,"
                f"if(lt(t,{build:.2f}),0.82,"
                f"if(lt(t,{climax:.2f}),1.0,0.72)))"
            )
            music_chain += f",volume='{expression}':eval=frame"
        music_chain += "[duck_src]"
        parts.append(music_chain)
        # ducking: musik turun saat narator bicara
        parts.append(
            "[duck_src][1:a]sidechaincompress=threshold=0.03:ratio=6:attack=5:release=300[ducked]"
        )
        mix_inputs.append("[ducked]")
    if sfx_index is not None:
        parts.append(f"[{sfx_index}:a]volume=-12dB[sfx]")
        mix_inputs.append("[sfx]")

    weights = " ".join("1" if label == voice_label else ("1" if label == "[ducked]" else "0.7") for label in mix_inputs)
    parts.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:"
        f"dropout_transition=0:normalize=0:weights={weights}[aout]"
    )
    return ";".join(parts), "[aout]"


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


def _find_ass(ctx: StageContext) -> Optional[Path]:
    record = ctx.repo.get_stage(ctx.job.id, "subtitles")
    if record and record.artifacts.get("ass"):
        path = Path(record.artifacts["ass"])
        if path.exists():
            return path
    fallback = ctx.artifacts_dir / "subtitle.ass"
    return fallback if fallback.exists() else None
