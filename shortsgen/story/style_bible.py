"""Style bible per video: bahasa visual yang konsisten untuk semua scene.

Tujuan:
- Menghindari setiap gambar punya gaya/palet/lensa berbeda.
- Menyediakan `style_prefix` yang dipakai ulang di semua prompt gambar.
- Menyimpan aturan kontinuitas (karakter/objek/lingkungan) dan negative prompt.
- Menyediakan pemeriksaan konsistensi palet sederhana (heuristik, bukan penilaian objektif).
"""
from __future__ import annotations

import colorsys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image, ImageStat

STYLE_VERSION = 1


@dataclass(frozen=True)
class StylePreset:
    id: str
    name: str
    visual_style: str
    palette: tuple[tuple[str, str, str], ...]   # (nama, hex, peran)
    lighting: str
    lens: str
    texture: str
    realism: str
    camera_language: dict[str, str]
    character_rules: str
    negative_prompt: str
    music_mood: str
    sfx_palette: tuple[str, ...]


_PRESETS: dict[str, StylePreset] = {
    "cinematic_3d_animals": StylePreset(
        id="cinematic_3d_animals",
        name="Cinematic 3D Wildlife",
        visual_style=(
            "cinematic 3D animated wildlife documentary still, stylized realism, "
            "detailed skin and eyes, volumetric natural light, atmospheric depth"
        ),
        palette=(
            ("deep teal", "#0B3C49", "background"),
            ("emerald", "#1F7A6B", "midground"),
            ("warm amber", "#F2A65A", "accent"),
            ("bone white", "#F4EBDD", "highlight"),
        ),
        lighting="soft volumetric light from above, rim light on the subject, gentle god rays",
        lens="35mm lens, shallow depth of field, subject sharp, background softly blurred",
        texture="subtle film grain, realistic micro-detail, no plastic look",
        realism="stylized realism, believable anatomy, expressive eyes",
        camera_language={
            "push_in": "slow push-in to build curiosity",
            "pull_out": "pull-out to reveal context",
            "pan_left": "lateral pan following the subject",
            "pan_right": "lateral pan revealing a detail",
            "tilt_up": "tilt up to reveal scale",
            "orbit": "gentle orbit to show dimension",
            "macro": "macro detail for evidence",
            "handheld": "subtle handheld energy for surprise",
        },
        character_rules=(
            "keep the same individual across scenes: same markings, same eye color, "
            "same body proportions; no extra limbs or species changes"
        ),
        negative_prompt=(
            "no text, no captions, no logos, no watermarks, no human faces, no celebrities, "
            "no extra limbs, no deformed anatomy, no collage, no split screen, no low resolution"
        ),
        music_mood="curious, building, playful, awe",
        sfx_palette=("whoosh", "impact", "riser", "heartbeat", "sparkle"),
    ),
    "cinematic_3d_space": StylePreset(
        id="cinematic_3d_space",
        name="Cinematic 3D Space",
        visual_style=(
            "cinematic 3D space documentary still, stylized realism, vast scale, "
            "volumetric starlight, atmospheric haze, detailed planetary surfaces"
        ),
        palette=(
            ("deep navy", "#070B1A", "background"),
            ("violet", "#3B2F63", "midground"),
            ("solar gold", "#F6C453", "accent"),
            ("ice blue", "#BFE3FF", "highlight"),
        ),
        lighting="dramatic single-source sunlight, deep shadows, soft nebula glow",
        lens="24mm wide lens, deep focus, subtle anamorphic flare",
        texture="fine grain, realistic dust and starfields, no cartoon outlines",
        realism="stylized realism, scientifically plausible scale",
        camera_language={
            "push_in": "slow push-in toward the subject",
            "pull_out": "pull-out to reveal scale",
            "pan_left": "slow lateral pan across the sky",
            "pan_right": "pan toward a distant detail",
            "tilt_up": "tilt up to reveal a giant structure",
            "orbit": "orbital move around the subject",
            "macro": "close detail of surface/ice",
            "handheld": "subtle drift for unease",
        },
        character_rules="keep the same planet/object design and lighting direction across scenes",
        negative_prompt=(
            "no text, no logos, no watermarks, no fantasy spaceships unless requested, "
            "no impossible physics, no collage, no low resolution"
        ),
        music_mood="mysterious, vast, building, awe",
        sfx_palette=("whoosh", "sub_drop", "riser", "impact", "sparkle"),
    ),
    "cinematic_3d_history": StylePreset(
        id="cinematic_3d_history",
        name="Cinematic 3D Historical Diorama",
        visual_style=(
            "cinematic 3D historical diorama still, stylized realism, miniature set feel, "
            "detailed materials, dramatic chiaroscuro"
        ),
        palette=(
            ("charcoal", "#1A1613", "background"),
            ("sepia", "#8A5A3B", "midground"),
            ("brass", "#C9A227", "accent"),
            ("parchment", "#E8D9B5", "highlight"),
        ),
        lighting="warm practical light, candle/lantern glow, deep shadows",
        lens="40mm lens, shallow depth of field, diorama tilt-shift feel",
        texture="aged paper, wood, stone, subtle dust",
        realism="stylized historical realism, no fantasy armor",
        camera_language={
            "push_in": "push-in on a decisive detail",
            "pull_out": "pull-out to reveal the diorama",
            "pan_left": "pan across the scene",
            "pan_right": "pan to a new clue",
            "tilt_up": "tilt up to reveal scale",
            "orbit": "slow orbit around the diorama",
            "macro": "macro on an artifact",
            "handheld": "subtle handheld for chaos",
        },
        character_rules="keep costumes, props, and architecture consistent with the stated era",
        negative_prompt=(
            "no text, no logos, no watermarks, no modern objects unless requested, "
            "no fantasy armor, no collage, no low resolution"
        ),
        music_mood="tense, curious, dramatic, reflective",
        sfx_palette=("whoosh", "impact", "riser", "sparkle"),
    ),
    "cinematic_3d_tech": StylePreset(
        id="cinematic_3d_tech",
        name="Cinematic 3D Technology",
        visual_style=(
            "clean cinematic 3D product visualization, stylized realism, precise materials, "
            "studio lighting, subtle depth"
        ),
        palette=(
            ("graphite", "#101418", "background"),
            ("cyan", "#22D3EE", "accent"),
            ("orange", "#FB923C", "highlight"),
            ("steel", "#94A3B8", "midground"),
        ),
        lighting="soft studio key light, crisp rim light, controlled reflections",
        lens="50mm lens, shallow depth of field, macro detail",
        texture="brushed metal, glass, matte polymer, fine grain",
        realism="stylized realism, believable engineering",
        camera_language={
            "push_in": "push-in on the mechanism",
            "pull_out": "pull-out to reveal the system",
            "pan_left": "pan across the components",
            "pan_right": "pan to the next component",
            "tilt_up": "tilt up to reveal scale",
            "orbit": "orbit to show 3D form",
            "macro": "macro on a precise detail",
            "handheld": "subtle handheld for urgency",
        },
        character_rules="keep the same device design and material language across scenes",
        negative_prompt=(
            "no text, no logos, no watermarks, no impossible physics, no collage, "
            "no low resolution, no generic stock-photo look"
        ),
        music_mood="clean, driving, curious, precise",
        sfx_palette=("whoosh", "impact", "riser", "sparkle"),
    ),
    "cinematic_3d_playful": StylePreset(
        id="cinematic_3d_playful",
        name="Cinematic 3D Playful",
        visual_style=(
            "playful cinematic 3D animated film still, stylized realism, expressive shapes, "
            "soft global illumination, rich color"
        ),
        palette=(
            ("midnight teal", "#0E2A33", "background"),
            ("magenta", "#D9468F", "accent"),
            ("amber", "#F5B942", "highlight"),
            ("cream", "#FFF1D6", "midground"),
        ),
        lighting="soft global illumination, warm practical accents, gentle rim light",
        lens="35mm lens, shallow depth of field, slight wide-angle charm",
        texture="soft surfaces, tactile props, subtle grain",
        realism="stylized realism, expressive but believable",
        camera_language={
            "push_in": "playful push-in",
            "pull_out": "pull-out for the punchline",
            "pan_left": "quick pan",
            "pan_right": "quick pan to a reaction",
            "tilt_up": "tilt up for scale",
            "orbit": "orbit for a hero moment",
            "macro": "macro on a funny detail",
            "handheld": "handheld for comedic energy",
        },
        character_rules="keep the same character design, proportions, and color accents across scenes",
        negative_prompt=(
            "no text, no logos, no watermarks, no deformed anatomy, no extra limbs, "
            "no collage, no low resolution"
        ),
        music_mood="playful, curious, bouncy, warm",
        sfx_palette=("whoosh", "impact", "riser", "sparkle", "sub_drop"),
    ),
    "cinematic_3d_nature": StylePreset(
        id="cinematic_3d_nature",
        name="Cinematic 3D Nature",
        visual_style=(
            "cinematic 3D nature documentary still, stylized realism, macro and wide nature detail, "
            "volumetric light, atmospheric depth"
        ),
        palette=(
            ("forest green", "#123B2A", "background"),
            ("moss", "#4F7A4A", "midground"),
            ("sun gold", "#F2C14E", "accent"),
            ("mist white", "#E8F0E8", "highlight"),
        ),
        lighting="soft morning light, god rays through foliage, gentle rim light",
        lens="35mm lens, shallow depth of field, macro detail when needed",
        texture="realistic leaves, water, stone, fine grain",
        realism="stylized realism, believable natural forms",
        camera_language={
            "push_in": "slow push-in on the phenomenon",
            "pull_out": "pull-out to reveal the landscape",
            "pan_left": "lateral pan across the landscape",
            "pan_right": "pan to a detail",
            "tilt_up": "tilt up to the sky",
            "orbit": "gentle orbit around the subject",
            "macro": "macro on water/light",
            "handheld": "subtle handheld for weather",
        },
        character_rules="keep the same landscape/weather logic and light direction across scenes",
        negative_prompt=(
            "no text, no logos, no watermarks, no impossible weather, no collage, "
            "no low resolution, no generic stock-photo look"
        ),
        music_mood="calm, curious, building, awe",
        sfx_palette=("whoosh", "impact", "riser", "sparkle"),
    ),
    "cinematic_3d_default": StylePreset(
        id="cinematic_3d_default",
        name="Cinematic 3D Default",
        visual_style=(
            "cinematic 3D animated film still, stylized realism, detailed materials, "
            "volumetric lighting, atmospheric depth"
        ),
        palette=(
            ("deep teal", "#0B2E36", "background"),
            ("slate", "#38505A", "midground"),
            ("warm amber", "#F2A65A", "accent"),
            ("cream", "#F7EFE3", "highlight"),
        ),
        lighting="soft volumetric key light, gentle rim light, controlled contrast",
        lens="35mm lens, shallow depth of field, subject sharp",
        texture="subtle film grain, realistic micro-detail",
        realism="stylized realism, believable forms",
        camera_language={
            "push_in": "slow push-in",
            "pull_out": "pull-out",
            "pan_left": "lateral pan",
            "pan_right": "lateral pan",
            "tilt_up": "tilt up",
            "orbit": "gentle orbit",
            "macro": "macro detail",
            "handheld": "subtle handheld",
        },
        character_rules="keep the same subject design, palette, and lighting direction across scenes",
        negative_prompt=(
            "no text, no logos, no watermarks, no deformed anatomy, no collage, no low resolution"
        ),
        music_mood="curious, warm, building",
        sfx_palette=("whoosh", "impact", "riser", "sparkle"),
    ),
}


def build_style_bible(
    topic: str,
    archetype: str,
    *,
    series_character: str = "",
    reference_chain: bool = True,
    visual_style_override: str = "",
) -> dict[str, Any]:
    """Bangun style bible deterministik dari arketipe topik."""
    preset = _PRESETS.get(f"cinematic_3d_{archetype}", _PRESETS["cinematic_3d_default"])
    if visual_style_override:
        preset = StylePreset(
            **{**preset.__dict__, "visual_style": visual_style_override, "id": f"{preset.id}_custom"}
        )
    underwater = _is_underwater(topic)
    environment = (
        "consistent underwater environment: deep blue water, suspended particles, caustic light"
        if underwater
        else "consistent environment family across scenes: same location logic and light direction"
    )
    hero_object = _hero_object(topic)
    palette = [{"name": name, "hex": value, "role": role} for name, value, role in preset.palette]
    continuity = {
        "hero_object": hero_object,
        "series_character": series_character,
        "environment": environment,
        "anchors": [
            "same color palette",
            "same lighting direction",
            "same lens language",
            "same level of realism",
        ]
        + ([f"recurring character: {series_character}"] if series_character else [])
        + ([f"recurring subject: {hero_object}"] if hero_object else []),
        "reference_chain": bool(reference_chain),
        "reference_policy": (
            "generate a hero/reference frame first; pass it as reference for all later scene images"
            if reference_chain
            else "no reference chain (style drift possible)"
        ),
    }
    style_prefix = (
        f"{preset.visual_style}. {preset.lighting}. {preset.lens}. {preset.texture}. "
        f"Palette: {', '.join(item['name'] for item in palette)}. {preset.realism}."
    )
    return {
        "version": STYLE_VERSION,
        "id": preset.id,
        "name": preset.name,
        "topic": topic,
        "archetype": archetype,
        "visual_style": preset.visual_style,
        "style_prefix": style_prefix,
        "palette": palette,
        "lighting": preset.lighting,
        "lens": preset.lens,
        "texture": preset.texture,
        "realism": preset.realism,
        "camera_language": dict(preset.camera_language),
        "character_rules": preset.character_rules,
        "negative_prompt": preset.negative_prompt,
        "music_mood": preset.music_mood,
        "sfx_palette": list(preset.sfx_palette),
        "continuity": continuity,
        "aspect_ratio": "9:16",
        "typography": {
            "font": "Segoe UI",
            "caption_color": "#F5B942",
            "hook_color": "#FFFFFF",
            "box_color": "#000000",
        },
    }


def style_prompt(style_bible: dict[str, Any], scene: dict[str, Any]) -> str:
    """Susun prompt gambar yang konsisten dari style bible + scene spec."""
    subject = str(scene.get("subject") or scene.get("visual_seed") or "").strip()
    action = str(scene.get("action") or "").strip()
    location = str(scene.get("location") or "").strip()
    composition = str(scene.get("composition") or "cinematic vertical composition").strip()
    camera = str(scene.get("camera_move") or "slow push-in").replace("_", " ")
    lighting = str(scene.get("lighting") or style_bible.get("lighting") or "").strip()
    palette = ", ".join(item["name"] for item in style_bible.get("palette", []))
    continuity = style_bible.get("continuity") or {}
    anchors = "; ".join(continuity.get("anchors") or [])
    parts = [
        style_bible.get("style_prefix", ""),
        f"Subject: {subject}." if subject else "",
        f"Action: {action}." if action else "",
        f"Location: {location}." if location else "",
        f"Composition: {composition}.",
        f"Camera: {camera}.",
        f"Lighting: {lighting}." if lighting else "",
        f"Palette: {palette}.",
        f"Continuity: {anchors}." if anchors else "",
        f"Negative prompt: {style_bible.get('negative_prompt', '')}.",
    ]
    return " ".join(part for part in parts if part).strip()


def style_consistency_report(
    style_bible: dict[str, Any],
    image_paths: Iterable[Path],
) -> dict[str, Any]:
    """Heuristik konsistensi palet + rasio. Bukan penilaian estetika objektif."""
    images = [Path(path) for path in image_paths if Path(path).exists()]
    if not images:
        return {"score": 0, "images": 0, "issues": ["tidak ada gambar untuk diperiksa"], "palette": []}
    target = [_hex_to_rgb(item["hex"]) for item in style_bible.get("palette", [])]
    averages: list[tuple[int, int, int]] = []
    issues: list[str] = []
    for path in images:
        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                if image.height <= image.width:
                    issues.append(f"{path.name}: bukan rasio vertikal ({image.width}x{image.height})")
                stat = ImageStat.Stat(image.resize((64, 64)))
                averages.append(tuple(int(value) for value in stat.mean[:3]))
        except Exception as exc:  # gambar rusak tidak boleh menggagalkan laporan
            issues.append(f"{path.name}: gagal dibaca ({exc})")
    if not averages:
        return {"score": 0, "images": 0, "issues": issues, "palette": []}
    # jarak rata-rata ke palet terdekat (0-255) -> skor 0-100
    distances: list[float] = []
    for average in averages:
        nearest = min(_rgb_distance(average, color) for color in target) if target else 0.0
        distances.append(nearest)
    mean_distance = sum(distances) / len(distances)
    score = max(0, min(100, round(100 - mean_distance / 255 * 100)))
    if mean_distance > 120:
        issues.append(f"jarak palet rata-rata tinggi ({mean_distance:.0f}/255)")
    return {
        "score": score,
        "images": len(averages),
        "mean_palette_distance": round(mean_distance, 1),
        "issues": issues,
        "palette": style_bible.get("palette", []),
    }


def _is_underwater(topic: str) -> bool:
    lowered = topic.lower()
    return any(word in lowered for word in ("gurita", "octopus", "laut", "samudra", "ikan", "paus", "ubur", "terumbu"))


def _hero_object(topic: str) -> str:
    lowered = topic.lower()
    known = {
        "gurita": "the same octopus individual with consistent mantle markings",
        "octopus": "the same octopus individual with consistent mantle markings",
        "lebah": "the same bee with consistent stripe pattern",
        "paus": "the same whale with consistent markings",
        "pesawat": "the same aircraft with consistent livery (no brand logos)",
        "madu": "the same honey jar and golden honey texture",
        "pelangi": "the same landscape and rainbow light logic",
    }
    for key, value in known.items():
        if key in lowered:
            return value
    return "the same main subject design across scenes"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def palette_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Jarak palet sederhana (0-441) untuk laporan evaluasi."""
    return round(_rgb_distance(a, b), 1)


def dominant_colors(path: Path, *, count: int = 3) -> list[str]:
    """Ambil warna dominan gambar (hex) untuk laporan konsistensi."""
    with Image.open(path) as image:
        image = image.convert("RGB").resize((96, 96))
        quantized = image.quantize(colors=max(1, count))
        palette = quantized.getpalette() or []
        counts = sorted(quantized.getcolors() or [], reverse=True)
        result: list[str] = []
        for _, index in counts[:count]:
            rgb = palette[index * 3 : index * 3 + 3]
            if len(rgb) == 3:
                result.append("#{:02X}{:02X}{:02X}".format(*rgb))
        return result


def hue_saturation(path: Path) -> tuple[float, float]:
    """Rata-rata hue/saturation (0-1) untuk cek konsistensi warna."""
    with Image.open(path) as image:
        image = image.convert("RGB").resize((64, 64))
        stat = ImageStat.Stat(image)
        r, g, b = (value / 255 for value in stat.mean[:3])
        h, s, _ = colorsys.rgb_to_hsv(r, g, b)
        return round(h, 3), round(s, 3)
