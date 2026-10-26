"""Pembuatan & validasi subtitle (SRT untuk YouTube, ASS untuk burn-in).

Timing TIDAK memakai STT: dibagi proporsional dari durasi audio nyata per scene
yang diukur ffprobe (lihat stage tts). Setiap kalimat dipecah menjadi cue
pendek agar terbaca di layar vertikal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

MAX_CHARS_PER_LINE = 30
MAX_LINES = 2
MIN_CUE_S = 0.6

# Caption dinamis (gaya karaoke): 2-3 kata per potongan agar mata terus "ditarik".
KARAOKE_WORDS_PER_CHUNK = 2
HOOK_OVERLAY_SECONDS = 2.5

# Warna ASS (format &HAABBGGRR): putih untuk caption, kuning aksen untuk hook.
COLOR_WHITE = "&H00FFFFFF"
COLOR_ACCENT = "&H0000D7FF"   # kuning keemasan
COLOR_BOX = "&H99000000"      # kotak semi-transparan untuk hook

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Cue:
    index: int
    start_s: float
    end_s: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end_s - self.start_s)


def split_into_cues(text: str, start_s: float, end_s: float, *, first_index: int = 1) -> list[Cue]:
    """Bagi teks satu scene menjadi cue-cue pendek dengan timing proporsional.

    Aturan: setiap cue <= MAX_CHARS_PER_LINE*MAX_LINES karakter; cue yang terlalu
    pendek digabung ke tetangga HANYA bila hasil gabungan masih memenuhi batas
    panjang (mencegah cue tak terbaca).
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    duration = max(0.0, end_s - start_s)
    if not text or duration <= 0:
        return []

    pieces = _merge_short_pieces(_wrap_pieces(text), duration)
    if not pieces:
        return []

    total_chars = sum(len(p) for p in pieces)
    cues: list[Cue] = []
    cursor = start_s
    index = first_index
    for position, piece in enumerate(pieces):
        share = len(piece) / total_chars if total_chars else 1 / len(pieces)
        piece_duration = duration * share
        piece_end = end_s if position == len(pieces) - 1 else cursor + piece_duration
        cues.append(Cue(index=index, start_s=round(cursor, 3), end_s=round(piece_end, 3), text=piece))
        index += 1
        cursor = piece_end
    return cues


def _merge_short_pieces(pieces: list[str], duration: float) -> list[str]:
    """Gabungkan potongan yang durasinya < MIN_CUE_S dengan tetangga (batas panjang dijaga)."""
    limit = MAX_CHARS_PER_LINE * MAX_LINES
    total_chars = sum(len(p) for p in pieces) or 1
    merged: list[str] = []
    for piece in pieces:
        piece_duration = duration * len(piece) / total_chars
        if (
            merged
            and piece_duration < MIN_CUE_S
            and len(merged[-1]) + 1 + len(piece) <= limit
        ):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    # pass kedua: gabung ke depan bila cue awal masih terlalu pendek
    result: list[str] = []
    index = 0
    while index < len(merged):
        current = merged[index]
        current_duration = duration * len(current) / total_chars
        if (
            current_duration < MIN_CUE_S
            and index + 1 < len(merged)
            and len(current) + 1 + len(merged[index + 1]) <= limit
        ):
            result.append(f"{current} {merged[index + 1]}")
            index += 2
            continue
        result.append(current)
        index += 1
    return [piece for piece in result if piece.strip()]


def _wrap_pieces(text: str) -> list[str]:
    """Pecah teks menjadi potongan <= 2 baris x MAX_CHARS_PER_LINE."""
    limit = MAX_CHARS_PER_LINE * MAX_LINES
    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text) or [text]:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        # pecah per kata
        current = ""
        for word in sentence.split():
            candidate = f"{current} {word}".strip()
            if len(candidate) > limit and current:
                pieces.append(current)
                current = word
            else:
                current = candidate
        if current:
            pieces.append(current)
    return pieces


def build_cues_from_scenes(scenes: Sequence[dict]) -> list[Cue]:
    """Bangun cue dari daftar scene ber-timing (start_s/end_s + narration)."""
    cues: list[Cue] = []
    index = 1
    for scene in scenes:
        start = float(scene.get("start_s") or 0.0)
        end = float(scene.get("end_s") or 0.0)
        if end <= start:
            continue
        new_cues = split_into_cues(str(scene.get("narration") or ""), start, end, first_index=index)
        cues.extend(new_cues)
        index += len(new_cues)
    return cues


def build_karaoke_cues(scenes: Sequence[dict], *, words_per_chunk: int = KARAOKE_WORDS_PER_CHUNK) -> list[Cue]:
    """Caption gaya karaoke: potongan 2-3 kata per cue, timing proporsional per kata.

    Dipakai untuk gaya caption `dynamic` (standar Shorts beretensi tinggi).
    """
    cues: list[Cue] = []
    index = 1
    for scene in scenes:
        start = float(scene.get("start_s") or 0.0)
        end = float(scene.get("end_s") or 0.0)
        narration = re.sub(r"\s+", " ", str(scene.get("narration") or "")).strip()
        duration = end - start
        if not narration or duration <= 0:
            continue
        words = narration.split()
        chunks: list[str] = []
        i = 0
        while i < len(words):
            # kata pendek -> 3 kata per potongan, kata panjang -> 2
            span = 3 if all(len(w) <= 6 for w in words[i : i + 3]) and i + 2 < len(words) else max(1, words_per_chunk)
            chunks.append(" ".join(words[i : i + span]))
            i += span
        total_chars = sum(len(c) for c in chunks) or 1
        cursor = start
        for position, chunk in enumerate(chunks):
            share = len(chunk) / total_chars
            chunk_end = end if position == len(chunks) - 1 else cursor + duration * share
            if chunk_end - cursor >= 0.25:  # abaikan potongan super pendek
                cues.append(
                    Cue(index=index, start_s=round(cursor, 3), end_s=round(chunk_end, 3), text=chunk)
                )
                index += 1
            cursor = chunk_end
    return cues


def to_srt(cues: Iterable[Cue]) -> str:
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{_srt_time(cue.start_s)} --> {_srt_time(cue.end_s)}\n{_two_lines(cue.text)}\n"
        )
    return "\n".join(blocks)


def to_vtt(cues: Iterable[Cue]) -> str:
    blocks = ["WEBVTT", ""]
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{_vtt_time(cue.start_s)} --> {_vtt_time(cue.end_s)}\n{_two_lines(cue.text)}\n"
        )
    return "\n".join(blocks)


def to_ass(cues: Iterable[Cue], *, width: int, height: int, font: str = "Segoe UI", font_size: int = 64) -> str:
    """ASS untuk burn-in: resolusi eksplisit -> ukuran font deterministik."""
    style = (
        f"Style: Default,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,4,0,2,80,80,140,1"
    )
    events = [
        "Dialogue: 0,{start},{end},Default,,0,0,0,,{text}".format(
            start=_ass_time(cue.start_s), end=_ass_time(cue.end_s), text=_two_lines(cue.text, ass=True)
        )
        for cue in cues
    ]
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"{style}\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        + "\n".join(events)
        + "\n"
    )


def build_karaoke_cues_from_measured(
    scenes: Sequence[dict],
    measured_scenes: Sequence[dict],
    *,
    words_per_chunk: int = KARAOKE_WORDS_PER_CHUNK,
) -> list[Cue]:
    """Karaoke dari klausa TTS yang benar-benar diukur (lebih akurat dari proporsi).

    `measured_scenes` berasal dari timings.json (`scenes[].clauses[]`). Bila klausa
    tidak tersedia, jatuh kembali ke pembagian proporsional per scene.
    """
    measured = {int(item.get("scene_index", -1)): item for item in measured_scenes}
    cues: list[Cue] = []
    index = 1
    for scene in scenes:
        scene_index = int(scene.get("index", -1))
        item = measured.get(scene_index)
        clauses = (item or {}).get("clauses") or []
        if not clauses:
            scene_cues = build_karaoke_cues([scene], words_per_chunk=words_per_chunk)
            for cue in scene_cues:
                cues.append(Cue(index=index, start_s=cue.start_s, end_s=cue.end_s, text=cue.text))
                index += 1
            continue
        cursor = float(scene.get("start_s") or item.get("start_s") or 0.0)
        scene_end = float(scene.get("end_s") or cursor)
        for clause in clauses:
            if clause.get("start_offset_s") is not None:
                cursor = float(scene.get("start_s") or 0.0) + float(clause["start_offset_s"])
            text = re.sub(r"\s+", " ", str(clause.get("text") or "")).strip()
            duration = float(clause.get("duration_s") or 0.0)
            if not text or duration <= 0:
                continue
            words = text.split()
            chunks: list[str] = []
            i = 0
            while i < len(words):
                span = 3 if all(len(w) <= 6 for w in words[i : i + 3]) and i + 2 < len(words) else max(1, words_per_chunk)
                chunks.append(" ".join(words[i : i + span]))
                i += span
            total_chars = sum(len(chunk) for chunk in chunks) or 1
            clause_start = min(cursor, scene_end)
            clause_end = min(scene_end, cursor + duration)
            chunk_cursor = clause_start
            for position, chunk in enumerate(chunks):
                share = len(chunk) / total_chars
                chunk_end = clause_end if position == len(chunks) - 1 else chunk_cursor + (clause_end - clause_start) * share
                if chunk_end - chunk_cursor >= 0.18:
                    cues.append(
                        Cue(index=index, start_s=round(chunk_cursor, 3), end_s=round(chunk_end, 3), text=chunk)
                    )
                    index += 1
                chunk_cursor = chunk_end
            cursor += float(clause.get("padded_s") or duration)
    return cues


def build_overlay_cues(scenes: Sequence[dict]) -> list[dict]:
    """Overlay fakta/angka (fact chip) untuk scene evidence/twist/payoff.

    Bukan caption: teks layar singkat yang muncul sebentar untuk menegaskan poin.
    """
    overlays: list[dict] = []
    seen: set[str] = set()
    for scene in scenes:
        role = str(scene.get("role") or "")
        if role not in ("evidence", "twist", "payoff", "context"):
            continue
        text = re.sub(r"\s+", " ", str(scene.get("on_screen_text") or "")).strip()
        if not text or text.lower() in seen:
            continue
        duration = float(scene.get("duration_s") or 0.0)
        if duration <= 0.6:
            continue
        start = float(scene.get("start_s") or 0.0) + min(0.35, duration * 0.15)
        end = start + min(2.2, max(0.9, duration * 0.62))
        overlays.append(
            {
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "text": text[:40],
                "kind": "fact_chip" if role in ("evidence", "context") else "keyword",
            }
        )
        seen.add(text.lower())
    return overlays


def to_ass_dynamic(
    cues: Iterable[Cue],
    *,
    width: int,
    height: int,
    hook_text: Optional[str] = None,
    hook_seconds: float = HOOK_OVERLAY_SECONDS,
    overlays: Optional[Sequence[dict]] = None,
    font: str = "Segoe UI",
    caption_size: int = 86,
    hook_size: int = 72,
) -> str:
    """ASS dinamis: caption pop-in kuning + overlay hook + fact chip.

    Caption muncul potongan-per-potongan (2-3 kata) dengan animasi skala singkat
    agar perhatian penonton tetap terjaga (praktik standar Shorts beretensi tinggi).
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"           # auto-wrap: hook panjang dilipat otomatis
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        # caption bawah: besar, tebal, kuning, outline hitam tebal
        f"Style: Caption,{font},{caption_size},{COLOR_ACCENT},{COLOR_WHITE},&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,6,3,2,60,60,300,1\n"
        # overlay hook di area aman atas-tengah (kotak semi-transparan)
        f"Style: Hook,{font},{hook_size},{COLOR_WHITE},{COLOR_WHITE},&H00000000,{COLOR_BOX},"
        "-1,0,0,0,100,100,0,0,3,5,0,8,90,90,360,1\n"
        # fact chip: kotak aksen, teks putih, di atas caption
        f"Style: Fact,{font},{max(40, caption_size - 24)},{COLOR_WHITE},{COLOR_WHITE},&H00000000,&H66000000,"
        "-1,0,0,0,100,100,1,0,3,4,0,1,80,80,540,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    events: list[str] = []
    if hook_text:
        # escape dulu, lalu lipat (agar \N hasil lipatan tidak ikut di-escape)
        safe_hook = _wrap_hook(_escape_ass(hook_text.strip()))
        events.append(
            "Dialogue: 1,{start},{end},Hook,,0,0,0,,"
            "{{\\fad(120,220)\\fscx96\\fscy96\\t(0,140,\\fscx104\\fscy104)}}{text}".format(
                start=_ass_time(0.15),
                end=_ass_time(max(0.6, hook_seconds)),
                text=safe_hook,
            )
        )
    for overlay in overlays or []:
        start = float(overlay.get("start_s") or 0.0)
        end = float(overlay.get("end_s") or 0.0)
        text = _wrap_hook(_escape_ass(str(overlay.get("text") or "")), max_chars_per_line=22, max_lines=2)
        if end <= start or not text:
            continue
        events.append(
            "Dialogue: 2,{start},{end},Fact,,0,0,0,,"
            "{{\\an1\\pos(70,{y})\\fad(120,160)\\fscx92\\fscy92\\t(0,140,\\fscx100\\fscy100)}}{text}".format(
                start=_ass_time(start),
                end=_ass_time(end),
                y=height - 560,
                text=text,
            )
        )
    for cue in cues:
        events.append(
            "Dialogue: 0,{start},{end},Caption,,0,0,0,,"
            "{{\\fad(60,80)\\fscx112\\fscy112\\t(0,120,\\fscx100\\fscy100)}}{text}".format(
                start=_ass_time(cue.start_s),
                end=_ass_time(cue.end_s),
                text=_escape_ass(cue.text),
            )
        )
    return header + "\n".join(events) + "\n"


def _escape_ass(text: str) -> str:
    """Escape karakter khusus ASS (kurung kurawal = override block)."""
    return str(text).replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _wrap_hook(text: str, *, max_chars_per_line: int = 26, max_lines: int = 3) -> str:
    """Lipat teks hook menjadi beberapa baris eksplisit (\\) agar tidak terpotong."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars_per_line and current:
            lines.append(current)
            current = word
        else:
            current = candidate
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return "\\N".join(lines[:max_lines])


def validate_cues(cues: Sequence[Cue], *, media_duration_s: float, tolerance_s: float = 0.5) -> list[str]:
    """Validasi timing subtitle; kembalikan daftar masalah (kosong = valid)."""
    issues: list[str] = []
    if not cues:
        return ["tidak ada cue subtitle"]
    previous_end = -1.0
    for cue in cues:
        if cue.end_s <= cue.start_s:
            issues.append(f"cue {cue.index}: end <= start ({cue.start_s} -> {cue.end_s})")
        if cue.start_s < previous_end - 1e-6:
            issues.append(f"cue {cue.index}: tumpang tindih dengan cue sebelumnya")
        if cue.end_s > media_duration_s + tolerance_s:
            issues.append(
                f"cue {cue.index}: melewati durasi media ({cue.end_s:.2f}s > {media_duration_s:.2f}s)"
            )
        if len(cue.text) > MAX_CHARS_PER_LINE * MAX_LINES:
            issues.append(f"cue {cue.index}: teks terlalu panjang ({len(cue.text)} karakter)")
        previous_end = max(previous_end, cue.end_s)
    return issues


# -- format waktu ------------------------------------------------------------

def _two_lines(text: str, *, ass: bool = False) -> str:
    """Bagi teks menjadi maksimal dua baris seimbang.

    Pemisah: baris baru asli untuk SRT/VTT, `\\N` untuk ASS.
    """
    words = text.split()
    if len(text) <= MAX_CHARS_PER_LINE or len(words) < 2:
        return text
    best: tuple[int, str, str] | None = None
    for split_at in range(1, len(words)):
        left = " ".join(words[:split_at])
        right = " ".join(words[split_at:])
        score = abs(len(left) - len(right))
        if best is None or score < best[0]:
            best = (score, left, right)
    if best is None:
        return text
    _, left, right = best
    separator = "\\N" if ass else "\n"
    return f"{left}{separator}{right}"


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def _ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    hours, cs = divmod(cs, 360_000)
    minutes, cs = divmod(cs, 6_000)
    secs, cs = divmod(cs, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def write_subtitles(
    cues: Sequence[Cue],
    out_dir: Path,
    *,
    width: int,
    height: int,
    hook_text: Optional[str] = None,
    style: str = "static",
) -> dict[str, Path]:
    """Tulis SRT/VTT (untuk YouTube) + ASS untuk burn-in.

    `style='dynamic'` menghasilkan ASS karaoke beranimasi (dan menyimpan versi
    statis sebagai `subtitle_static.ass` untuk perbandingan/audit).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = out_dir / "subtitle.srt"
    vtt_path = out_dir / "subtitle.vtt"
    ass_path = out_dir / "subtitle.ass"
    srt_path.write_text(to_srt(cues), encoding="utf-8")
    vtt_path.write_text(to_vtt(cues), encoding="utf-8")
    if style == "dynamic":
        ass_path.write_text(
            to_ass_dynamic(cues, width=width, height=height, hook_text=hook_text),
            encoding="utf-8",
        )
        (out_dir / "subtitle_static.ass").write_text(
            to_ass(cues, width=width, height=height), encoding="utf-8"
        )
    else:
        ass_path.write_text(to_ass(cues, width=width, height=height), encoding="utf-8")
    return {"srt": srt_path, "vtt": vtt_path, "ass": ass_path}
