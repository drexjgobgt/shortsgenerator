"""Grafis prosedural (fallback tanpa biaya): kartu judul, latar gradien, teks.

Dipakai ketika provider image AI tidak tersedia/berlebih anggaran. Menghasilkan
PNG 9:16 dengan gradien halus + teks (nama ubin bawah). Font diambil dari
Windows (Segoe UI/Arial) bila ada; jika tidak, font bawaan Pillow.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

PALETTES: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((14, 22, 40), (52, 84, 140)),      # malam
    ((36, 16, 24), (150, 66, 60)),      # hangat
    ((10, 30, 28), (40, 128, 110)),     # teal
    ((24, 18, 44), (108, 82, 160)),     # ungu
    ((38, 30, 12), (170, 130, 50)),     # pasir
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def create_background(
    out_path: Path,
    *,
    width: int,
    height: int,
    seed_text: str = "",
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    palette_index: Optional[int] = None,
    text_safe_scale: float = 1.0,
    text_vertical: float = 0.34,
) -> Path:
    """Buat PNG 9:16 bergradien + teks opsional (judul/teks layar).

    `text_safe_scale` < 1 mempersempit area teks agar tetap terlihat bila gambar
    persegi nantinya di-crop ke rasio 9:16 (dipakai oleh MockImage).
    `text_vertical` menggeser posisi vertikal teks (0=atas, 1=bawah).
    """
    seed = hashlib.sha256((seed_text or title or "shortsgen").encode("utf-8")).digest()
    if palette_index is None:
        palette_index = seed[0] % len(PALETTES)
    top, bottom = PALETTES[palette_index % len(PALETTES)]

    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)

    # gradien vertikal
    for y in range(height):
        ratio = y / max(1, height - 1)
        blend = tuple(int(top[c] + (bottom[c] - top[c]) * ratio) for c in range(3))
        draw.line([(0, y), (width, y)], fill=blend)

    # lingkaran dekoratif lembut
    accent = Image.new("RGB", (width, height), (0, 0, 0))
    accent_draw = ImageDraw.Draw(accent)
    radius = int(min(width, height) * (0.28 + (seed[1] % 12) / 100))
    cx = int(width * (0.3 + (seed[2] % 40) / 100))
    cy = int(height * (0.3 + (seed[3] % 30) / 100))
    accent_draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=tuple(min(255, c + 60) for c in bottom),
    )
    accent = accent.filter(ImageFilter.GaussianBlur(radius=max(24, radius // 3)))
    image = Image.blend(image, accent, 0.35)

    draw = ImageDraw.Draw(image)
    if title:
        title_font = _font(max(28, int(width // 16 * min(1.0, text_safe_scale)) + 12))
        _draw_wrapped_center(
            draw, title, title_font, width, height * text_vertical,
            fill=(255, 255, 255), safe_scale=text_safe_scale,
        )

    if subtitle:
        sub_font = _font(max(20, int(width // 26 * min(1.0, text_safe_scale)) + 8))
        _draw_wrapped_center(
            draw, subtitle, sub_font, width, height * (text_vertical + 0.12),
            fill=(235, 235, 235), safe_scale=text_safe_scale,
        )

    # watermark kecil "dibuat lokal" agar jelas bukan stok eksternal
    mark_font = _font(max(14, width // 60))
    draw.text((width * 0.04, height * 0.95), "shortsgen · grafis prosedural", font=mark_font, fill=(200, 200, 200))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")
    return out_path


def _draw_wrapped_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
    center_y: float,
    *,
    fill: tuple[int, int, int],
    max_chars: Optional[int] = None,
    safe_scale: float = 1.0,
) -> None:
    base_chars = max(12, int(width / (getattr(font, "size", 32) * 0.55)))
    max_chars = max_chars or max(10, int(base_chars * max(0.3, min(1.0, safe_scale))))
    lines: list[str] = []
    current = ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    line_height = int(getattr(font, "size", 32) * 1.3)
    total_height = line_height * len(lines)
    y = center_y - total_height / 2
    for line in lines:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
        except Exception:
            text_width = len(line) * getattr(font, "size", 32) * 0.55
        draw.text(
            ((width - text_width) / 2, y),
            line,
            font=font,
            fill=fill,
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )
        y += line_height
