"""Pembungkus FFmpeg/FFprobe.

Prinsip:
- Semua perintah dijalankan sebagai argumen list (tanpa shell) -> aman dari injeksi.
- Tidak ada klaim kualitas sebelum diukur: durasi/resolusi/loudness selalu diambil
  dari ffprobe/ffmpeg, bukan diestimasi.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..errors import ShortsGenError


class FFmpegError(ShortsGenError):
    """FFmpeg/ffprobe gagal atau tidak tersedia."""


class FFmpegNotFound(FFmpegError):
    pass


# Lokasi pemasangan umum yang dicek bila FFmpeg tidak ada di PATH.
COMMON_BIN_DIRS: tuple[str, ...] = (
    r"D:\tools\ffmpeg\bin",
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",
)


def _resolve(binary: str, explicit_path: str = "") -> str:
    if explicit_path:
        path = Path(explicit_path)
        if path.name.lower().startswith(binary) and path.exists():
            return str(path)
        sibling = path.with_name(f"{binary}{path.suffix}")
        if sibling.exists():
            return str(sibling)
    found = shutil.which(binary)
    if found:
        return found
    # lokasi pemasangan umum (Windows/D:/tools dipakai mesin ini)
    for directory in COMMON_BIN_DIRS:
        for candidate in (Path(directory) / binary, Path(directory) / f"{binary}.exe"):
            if candidate.exists():
                return str(candidate)
    raise FFmpegNotFound(
        f"{binary} tidak ditemukan. Pasang FFmpeg dan/atau set FFMPEG_PATH di .env."
    )


def find_ffmpeg(explicit_path: str = "") -> Optional[str]:
    """Kembalikan path ffmpeg bila ditemukan (tanpa melempar), untuk doctor/test."""
    try:
        return _resolve("ffmpeg", explicit_path)
    except FFmpegNotFound:
        return None


@dataclass
class MediaInfo:
    path: Path
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    audio_channels: int = 0
    size_bytes: int = 0
    container: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec) and self.width > 0

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)


class FFmpeg:
    """Antarmuka tipis ke ffmpeg/ffprobe dengan helper umum untuk pipeline."""

    def __init__(self, ffmpeg_path: str = "", *, timeout_s: int = 900):
        self.ffmpeg_exe = _resolve("ffmpeg", ffmpeg_path)
        self.ffprobe_exe = _resolve("ffprobe", ffmpeg_path)
        self.timeout_s = timeout_s

    # -- eksekusi ------------------------------------------------------

    def run(self, args: Sequence[str], *, timeout_s: Optional[int] = None) -> str:
        command = [self.ffmpeg_exe, "-hide_banner", "-nostdin", "-y", *args]
        return self._exec(command, timeout_s)

    def run_probe(self, args: Sequence[str]) -> str:
        command = [self.ffprobe_exe, "-hide_banner", *args]
        return self._exec(command, None)

    @staticmethod
    def _exec(command: Sequence[str], timeout_s: Optional[int]) -> str:
        try:
            completed = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except FileNotFoundError as exc:
            raise FFmpegNotFound(f"biner tidak ditemukan: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(f"perintah FFmpeg melewati batas waktu: {' '.join(command[:6])}...") from exc
        if completed.returncode != 0:
            tail = (completed.stderr or "").strip().splitlines()[-6:]
            raise FFmpegError(
                f"FFmpeg gagal (kode {completed.returncode}): " + " | ".join(tail)
            )
        return completed.stdout or ""

    # -- probe ---------------------------------------------------------

    def probe(self, path: Path | str) -> MediaInfo:
        path = Path(path)
        if not path.exists():
            raise FFmpegError(f"file tidak ditemukan: {path}")
        output = self.run_probe(
            [
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ]
        )
        data = json.loads(output or "{}")
        streams = data.get("streams") or []
        fmt = data.get("format") or {}
        info = MediaInfo(path=path, raw=data)
        info.duration_s = float(fmt.get("duration") or 0.0)
        info.size_bytes = int(fmt.get("size") or path.stat().st_size)
        info.container = str(fmt.get("format_name") or "")
        for stream in streams:
            if stream.get("codec_type") == "video" and not info.video_codec:
                info.video_codec = str(stream.get("codec_name") or "")
                info.width = int(stream.get("width") or 0)
                info.height = int(stream.get("height") or 0)
                info.fps = _parse_fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
                if info.duration_s <= 0:
                    info.duration_s = float(stream.get("duration") or 0.0)
            elif stream.get("codec_type") == "audio" and not info.audio_codec:
                info.audio_codec = str(stream.get("codec_name") or "")
                info.audio_channels = int(stream.get("channels") or 0)
        return info

    def duration(self, path: Path | str) -> float:
        return self.probe(path).duration_s

    # -- operasi umum --------------------------------------------------

    def make_silence(self, out_path: Path, seconds: float, *, sample_rate: int = 48000) -> Path:
        self.run(
            [
                "-f", "lavfi",
                "-i", f"anullsrc=channel_layout=mono:sample_rate={sample_rate}",
                "-t", f"{seconds:.3f}",
                "-c:a", "pcm_s16le",
                str(out_path),
            ]
        )
        return out_path

    def concat_audio(self, files: Sequence[Path], out_path: Path, *, sample_rate: int = 48000) -> Path:
        """Gabungkan audio (semua file harus sudah seragam) memakai concat demuxer."""
        if not files:
            raise FFmpegError("tidak ada file audio untuk digabung")
        list_file = out_path.with_suffix(".txt")
        list_file.write_text(
            "\n".join(f"file '{Path(f).as_posix()}'" for f in files), encoding="utf-8"
        )
        self.run(
            [
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-ar", str(sample_rate), "-ac", "1",
                "-c:a", "pcm_s16le",
                str(out_path),
            ]
        )
        list_file.unlink(missing_ok=True)
        return out_path

    def pad_audio_to(self, src: Path, out_path: Path, seconds: float, *, sample_rate: int = 48000) -> Path:
        """Perpanjang audio dengan keheningan sampai durasi target (untuk sinkronisasi)."""
        self.run(
            [
                "-i", str(src),
                "-af", f"apad=pad_dur={max(0.0, seconds):.3f}",
                "-t", f"{seconds:.3f}",
                "-ar", str(sample_rate), "-ac", "1",
                "-c:a", "pcm_s16le",
                str(out_path),
            ]
        )
        return out_path

    def normalize_loudness(
        self,
        src: Path,
        out_path: Path,
        *,
        target_lufs: float = -16.0,
        true_peak: float = -1.5,
        sample_rate: int = 48000,
    ) -> Path:
        """Normalisasi kenyaringan satu lintasan (loudnorm)."""
        self.run(
            [
                "-i", str(src),
                "-af", f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
                "-ar", str(sample_rate), "-ac", "1",
                "-c:a", "pcm_s16le",
                str(out_path),
            ]
        )
        return out_path

    def measure_loudness(self, path: Path) -> dict[str, float]:
        """Ukur loudness & true peak via loudnorm print_format=json (JSON keluar di stderr)."""
        completed = subprocess.run(
            [
                self.ffmpeg_exe, "-hide_banner", "-nostdin",
                "-i", str(path),
                "-af", "loudnorm=print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", completed.stderr or "", re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        result: dict[str, float] = {}
        for key in ("input_i", "input_tp", "input_lra", "input_thresh", "output_i"):
            if key in data:
                try:
                    result[key] = float(data[key])
                except (TypeError, ValueError):
                    continue
        return result

    def measure_peak(self, path: Path) -> Optional[float]:
        """Volume maksimum (dBFS) via volumedetect; None bila tidak terukur."""
        completed = subprocess.run(
            [
                self.ffmpeg_exe, "-hide_banner", "-nostdin",
                "-i", str(path), "-af", "volumedetect",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", completed.stderr or "")
        return float(match.group(1)) if match else None

    def decode_check(self, path: Path) -> None:
        """Pastikan file bisa didekode penuh (deteksi file korup)."""
        self.run(["-v", "error", "-i", str(path), "-f", "null", "-"], timeout_s=600)

    def extract_frame(self, src: Path, out_path: Path, *, at_s: float = 1.0) -> Path:
        self.run(["-ss", f"{max(0.0, at_s):.2f}", "-i", str(src), "-frames:v", "1", str(out_path)])
        return out_path


def _parse_fraction(value: Optional[str]) -> float:
    if not value:
        return 0.0
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            den = float(denominator)
            return float(numerator) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0
