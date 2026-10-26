"""Musik latar & efek suara prosedural (bebas lisensi, dibuat lokal).

Alasan: Shorts beretensi tinggi memakai musik latar halus yang mengikuti tempo.
Alih-alih mengunduh musik (masalah lisensi), kita SINTESIS sendiri:
- kick, hi-hat, snare, bass, dan pad sederhana pada progresi Am-F-C-G
- efek "whoosh" untuk transisi dan "pop" untuk teks

`MUSIC_MODE=folder` juga didukung: pengguna menaruh file musik berlisensi di
`MUSIC_DIR` bersama `licenses.json` (format sama seperti stok video).

Semua keluaran: WAV mono 48 kHz agar konsisten dengan voice-over.
"""
from __future__ import annotations

import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SAMPLE_RATE = 48000
LICENSE_PROCEDURAL = "dibuat lokal oleh shortsgen (bebas lisensi)"

# Progresi Am - F - C - G (frekuensi nada dasar)
CHORDS: tuple[tuple[float, tuple[float, ...]], ...] = (
    (110.00, (220.00, 261.63, 329.63)),   # A2  | Am
    (87.31, (174.61, 220.00, 261.63)),    # F2  | F
    (130.81, (196.00, 261.63, 329.63)),   # C3  | C
    (98.00, (196.00, 246.94, 293.66)),    # G2  | G
)


@dataclass
class MusicTrack:
    path: Path
    duration_s: float
    bpm: int
    license: str
    attribution: str = ""
    source: str = "procedural"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration_s": round(self.duration_s, 3),
            "bpm": self.bpm,
            "license": self.license,
            "attribution": self.attribution,
            "source": self.source,
        }


def _require_numpy():
    try:
        import numpy as np

        return np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "numpy dibutuhkan untuk musik prosedural. Pasang: pip install numpy "
            "(atau set MUSIC_MODE=off)."
        ) from exc


def _envelope(n: int, attack: int, decay: int) -> "Any":
    np = _require_numpy()
    env = np.ones(n)
    if attack > 0:
        env[:attack] = np.linspace(0, 1, attack)
    if decay > 0 and decay < n:
        env[-decay:] = np.linspace(1, 0, decay)
    return env


def _write_wav(path: Path, samples: "Any") -> Path:
    np = _require_numpy()
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def generate_music_bed(
    out_path: Path,
    duration_s: float,
    *,
    bpm: int = 120,
    seed: int = 7,
    fade_out_s: float = 1.5,
) -> MusicTrack:
    """Sintesis musik latar energik yang loop-friendly (progresi 4 bar)."""
    np = _require_numpy()
    rng = np.random.default_rng(seed)

    beat = 60.0 / bpm
    bar = beat * 4
    loop_bars = 4
    loop_s = bar * loop_bars
    total = max(loop_s, float(duration_s) + 0.5)
    n_total = int(total * SAMPLE_RATE)
    buffer = np.zeros(n_total)

    def add(signal: "Any", at_s: float, gain: float = 1.0) -> None:
        start = int(at_s * SAMPLE_RATE)
        end = min(n_total, start + len(signal))
        if start < n_total and end > start:
            buffer[start:end] += signal[: end - start] * gain

    def kick() -> "Any":
        length = int(0.28 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        freq = 120 * np.exp(-t * 26) + 48
        body = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE)
        return body * np.exp(-t * 11)

    def hat(accent: float = 1.0) -> "Any":
        length = int(0.06 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        noise = rng.uniform(-1, 1, length)
        noise = np.diff(noise, prepend=0)          # highpass kasar
        return noise * np.exp(-t * 90) * accent

    def snare() -> "Any":
        length = int(0.18 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        noise = rng.uniform(-1, 1, length) * np.exp(-t * 26)
        tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t * 30)
        return 0.7 * noise + 0.3 * tone

    def bass(freq: float) -> "Any":
        length = int(beat * 0.95 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        wave_shape = 0.7 * np.sin(2 * np.pi * freq * t) + 0.3 * np.sign(np.sin(2 * np.pi * freq * t))
        return wave_shape * _envelope(length, int(0.01 * SAMPLE_RATE), int(0.08 * SAMPLE_RATE)) * 0.9

    def pad(tones: tuple[float, ...]) -> "Any":
        length = int(bar * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        signal = np.zeros(length)
        for freq in tones:
            signal += np.sin(2 * np.pi * freq * t) * 0.33
        return signal * _envelope(length, int(0.35 * SAMPLE_RATE), int(0.35 * SAMPLE_RATE)) * 0.22

    # bangun 4 bar loop
    for bar_index in range(loop_bars):
        bar_start = bar_index * bar
        root, tones = CHORDS[bar_index % len(CHORDS)]
        add(pad(tones), bar_start, gain=0.9)
        for beat_index in range(4):
            beat_start = bar_start + beat_index * beat
            add(kick(), beat_start, gain=1.0 if beat_index % 2 == 0 else 0.75)
            add(bass(root), beat_start, gain=0.85)
            add(hat(1.0 if beat_index % 2 == 1 else 0.6), beat_start + beat / 2)
            if beat_index in (1, 3):
                add(snare(), beat_start, gain=0.55)

    # tile loop sampai durasi penuh + fade out akhir
    loops = int(math.ceil(total / loop_s))
    tiled = np.tile(buffer[: int(loop_s * SAMPLE_RATE)], loops)[:n_total]
    fade = int(fade_out_s * SAMPLE_RATE)
    if fade > 0 and fade < n_total:
        tiled[-fade:] *= np.linspace(1, 0, fade)
    # normalisasi ke puncak 0.8 (level final diatur mixer)
    peak = float(np.max(np.abs(tiled))) or 1.0
    tiled = tiled / peak * 0.8

    _write_wav(out_path, tiled)
    return MusicTrack(
        path=Path(out_path),
        duration_s=n_total / SAMPLE_RATE,
        bpm=bpm,
        license=LICENSE_PROCEDURAL,
        source="procedural",
    )


def generate_whoosh(out_path: Path, *, seed: int = 11) -> Path:
    """Efek transisi 'whoosh' (noise tersapu) untuk pergantian scene."""
    np = _require_numpy()
    rng = np.random.default_rng(seed)
    length = int(0.22 * SAMPLE_RATE)
    t = np.arange(length) / SAMPLE_RATE
    noise = rng.uniform(-1, 1, length)
    # sapuan frekuensi: koefisien lowpass bergerak 0.02 -> 0.35 -> 0.05
    cutoff = 0.02 + 0.33 * np.exp(-((t - 0.1) ** 2) / 0.002)
    out = np.zeros(length)
    acc = 0.0
    for i in range(length):
        acc += cutoff[i] * (noise[i] - acc)
        out[i] = acc
    envelope = np.exp(-((t - 0.09) ** 2) / 0.004)
    _write_wav(out_path, out * envelope * 1.6)
    return Path(out_path)


def generate_sfx_track(
    out_path: Path,
    *,
    duration_s: float,
    whoosh_offsets: list[float],
    cues: Optional[list[tuple[float, str]]] = None,
    seed: int = 11,
) -> Path:
    """Susun track SFX: whoosh transisi + isyarat naratif (impact/riser/heartbeat/...).

    `cues` berisi (waktu_detik, jenis). Jenis tidak dikenal diabaikan dengan aman.
    """
    np = _require_numpy()
    n_total = int(max(0.1, duration_s) * SAMPLE_RATE)
    track = np.zeros(n_total)
    cues = cues or []
    if not whoosh_offsets and not cues:
        _write_wav(out_path, track)
        return Path(out_path)

    whoosh_path = out_path.with_name("whoosh.wav")
    generate_whoosh(whoosh_path, seed=seed)
    with wave.open(str(whoosh_path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    whoosh = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32767.0
    whoosh_path.unlink(missing_ok=True)

    def add(signal: "Any", at_s: float, gain: float = 1.0) -> None:
        start = int(at_s * SAMPLE_RATE)
        if start < 0 or start >= n_total:
            return
        end = min(n_total, start + len(signal))
        track[start:end] += signal[: end - start] * gain

    for offset in whoosh_offsets:
        add(whoosh, offset, 0.5)

    for offset, kind in cues:
        signal = _sfx_signal(kind, np)
        if signal is not None:
            add(signal, float(offset), 0.45)

    _write_wav(out_path, track)
    return Path(out_path)


def _sfx_signal(kind: str, np: "Any") -> Optional["Any"]:
    """Sintesis satu efek suara pendek (deterministik)."""
    kind = (kind or "").lower()
    if kind in ("none", "whoosh"):
        return None
    if kind == "impact":
        length = int(0.32 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        freq = 90 * np.exp(-t * 14) + 38
        body = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE)
        noise = np.random.default_rng(3).uniform(-1, 1, length) * np.exp(-t * 22)
        return (body * np.exp(-t * 9) + 0.25 * noise) * 1.1
    if kind == "riser":
        length = int(0.55 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        freq = 180 + 900 * (t / max(t[-1], 1e-6)) ** 2
        tone = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE)
        noise = np.random.default_rng(5).uniform(-1, 1, length)
        return (0.5 * tone + 0.25 * noise) * np.linspace(0.05, 0.9, length)
    if kind == "heartbeat":
        length = int(0.5 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        first = np.sin(2 * np.pi * 58 * t) * np.exp(-((t - 0.08) ** 2) / 0.0025)
        second = np.sin(2 * np.pi * 52 * t) * np.exp(-((t - 0.26) ** 2) / 0.003)
        return (first + 0.8 * second) * 0.9
    if kind == "sparkle":
        length = int(0.4 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        signal = np.zeros(length)
        for freq, delay in ((1568.0, 0.0), (2093.0, 0.06), (2637.0, 0.12)):
            local = np.maximum(0.0, t - delay)
            signal += np.sin(2 * np.pi * freq * local) * np.exp(-local * 14)
        return signal * 0.5
    if kind == "sub_drop":
        length = int(0.6 * SAMPLE_RATE)
        t = np.arange(length) / SAMPLE_RATE
        freq = 120 * np.exp(-t * 5) + 30
        return np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE) * np.exp(-t * 4) * 0.9
    return None


def pick_folder_music(folder: Path | str, *, min_duration_s: float = 5.0) -> Optional[MusicTrack]:
    """Pilih satu lagu dari folder pengguna + lisensinya (licenses.json).

    Tanpa entri lisensi, lagu tetap dipakai tetapi lisensi ditandai 'unknown'
    agar pemanggil dapat memutuskan (default pemakaian: hanya bila diizinkan).
    """
    folder = Path(folder).expanduser()
    if not folder.exists():
        return None
    licenses: dict[str, Any] = {}
    license_file = folder / "licenses.json"
    if license_file.exists():
        try:
            licenses = json.loads(license_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            licenses = {}
    for suffix in (".mp3", ".wav", ".m4a", ".ogg"):
        for path in sorted(folder.rglob(f"*{suffix}")):
            entry = licenses.get(path.name) or {}
            return MusicTrack(
                path=path,
                duration_s=float(entry.get("duration_s") or 0.0),
                bpm=int(entry.get("bpm") or 0),
                license=str(entry.get("license") or "unknown (belum ada entri licenses.json)"),
                attribution=str(entry.get("attribution") or ""),
                source="folder",
            )
    return None


def build_music_for_job(
    out_dir: Path,
    *,
    duration_s: float,
    mode: str,
    music_dir: str = "",
    bpm: int = 120,
) -> Optional[MusicTrack]:
    """Siapkan musik sesuai MUSIC_MODE: procedural | folder | off."""
    mode = (mode or "off").lower()
    if mode == "off":
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "folder":
        track = pick_folder_music(music_dir)
        if track is not None:
            return track
        # folder kosong/tidak ada -> fallback prosedural (jangan gagalkan job)
    return generate_music_bed(out_dir / "music_bed.wav", duration_s, bpm=bpm)
