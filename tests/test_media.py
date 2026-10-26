"""Test lapisan media: FFmpeg wrapper, subtitle, grafis (butuh FFmpeg lokal)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shortsgen.media.ffmpeg import FFmpeg, FFmpegError
from shortsgen.media.graphics import create_background
from shortsgen.media.subtitles import (
    Cue,
    build_cues_from_scenes,
    build_karaoke_cues,
    split_into_cues,
    to_ass,
    to_srt,
    to_vtt,
    validate_cues,
    write_subtitles,
)
from shortsgen.providers.mock import MockTTS

ffmpeg_available = shutil.which("ffmpeg") is not None or Path("D:/tools/ffmpeg/bin/ffmpeg.exe").exists()
requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available, reason="FFmpeg tidak terpasang")


# ---------------------------------------------------------------------------
# Subtitle (tanpa FFmpeg)
# ---------------------------------------------------------------------------

def test_split_into_cues_proportional() -> None:
    cues = split_into_cues("Satu dua tiga empat lima enam tujuh delapan.", 0.0, 8.0)
    assert cues
    assert cues[0].start_s == 0.0
    assert abs(cues[-1].end_s - 8.0) < 1e-6
    for previous, current in zip(cues, cues[1:]):
        assert current.start_s >= previous.end_s - 1e-9


def test_split_long_sentence_wraps() -> None:
    from shortsgen.media.subtitles import MAX_CHARS_PER_LINE, MAX_LINES

    text = " ".join(["kata"] * 60)
    cues = split_into_cues(text, 0.0, 20.0)
    assert len(cues) > 1
    assert all(len(cue.text) <= MAX_CHARS_PER_LINE * MAX_LINES for cue in cues)


def test_build_karaoke_cues_chunks() -> None:
    scenes = [{"index": 0, "start_s": 0.0, "end_s": 6.0,
               "narration": "Langit Mars berwarna merah tapi senjanya justru biru"}]
    cues = build_karaoke_cues(scenes, words_per_chunk=2)
    assert len(cues) >= 3                      # potongan pendek, bukan satu blok panjang
    assert all(len(c.text.split()) <= 3 for c in cues)
    assert cues[0].start_s == 0.0
    assert abs(cues[-1].end_s - 6.0) < 1e-6
    for previous, current in zip(cues, cues[1:]):
        assert current.start_s >= previous.end_s - 1e-9


def test_to_ass_dynamic_has_animation_and_hook() -> None:
    from shortsgen.media.subtitles import to_ass_dynamic

    cues = [Cue(index=1, start_s=0.0, end_s=1.2, text="Langit Mars")]
    ass = to_ass_dynamic(cues, width=1080, height=1920, hook_text="Senja Mars biru?")
    assert "Style: Hook" in ass and "Style: Caption" in ass
    assert "fscx112" in ass            # animasi pop-in caption
    assert "Dialogue: 1," in ass       # layer hook terpisah
    assert "Senja Mars biru?" in ass
    assert "PlayResY: 1920" in ass


def test_karaoke_cue_text_escapes_braces() -> None:
    from shortsgen.media.subtitles import to_ass_dynamic

    ass = to_ass_dynamic(
        [Cue(index=1, start_s=0.0, end_s=1.0, text="nilai {aneh} di sini")],
        width=720,
        height=1280,
    )
    assert "{aneh}" not in ass  # kurung kurawal di-escape agar tidak jadi override block


def test_srt_uses_real_newline_and_ass_uses_backslash_n() -> None:
    text = "Kalimat panjang yang pasti akan dibagi menjadi dua baris di layar"
    srt = to_srt([Cue(index=1, start_s=0.0, end_s=3.0, text=text)])
    assert "\\N" not in srt
    assert "\n" in srt.split("-->", 1)[1]
    ass = to_ass([Cue(index=1, start_s=0.0, end_s=3.0, text=text)], width=1080, height=1920)
    assert "\\N" in ass


def test_music_bed_generation(tmp_path: Path) -> None:
    """Musik prosedural: WAV valid, durasi sesuai, bebas lisensi."""
    wave_mod = __import__("wave")

    from shortsgen.media.music import generate_music_bed

    track = generate_music_bed(tmp_path / "music.wav", 12.0, bpm=120)
    assert track.path.exists()
    assert track.license.startswith("dibuat lokal")
    with wave_mod.open(str(track.path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    assert rate == 48000
    assert abs(frames / rate - track.duration_s) < 0.05
    assert 12.0 <= track.duration_s <= 13.0


def test_sfx_track_places_whoosh_at_offsets(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    wave_mod = __import__("wave")

    from shortsgen.media.music import generate_sfx_track

    path = generate_sfx_track(tmp_path / "sfx.wav", duration_s=6.0, whoosh_offsets=[0.0, 3.0])
    with wave_mod.open(str(path), "rb") as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(float)
    assert len(data) == int(6.0 * 48000)
    energy_at_3s = np.max(np.abs(data[int(3.0 * 48000): int(3.2 * 48000)]))
    energy_at_1s = np.max(np.abs(data[int(1.0 * 48000): int(1.2 * 48000)]))
    assert energy_at_3s > 1000        # ada whoosh
    assert energy_at_1s == 0          # sisanya senyap


def test_audio_graph_keeps_voice_level() -> None:
    """amix harus normalize=0 agar narasi tidak diredam (bug loudness -24 LUFS)."""
    from shortsgen.pipeline.stages.render import _audio_graph

    graph, label = _audio_graph(music_index=2, sfx_index=3, music_volume_db=-18.0)
    assert "normalize=0" in graph
    assert "sidechaincompress" in graph
    assert label == "[aout]"
    # tanpa musik/sfx: pakai audio asli apa adanya
    graph_v, label_v = _audio_graph(music_index=None, sfx_index=None, music_volume_db=-18.0)
    assert graph_v == "" and label_v == "1:a:0"


def test_plan_shots_splits_long_scenes() -> None:
    from shortsgen.pipeline.stages.render import _plan_shots

    scenes = [
        {"index": 0, "start_s": 0.0, "duration_s": 3.0, "asset": {"path": "a.png"}},
        {"index": 1, "start_s": 3.0, "duration_s": 10.0, "asset": {"path": "b.mp4"}},
    ]
    shots = _plan_shots(scenes, max_shot_s=4.0)
    # scene 3 dtk -> 1 shot; scene 10 dtk -> 3 shot
    assert len(shots) == 4
    assert all(s["duration_s"] <= 4.0 + 1e-6 for s in shots)
    # zoom bergantian
    assert [s["zoom_in"] for s in shots] == [True, False, True, False]
    # timeline menerus
    total = sum(s["duration_s"] for s in shots)
    assert abs(total - 13.0) < 0.01


def test_plan_shots_skips_scenes_without_asset() -> None:
    from shortsgen.pipeline.stages.render import _plan_shots

    scenes = [
        {"index": 0, "start_s": 0.0, "duration_s": 3.0, "asset": None},
        {"index": 1, "start_s": 3.0, "duration_s": 2.0, "asset": {"path": "b.png"}},
    ]
    shots = _plan_shots(scenes, max_shot_s=4.0)
    assert len(shots) == 1
    assert shots[0]["scene_index"] == 1


def test_split_empty_returns_no_cues() -> None:
    assert split_into_cues("", 0.0, 5.0) == []
    assert split_into_cues("halo", 5.0, 5.0) == []


def test_build_cues_from_storyboard_scenes() -> None:
    scenes = [
        {"index": 0, "start_s": 0.0, "end_s": 4.0, "narration": "Kalimat pertama pendek."},
        {"index": 1, "start_s": 4.0, "end_s": 9.0, "narration": "Kalimat kedua sedikit lebih panjang dari yang pertama."},
    ]
    cues = build_cues_from_scenes(scenes)
    assert len(cues) >= 2
    assert cues[0].start_s == 0.0
    assert cues[-1].end_s == pytest.approx(9.0)


def test_srt_vtt_ass_formats(tmp_path: Path) -> None:
    cues = [Cue(index=1, start_s=0.0, end_s=2.5, text="Halo semua"), Cue(index=2, start_s=2.5, end_s=5.0, text="Selamat datang")]
    srt = to_srt(cues)
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "1\n" in srt
    vtt = to_vtt(cues)
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.500" in vtt
    ass = to_ass(cues, width=1080, height=1920)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:02.50" in ass

    files = write_subtitles(cues, tmp_path, width=1080, height=1920)
    assert files["srt"].exists() and files["vtt"].exists() and files["ass"].exists()


def test_validate_cues_detects_problems() -> None:
    bad = [Cue(index=1, start_s=0.0, end_s=3.0, text="a"), Cue(index=2, start_s=2.0, end_s=4.0, text="b")]
    issues = validate_cues(bad, media_duration_s=10.0)
    assert any("tumpang tindih" in issue for issue in issues)

    overrun = [Cue(index=1, start_s=0.0, end_s=12.0, text="x")]
    assert any("melewati durasi" in issue for issue in validate_cues(overrun, media_duration_s=10.0))

    good = [Cue(index=1, start_s=0.0, end_s=3.0, text="aman")]
    assert validate_cues(good, media_duration_s=10.0) == []


# ---------------------------------------------------------------------------
# Grafis prosedural (butuh Pillow, tanpa FFmpeg)
# ---------------------------------------------------------------------------

def test_create_background_png(tmp_path: Path) -> None:
    out = tmp_path / "bg.png"
    create_background(out, width=360, height=640, seed_text="topik", title="Judul", subtitle="sub")
    assert out.exists() and out.stat().st_size > 1000
    raw = out.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    import struct

    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (360, 640)


# ---------------------------------------------------------------------------
# FFmpeg wrapper (integrasi lokal)
# ---------------------------------------------------------------------------

def make_ffmpeg(tmp_path: Path) -> FFmpeg:
    ffmpeg_path = ""
    if not shutil.which("ffmpeg") and Path("D:/tools/ffmpeg/bin/ffmpeg.exe").exists():
        ffmpeg_path = "D:/tools/ffmpeg/bin/ffmpeg.exe"
    return FFmpeg(ffmpeg_path)


@requires_ffmpeg
def test_ffmpeg_probe_and_audio_ops(tmp_path: Path) -> None:
    ffmpeg = make_ffmpeg(tmp_path)
    # WAV dari MockTTS
    wav = tmp_path / "voice.wav"
    MockTTS(words_per_minute=120).synthesize("satu dua tiga empat lima", wav)
    info = ffmpeg.probe(wav)
    assert info.has_audio and info.duration_s > 0.5

    silence = ffmpeg.make_silence(tmp_path / "sil.wav", 0.5)
    assert ffmpeg.duration(silence) == pytest.approx(0.5, abs=0.05)

    padded = ffmpeg.pad_audio_to(wav, tmp_path / "pad.wav", 5.0)
    assert ffmpeg.duration(padded) == pytest.approx(5.0, abs=0.1)

    merged = ffmpeg.concat_audio([padded, silence], tmp_path / "merged.wav")
    assert ffmpeg.duration(merged) == pytest.approx(5.5, abs=0.15)

    normalized = ffmpeg.normalize_loudness(merged, tmp_path / "norm.wav")
    loudness = ffmpeg.measure_loudness(normalized)
    assert "input_i" in loudness
    assert ffmpeg.measure_peak(normalized) is not None
    ffmpeg.decode_check(normalized)


@requires_ffmpeg
def test_ffmpeg_extract_frame_and_missing_file(tmp_path: Path) -> None:
    ffmpeg = make_ffmpeg(tmp_path)
    image = tmp_path / "img.png"
    create_background(image, width=180, height=320, seed_text="x")
    clip = tmp_path / "clip.mp4"
    ffmpeg.run(
        [
            "-loop", "1", "-i", str(image), "-t", "1",
            "-vf", "scale=180:320,format=yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-an",
            str(clip),
        ]
    )
    info = ffmpeg.probe(clip)
    assert (info.width, info.height) == (180, 320)
    frame = ffmpeg.extract_frame(clip, tmp_path / "thumb.jpg", at_s=0.2)
    assert frame.exists() and frame.stat().st_size > 500

    with pytest.raises(FFmpegError):
        ffmpeg.probe(tmp_path / "tidak-ada.mp4")
