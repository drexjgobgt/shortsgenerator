"""Renderer sinematik 2.5D: camera moves terencana, color grade, grain, transisi.

Batas yang diakui:
- Ini BUKAN animasi 3D kompleks. Aset tetap gambar/klip 2D yang digerakkan
  kamera virtual (push-in, pull-out, pan, tilt, orbit semu) + grading.
- Tanpa video AI, gerakan tidak bisa menghasilkan parallax objek nyata.
  Yang tersedia: gerak kamera, kedalaman palsu (blur/zoom berbeda tidak dipakai
  karena tanpa segmentasi subjek), grain, vignette, light grade.
- Semua gerakan punya `camera_reason` dari storyboard; tidak ada zoom acak.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

MOTION_AMPLITUDE = {"low": 0.06, "medium": 0.10, "high": 0.14}
TRANSITION_SECONDS = 0.24

# Nama transisi xfade yang valid + fallback aman.
XFADE_MAP = {
    "cut": "fade",
    "fade": "fade",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "circleopen": "circleopen",
    "zoomin": "zoomin",
    "wipeup": "wipeup",
}


def plan_render_shots(
    scenes: list[dict[str, Any]],
    *,
    max_shot_s: float,
    transitions_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Ambil shot plan dari storyboard; fallback pecah scene bila belum ada."""
    shots: list[dict[str, Any]] = []
    index = 0
    for scene in scenes:
        asset = (scene.get("asset") or {}).get("path")
        start = float(scene.get("start_s") or 0.0)
        duration = float(scene.get("duration_s") or 0.0)
        if not asset or duration <= 0:
            continue
        planned = scene.get("shot_plan") or []
        if planned:
            for item in planned:
                shot_duration = float(item.get("duration_s") or 0.0)
                if shot_duration <= 0:
                    continue
                shots.append(
                    {
                        "index": index,
                        "scene_index": int(scene.get("index", index)),
                        "scene_id": scene.get("id"),
                        "start_s": round(float(item.get("start_s") or start), 3),
                        "duration_s": round(shot_duration, 3),
                        "asset": asset,
                        "camera_move": str(item.get("camera_move") or "push_in"),
                        "camera_reason": str(item.get("camera_reason") or ""),
                        "speed": str(item.get("speed") or "medium"),
                        "transition_in": str(item.get("transition_in") or "cut"),
                        "transition_out": str(item.get("transition_out") or "cut"),
                        "sfx_cue": str(item.get("sfx_cue") or "none"),
                        "purpose": str(item.get("purpose") or ""),
                        "overlay": item.get("overlay"),
                    }
                )
                index += 1
            continue
        parts = max(1, int(math.ceil(duration / max_shot_s - 1e-6)))
        part_duration = duration / parts
        cursor = start
        for part in range(parts):
            shots.append(
                {
                    "index": index,
                    "scene_index": int(scene.get("index", index)),
                    "scene_id": scene.get("id"),
                    "start_s": round(cursor, 3),
                    "duration_s": round(part_duration, 3),
                    "asset": asset,
                    "camera_move": "push_in" if index % 2 == 0 else "pull_out",
                    "camera_reason": "fallback: pergantian visual agar ritme tetap",
                    "speed": "medium",
                    "transition_in": "cut" if part else str(scene.get("transition_in") or "cut"),
                    "transition_out": "cut",
                    "sfx_cue": "none",
                    "purpose": scene.get("narrative_purpose") or "",
                    "overlay": None,
                }
            )
            index += 1
            cursor += part_duration
    if not transitions_enabled:
        for shot in shots:
            shot["transition_in"] = "cut"
    return shots


def camera_filter(
    move: str,
    *,
    width: int,
    height: int,
    fps: int,
    frames: int,
    intensity: str = "medium",
    is_image: bool = True,
) -> str:
    """Filter kamera untuk satu shot.

    Untuk gambar: zoompan dengan easing (pow/sin), bukan gerak linear.
    Untuk video: crop bergerak (tanpa zoompan agar tidak menduplikasi frame).
    """
    amplitude = MOTION_AMPLITUDE.get(intensity, 0.10)
    frames = max(2, frames)
    move = (move or "push_in").lower()
    if not is_image:
        # video: sedikit overscan + crop bergerak
        overscan = 1.0 + amplitude
        base = (
            f"scale={int(width * overscan)}:{int(height * overscan)}:"
            "force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:x='(iw-ow)/2':y='(ih-oh)/2'"
        )
        if move in ("pan_left", "pan_right"):
            duration_s = frames / float(fps) if fps else 1.0
            direction = (
                f"1-t/{duration_s:.3f}" if move == "pan_left" else f"t/{duration_s:.3f}"
            )
            base = (
                f"scale={int(width * (1 + amplitude * 2))}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(iw-ow)*({direction})':y='(ih-oh)/2'"
            )
        return f"{base},fps={fps}"
    # gambar: upscale dulu agar zoompan tidak pecah
    pre = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},setsar=1"
    )
    center_x = "(iw-iw/zoom)/2"
    center_y = "(ih-ih/zoom)/2"
    if move == "push_in":
        z = f"1+{amplitude}*pow(on/{frames},1.5)"
        x, y = center_x, center_y
    elif move == "pull_out":
        z = f"1+{amplitude}*(1-pow(on/{frames},1.5))"
        x, y = center_x, center_y
    elif move == "pan_left":
        z = f"1+{amplitude}"
        x = f"(iw-iw/zoom)*(1-on/{frames})"
        y = center_y
    elif move == "pan_right":
        z = f"1+{amplitude}"
        x = f"(iw-iw/zoom)*(on/{frames})"
        y = center_y
    elif move == "tilt_up":
        z = f"1+{amplitude}"
        x = center_x
        y = f"(ih-ih/zoom)*(1-on/{frames})"
    elif move == "macro":
        z = f"1.22+{amplitude * 0.5:.3f}*pow(on/{frames},1.2)"
        x, y = center_x, center_y
    elif move == "orbit":
        z = f"1+{amplitude * 1.2:.3f}"
        x = f"(iw-iw/zoom)/2+(iw-iw/zoom)*0.16*sin(2*PI*on/{frames})"
        y = f"(ih-ih/zoom)/2+(ih-ih/zoom)*0.06*cos(2*PI*on/{frames})"
    elif move == "handheld":
        z = f"1+{amplitude * 0.9:.3f}"
        x = f"(iw-iw/zoom)/2+(iw-iw/zoom)*0.03*sin(4*PI*on/{frames})"
        y = f"(ih-ih/zoom)/2+(ih-ih/zoom)*0.02*cos(6*PI*on/{frames})"
    else:
        z = f"1+{amplitude}*pow(on/{frames},1.5)"
        x, y = center_x, center_y
    zoompan = (
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}"
    )
    return f"{pre},{zoompan}"


def grade_filter(intensity: str = "medium") -> str:
    """Color grade + vignette + grain (ringan, tidak mengubah warna kulit ekstrem)."""
    grain = {"low": 3, "medium": 5, "high": 7}.get(intensity, 5)
    return (
        "eq=contrast=1.06:saturation=1.08:brightness=-0.01,"
        "colorbalance=rs=-0.02:gs=0.0:bs=0.03,"
        "vignette=PI/5,"
        f"noise=alls={grain}:allf=t+u,"
        "format=yuv420p"
    )


def build_xfade_filter(shots: list[dict[str, Any]], *, duration_s: float = TRANSITION_SECONDS) -> tuple[str, str]:
    """Bangun filter_complex xfade berantai. Returns (filter, label output)."""
    if len(shots) < 2:
        return "", "[0:v]"
    parts: list[str] = []
    for index in range(len(shots)):
        parts.append(f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]")
    current = "[v0]"
    cumulative = float(shots[0]["duration_s"])
    for index in range(1, len(shots)):
        transition = XFADE_MAP.get(str(shots[index].get("transition_in") or "fade"), "fade")
        offset = max(0.0, cumulative - duration_s)
        label = f"[x{index}]"
        parts.append(
            f"{current}[v{index}]xfade=transition={transition}:duration={duration_s:.3f}:offset={offset:.3f}{label}"
        )
        current = label
        cumulative += float(shots[index]["duration_s"])
    return ";".join(parts), current


def sfx_cues_for_shots(shots: list[dict[str, Any]]) -> list[tuple[float, str]]:
    """Isyarat SFX naratif per shot + whoosh pada transisi non-cut."""
    cues: list[tuple[float, str]] = []
    for index, shot in enumerate(shots):
        cue = str(shot.get("sfx_cue") or "none").lower()
        start = float(shot.get("start_s") or 0.0)
        if cue not in ("none", "whoosh") and index > 0:
            cues.append((max(0.0, start - 0.05), cue))
        if index > 0 and str(shot.get("transition_in") or "cut") != "cut":
            cues.append((max(0.0, start - TRANSITION_SECONDS / 2), "whoosh"))
    return cues


def transition_offsets(shots: list[dict[str, Any]], *, duration_s: float = TRANSITION_SECONDS) -> list[float]:
    """Titik tengah transisi untuk laporan/SFX."""
    offsets: list[float] = []
    cumulative = 0.0
    for shot in shots[:-1]:
        cumulative += float(shot.get("duration_s") or 0.0)
        offsets.append(round(cumulative - duration_s / 2, 3))
    return offsets
