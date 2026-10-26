"""Storyboard terstruktur: scene spec lengkap + shot plan + kontinuitas visual.

Setiap scene wajib punya:
id, durasi, span naskah, tujuan naratif, subjek, aksi, lokasi, jenis visual,
komposisi, camera move + alasan, pencahayaan, palet, transisi, voice direction,
emphasis subtitle, SFX, dan referensi kontinuitas.

Shot plan memecah scene mengikuti durasi narasi (bukan interval tetap), dengan
camera move yang punya alasan. Renderer memakai shot plan ini.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from .style_bible import style_prompt

SCENE_PAUSE_S = 0.25
WORDS_PER_SECOND = 2.0
MAX_SHOT_S_DEFAULT = 4.0

ROLE_COMPOSITION = {
    "hook": "centered hero composition, subject large in frame, negative space at top for hook text",
    "context": "wide establishing shot, subject in the lower third, room for context text",
    "escalation": "medium shot, subject moving into frame, dynamic negative space",
    "evidence": "macro or close-up, key detail centered, clean background",
    "twist": "dynamic diagonal composition, subject off-center, strong contrast",
    "payoff": "hero low-angle composition, subject dominant, warm rim light",
    "closing": "centered composition with breathing room for the closing line",
}

ROLE_CAMERA = {
    "hook": ("push_in", "pan_right", "macro"),
    "context": ("pan_left", "push_in", "orbit"),
    "escalation": ("push_in", "orbit", "tilt_up"),
    "evidence": ("macro", "pull_out", "pan_right"),
    "twist": ("handheld", "push_in", "pan_left"),
    "payoff": ("push_in", "orbit", "pull_out"),
    "closing": ("pull_out", "push_in", "macro"),
}

ROLE_SPEED = {
    "hook": "slow",
    "context": "medium",
    "escalation": "medium",
    "evidence": "slow",
    "twist": "fast",
    "payoff": "slow",
    "closing": "slow",
}

ROLE_LIGHT = {
    "hook": "dramatic rim light, subject separated from background",
    "context": "soft ambient light, environment readable",
    "escalation": "directional key light, increasing contrast",
    "evidence": "focused practical light on the detail",
    "twist": "hard side light, deep shadow, high contrast",
    "payoff": "warm backlight and soft fill, heroic",
    "closing": "soft even light, calm",
}

ROLE_FOCUS = {
    "hook": "center",
    "context": "left",
    "escalation": "center",
    "evidence": "center",
    "twist": "right",
    "payoff": "center",
    "closing": "center",
}

_GENERIC_STOCK_WORDS = {
    "sky", "blue", "light", "sun", "sunset", "nature", "background", "landscape",
    "sea", "ocean", "water", "cloud", "clouds", "beautiful", "view", "horizon",
    "atmosphere", "air", "video", "footage", "cinematic", "concept", "abstract",
    "mysterious", "subtle", "glow", "glowing", "illustration", "visualization",
    "metaphor", "symbolic", "stylized", "artistic", "editorial", "detailed",
    "diagram", "cross", "section", "thin",
}


def build_storyboard(
    script: dict[str, Any],
    style_bible: dict[str, Any],
    config: Config,
    *,
    stock_available: bool = False,
    image_status: str = "not_configured",
    max_shot_s: Optional[float] = None,
) -> dict[str, Any]:
    """Bangun storyboard lengkap dari script.beats + style bible."""
    beats = script.get("beats") or []
    if not beats:
        raise RuntimeError("script.json tidak memuat beats")
    max_shot_s = max_shot_s or float(getattr(config, "max_scene_seconds", MAX_SHOT_S_DEFAULT))
    image_available = image_status == "available"
    ai_budget = int(getattr(config, "max_ai_images_per_job", 8))
    stock_policy = str(getattr(config, "stock_policy", "ai_first") or "ai_first").lower()
    max_stock = int(getattr(config, "stock_max_clips_per_job", 2))
    stock_used = 0
    ai_used = 0

    scenes: list[dict[str, Any]] = []
    cursor = 0.0
    for position, beat in enumerate(beats):
        words = int(beat.get("word_count") or len(str(beat.get("narration") or "").split()))
        duration = max(1.2, round(words / WORDS_PER_SECOND, 2))
        visual_seed = str(beat.get("visual_seed") or "").strip()
        stock_query = _stock_query(beat)
        strategy, reason = _choose_strategy(
            beat,
            visual_seed=visual_seed,
            stock_query=stock_query,
            stock_available=stock_available,
            image_available=image_available,
            ai_used=ai_used,
            ai_budget=ai_budget,
            stock_used=stock_used,
            stock_policy=stock_policy,
            max_stock=max_stock,
        )
        if strategy == "ai_image":
            ai_used += 1
        elif strategy == "stock_video":
            stock_used += 1
        visual_type = _visual_type(strategy, beat)
        scene = {
            "index": position,
            "id": f"S{position + 1:02d}",
            "kind": "hook" if beat.get("role") == "hook" else "closing" if beat.get("role") == "closing" else "body",
            "role": beat.get("role", "escalation"),
            "narration": beat.get("narration", ""),
            "word_count": words,
            "on_screen_text": beat.get("on_screen_text") or _first_short(beat.get("narration", "")),
            "visual_seed": visual_seed,
            "subject": beat.get("subject") or _subject_from_seed(visual_seed),
            "action": beat.get("action") or _action_from_seed(visual_seed),
            "location": beat.get("location") or _location_from_seed(visual_seed, style_bible),
            "composition": beat.get("composition") or ROLE_COMPOSITION.get(beat.get("role"), ""),
            "visual_strategy": strategy,
            "visual_type": visual_type,
            "strategy_reason": reason,
            "visual_prompt": "",
            "stock_query": stock_query,
            "camera_move": "",
            "camera_reason": "",
            "lighting": f"{style_bible.get('lighting', '')}. {ROLE_LIGHT.get(beat.get('role'), '')}".strip(),
            "palette": [item["name"] for item in style_bible.get("palette", [])],
            "transition_in": beat.get("transition") or "cut",
            "transition_out": "cut",
            "voice_direction": beat.get("voice_direction") or "",
            "emotion": beat.get("emotion") or "conversational",
            "intensity": int(beat.get("intensity") or 3),
            "subtitle_emphasis": list(beat.get("emphasis") or []),
            "sfx_cue": beat.get("sfx_cue") or "none",
            "continuity_ref": "hero_reference" if position == 0 else f"S{position:02d}",
            "narrative_purpose": beat.get("purpose") or "",
            "claim_type": beat.get("claim_type") or "theory",
            "fact_refs": list(beat.get("fact_refs") or []),
            "relevance_expectation": _relevance_expectation(beat),
            "start_s": round(cursor, 2),
            "duration_s": duration,
            "end_s": round(cursor + duration, 2),
            "asset": None,
        }
        scene["visual_prompt"] = style_prompt(style_bible, scene)
        scene["shot_plan"] = _plan_shots(
            scene,
            beat,
            style_bible,
            max_shot_s=max_shot_s,
            scene_start=cursor,
        )
        if scene["shot_plan"]:
            scene["camera_move"] = scene["shot_plan"][0]["camera_move"]
            scene["camera_reason"] = scene["shot_plan"][0]["camera_reason"]
            scene["transition_out"] = scene["shot_plan"][-1]["transition_out"]
        scenes.append(scene)
        cursor = round(cursor + duration + SCENE_PAUSE_S, 2)

    storyboard = {
        "version": 2,
        "topic": script.get("topic") or style_bible.get("topic", ""),
        "title": script.get("title", ""),
        "logline": script.get("logline", ""),
        "style_bible": style_bible,
        "scenes": scenes,
        "timeline": {
            "scene_count": len(scenes),
            "narration_seconds": round(sum(scene["duration_s"] for scene in scenes), 2),
            "pause_seconds": round(SCENE_PAUSE_S * max(0, len(scenes) - 1), 2),
            "total_seconds": round(cursor - (SCENE_PAUSE_S if scenes else 0), 2),
            "measured": False,
        },
        "visual_plan": {
            "stock_configured": stock_available,
            "image_status": image_status,
            "ai_images_planned": ai_used,
            "ai_images_budget": ai_budget,
            "stock_clips_planned": stock_used,
            "stock_policy": stock_policy,
            "strategies": sorted({scene["visual_strategy"] for scene in scenes}),
            "style_id": style_bible.get("id"),
            "reference_chain": bool((style_bible.get("continuity") or {}).get("reference_chain")),
        },
        "created_at": _utcnow(),
    }
    return storyboard


def retime_storyboard(
    storyboard: dict[str, Any],
    measured_scenes: Iterable[dict[str, Any]],
    *,
    max_shot_s: Optional[float] = None,
) -> dict[str, Any]:
    """Perbarui timing scene + shot plan setelah durasi TTS nyata diukur.

    Bila `max_shot_s` diberikan, shot dipecah ulang agar tidak ada shot yang
    melebihi batas (durasi TTS nyata bisa lebih panjang dari estimasi).
    """
    measured = {int(item["scene_index"]): item for item in measured_scenes}
    for scene in storyboard.get("scenes") or []:
        item = measured.get(int(scene.get("index", -1)))
        if not item:
            continue
        old_duration = float(scene.get("duration_s") or 0.0) or 1.0
        new_duration = float(item.get("duration_s") or old_duration)
        scene["start_s"] = round(float(item.get("start_s") or scene.get("start_s") or 0.0), 3)
        scene["duration_s"] = round(new_duration, 3)
        scene["end_s"] = round(scene["start_s"] + new_duration, 3)
        shots = scene.get("shot_plan") or []
        if not shots:
            continue
        moves = [str(shot.get("camera_move") or "push_in") for shot in shots]
        reasons = [str(shot.get("camera_reason") or "") for shot in shots]
        speeds = [str(shot.get("speed") or "medium") for shot in shots]
        focus = [str(shot.get("focus") or "center") for shot in shots]
        overlays = [shot.get("overlay") for shot in shots]
        sfx = [str(shot.get("sfx_cue") or "none") for shot in shots]
        parts = max(1, int(math.ceil(new_duration / max_shot_s - 1e-6))) if max_shot_s else len(shots)
        rebuilt: list[dict[str, Any]] = []
        cursor = scene["start_s"]
        for part in range(parts):
            shot_duration = new_duration / parts
            if part == parts - 1:
                shot_duration = scene["end_s"] - cursor
            rebuilt.append(
                {
                    "index": part,
                    "start_s": round(cursor, 3),
                    "duration_s": round(max(0.2, shot_duration), 3),
                    "end_s": round(cursor + max(0.2, shot_duration), 3),
                    "camera_move": moves[part % len(moves)],
                    "camera_reason": reasons[part % len(reasons)] or f"{moves[part % len(moves)]} mengikuti alur scene",
                    "speed": speeds[part % len(speeds)],
                    "focus": focus[part % len(focus)],
                    "transition_in": (
                        str(shots[0].get("transition_in") or "cut")
                        if part == 0
                        else ("cut" if moves[part % len(moves)] in ("handheld", "macro") else "smoothleft")
                    ),
                    "transition_out": "cut" if part < parts - 1 else str(shots[-1].get("transition_out") or "fade"),
                    "overlay": overlays[part % len(overlays)],
                    "sfx_cue": sfx[part % len(sfx)] if part == 0 else ("whoosh" if part > 0 else "none"),
                    "purpose": shots[part % len(shots)].get("purpose") or scene.get("narrative_purpose") or "",
                    "asset": scene.get("asset"),
                }
            )
            cursor = rebuilt[-1]["end_s"]
        scene["shot_plan"] = rebuilt
        scene["camera_move"] = rebuilt[0]["camera_move"]
        scene["camera_reason"] = rebuilt[0]["camera_reason"]
    timeline = storyboard.get("timeline") or {}
    timeline.update(
        {
            "scene_count": len(storyboard.get("scenes") or []),
            "narration_seconds": round(sum(float(s.get("duration_s") or 0) for s in storyboard.get("scenes") or []), 2),
            "pause_seconds": 0.0,
            "total_seconds": round(
                max((float(s.get("end_s") or 0) for s in storyboard.get("scenes") or []), default=0.0), 2
            ),
            "measured": True,
        }
    )
    storyboard["timeline"] = timeline
    return storyboard


def render_storyboard_preview(
    storyboard: dict[str, Any],
    out_path: Path,
    *,
    title: str = "",
) -> Path:
    """Contact sheet storyboard (sebelum/sesudah aset) untuk pemeriksaan manusia."""
    scenes = storyboard.get("scenes") or []
    width = 1080
    header = 190
    columns = 2
    card_w = 500
    card_h = 300
    gap = 20
    rows = max(1, math.ceil(len(scenes) / columns))
    height = header + rows * (card_h + gap) + 60
    image = Image.new("RGB", (width, height), "#0B1116")
    draw = ImageDraw.Draw(image)
    style = storyboard.get("style_bible") or {}
    palette = style.get("palette") or []
    accent = palette[2]["hex"] if len(palette) > 2 else "#F2A65A"
    muted = "#9FB3C8"
    title_font = _font(40)
    body_font = _font(22)
    small_font = _font(17)
    tiny_font = _font(14)
    draw.text((40, 32), "STORYBOARD PREVIEW", font=title_font, fill="#FFFFFF")
    draw.text((40, 84), (title or storyboard.get("title") or "")[:80], font=body_font, fill=accent)
    draw.text(
        (40, 120),
        f"{style.get('name', '-')} | {len(scenes)} scene | "
        f"{storyboard.get('timeline', {}).get('total_seconds', 0)} dtk | "
        "preview sebelum render final",
        font=small_font,
        fill=muted,
    )
    draw.text(
        (40, 148),
        "Palet: " + "  ".join(f"{item['name']} {item['hex']}" for item in palette),
        font=tiny_font,
        fill=muted,
    )
    for index, scene in enumerate(scenes):
        row, column = divmod(index, columns)
        x = 30 + column * (card_w + gap)
        y = header + row * (card_h + gap)
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=16, fill="#121C24", outline="#22313D")
        draw.rectangle((x, y, x + 8, y + card_h), fill=accent)
        draw.text((x + 22, y + 14), f"{scene.get('id', index + 1)}  {scene.get('role', '')}".upper(),
                  font=small_font, fill=accent)
        draw.text(
            (x + 22, y + 42),
            f"{scene.get('start_s', 0):.1f}-{scene.get('end_s', 0):.1f}s | "
            f"{scene.get('duration_s', 0):.1f}s | {scene.get('visual_type', '')}",
            font=tiny_font, fill=muted,
        )
        _draw_wrapped(draw, scene.get("narration", ""), x + 22, y + 66, card_w - 44, body_font, "#FFFFFF", max_lines=3)
        _draw_wrapped(
            draw,
            f"Visual: {scene.get('visual_seed', '')}",
            x + 22, y + 140, card_w - 44, tiny_font, muted, max_lines=2,
        )
        camera = scene.get("shot_plan", [{}])[0] if scene.get("shot_plan") else {}
        _draw_wrapped(
            draw,
            f"Kamera: {camera.get('camera_move', '-')} ({camera.get('speed', '-')}) | "
            f"Transisi: {scene.get('transition_in', '-')}→{scene.get('transition_out', '-')} | "
            f"SFX: {scene.get('sfx_cue', '-')}",
            x + 22, y + 206, card_w - 44, tiny_font, muted, max_lines=2,
        )
        _draw_wrapped(
            draw,
            f"Emosi: {scene.get('emotion', '-')} {scene.get('intensity', '')}/5 | "
            f"Klaim: {scene.get('claim_type', '-')} | Shot: {len(scene.get('shot_plan') or [])}",
            x + 22, y + 254, card_w - 44, tiny_font, muted, max_lines=1,
        )
    draw.text(
        (40, height - 42),
        "Catatan: ini preview storyboard, bukan video final. Skor relevansi ada di relevance.json.",
        font=tiny_font, fill=muted,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return out_path


def render_storyboard_markdown(storyboard: dict[str, Any]) -> str:
    style = storyboard.get("style_bible") or {}
    lines = [
        f"# Storyboard: {storyboard.get('title') or storyboard.get('topic')}",
        "",
        f"**Style bible:** {style.get('name', '-')} (`{style.get('id', '-')}`)",
        f"**Visual style:** {style.get('visual_style', '-')}",
        f"**Palet:** " + ", ".join(f"{item['name']} ({item['hex']})" for item in style.get("palette", [])),
        f"**Lensa/lighting:** {style.get('lens', '-')} | {style.get('lighting', '-')}",
        f"**Kontinuitas:** " + "; ".join((style.get("continuity") or {}).get("anchors", [])),
        "",
        f"Total {storyboard['timeline']['scene_count']} scene, "
        f"{storyboard['timeline']['total_seconds']} dtk.",
        "",
        "| # | peran | waktu | visual | subjek/aksi | kamera | transisi | emosi | klaim |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for scene in storyboard.get("scenes") or []:
        camera = (scene.get("shot_plan") or [{}])[0]
        lines.append(
            f"| {scene['id']} | {scene['role']} | {scene['start_s']:.1f}-{scene['end_s']:.1f}s | "
            f"{scene['visual_type']} | {scene['subject'][:36]} / {scene['action'][:36]} | "
            f"{camera.get('camera_move', '-')} ({camera.get('speed', '-')}) | "
            f"{scene['transition_in']}→{scene['transition_out']} | {scene['emotion']} {scene['intensity']}/5 | "
            f"{scene['claim_type']} |"
        )
    lines += ["", "## Shot plan"]
    for scene in storyboard.get("scenes") or []:
        lines.append(f"### {scene['id']} — {scene['role']} ({scene['duration_s']}s)")
        lines.append(f"- Tujuan: {scene['narrative_purpose']}")
        lines.append(f"- Ekspektasi relevansi: {scene['relevance_expectation']}")
        lines.append(f"- Prompt gambar: `{scene['visual_prompt'][:500]}`")
        for shot in scene.get("shot_plan") or []:
            lines.append(
                f"  - {shot['start_s']:.2f}-{shot['end_s']:.2f}s | {shot['camera_move']} "
                f"({shot['speed']}) | alasan: {shot['camera_reason']} | "
                f"transisi {shot['transition_in']}→{shot['transition_out']} | SFX {shot['sfx_cue']}"
            )
    return "\n".join(lines) + "\n"


def _choose_strategy(
    beat: dict[str, Any],
    *,
    visual_seed: str,
    stock_query: str,
    stock_available: bool,
    image_available: bool,
    ai_used: int,
    ai_budget: int,
    stock_used: int,
    stock_policy: str,
    max_stock: int,
) -> tuple[str, str]:
    role = beat.get("role", "escalation")
    if (
        stock_available
        and stock_policy != "off"
        and stock_used < max_stock
        and role in ("context", "evidence")
        and _strong_stock_query(stock_query)
    ):
        return "stock_video", f"stok relevan untuk peran {role} (kueri spesifik: '{stock_query}')"
    if image_available and ai_used < ai_budget:
        return "ai_image", f"gambar AI untuk konsistensi sinematik (anggaran {ai_used + 1}/{ai_budget})"
    if image_available and ai_used >= ai_budget:
        return "procedural", "anggaran gambar AI per job sudah tercapai"
    if not image_available:
        return "procedural", "image provider tidak tersedia (fallback grafis lokal)"
    return "procedural", "tidak ada aset visual yang cocok"


def _visual_type(strategy: str, beat: dict[str, Any]) -> str:
    if strategy == "stock_video":
        return "stock_video"
    if strategy == "ai_image":
        return "ai_image"
    if beat.get("role") in ("evidence", "twist") or beat.get("emphasis"):
        return "motion_graphic"
    return "procedural"


def _plan_shots(
    scene: dict[str, Any],
    beat: dict[str, Any],
    style_bible: dict[str, Any],
    *,
    max_shot_s: float,
    scene_start: float,
) -> list[dict[str, Any]]:
    duration = float(scene.get("duration_s") or 1.0)
    parts = max(1, int(math.ceil(duration / max_shot_s - 1e-6)))
    role = str(beat.get("role") or "escalation")
    moves = ROLE_CAMERA.get(role, ("push_in", "pan_right", "macro"))
    camera_language = style_bible.get("camera_language") or {}
    shots: list[dict[str, Any]] = []
    cursor = scene_start
    for part in range(parts):
        shot_duration = duration / parts
        if part == parts - 1:
            shot_duration = scene_start + duration - cursor
        move = moves[part % len(moves)]
        transition_in = (
            str(beat.get("transition") or "cut") if part == 0
            else "cut" if move in ("handheld", "macro") else "smoothleft"
        )
        transition_out = "cut" if part < parts - 1 else "fade"
        shots.append(
            {
                "index": len(shots),
                "start_s": round(cursor, 3),
                "duration_s": round(max(0.2, shot_duration), 3),
                "end_s": round(cursor + max(0.2, shot_duration), 3),
                "camera_move": move,
                "camera_reason": camera_language.get(move) or f"{move} untuk mendukung peran {role}",
                "speed": ROLE_SPEED.get(role, "medium"),
                "focus": ROLE_FOCUS.get(role, "center"),
                "transition_in": transition_in,
                "transition_out": transition_out,
                "overlay": _overlay_for_shot(scene, beat, part),
                "sfx_cue": beat.get("sfx_cue") if part == 0 else ("whoosh" if transition_in != "cut" else "none"),
                "purpose": beat.get("purpose") or role,
                "asset": scene.get("asset"),
            }
        )
        cursor += shot_duration
    return shots


def _overlay_for_shot(scene: dict[str, Any], beat: dict[str, Any], part: int) -> Optional[dict[str, Any]]:
    text = str(scene.get("on_screen_text") or "").strip()
    emphasis = list(beat.get("emphasis") or [])
    if part == 0 and text:
        return {"kind": "fact_chip", "text": text, "emphasis": emphasis, "position": "lower_left"}
    if part == 1 and emphasis:
        return {"kind": "keyword", "text": emphasis[0], "emphasis": emphasis, "position": "center"}
    if part == 2 and emphasis:
        return {"kind": "ring", "text": emphasis[-1], "emphasis": emphasis, "position": "center"}
    return None


def _stock_query(beat: dict[str, Any]) -> str:
    explicit = str(beat.get("stock_query") or "").strip()
    if explicit:
        return " ".join(explicit.split()[:4])
    seed = str(beat.get("visual_seed") or "").lower()
    words = [word.strip(".,;:!?()[]") for word in seed.split() if len(word) > 2]
    concrete = [word for word in words if word not in _GENERIC_STOCK_WORDS]
    return " ".join(concrete[:4])


def _strong_stock_query(query: str) -> bool:
    tokens = [token for token in query.lower().split() if len(token) > 2]
    return len(tokens) >= 2 and sum(1 for token in tokens if token not in _GENERIC_STOCK_WORDS) >= 2


def _subject_from_seed(seed: str) -> str:
    if not seed:
        return ""
    # ambil frasa sebelum kata preposisi pertama sebagai subjek kasar
    for marker in (" in ", " on ", " at ", " with ", " inside ", " under "):
        if marker in seed.lower():
            return seed[: seed.lower().index(marker)].strip()
    return seed[:80]


def _action_from_seed(seed: str) -> str:
    lowered = seed.lower()
    for marker in (" in ", " on ", " at ", " with ", " inside ", " under "):
        if marker in lowered:
            return seed[lowered.index(marker) + 1 :].strip()
    return ""


def _location_from_seed(seed: str, style_bible: dict[str, Any]) -> str:
    continuity = style_bible.get("continuity") or {}
    if continuity.get("environment"):
        return str(continuity["environment"])
    return "consistent cinematic environment"


def _relevance_expectation(beat: dict[str, Any]) -> str:
    seed = str(beat.get("visual_seed") or "").strip()
    narration = str(beat.get("narration") or "").strip()
    return f"gambar harus menunjukkan: {seed or narration[:80]}"


def _first_short(text: str, limit: int = 40) -> str:
    return " ".join(str(text).split()[:5])[:limit]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_width: int,
    font: Any,
    fill: str,
    *,
    max_lines: int = 3,
) -> None:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    for index, line in enumerate(lines):
        draw.text((x, y + index * (font.size + 4 if hasattr(font, "size") else 20)), line, font=font, fill=fill)


def _utcnow() -> str:
    from ..models import utcnow_iso

    return utcnow_iso()
