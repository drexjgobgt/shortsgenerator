"""Stage NASKAH: naskah Bahasa Indonesia orisinal + struktur blok untuk produksi.

Keluaran normalisasi (`script.json`) berisi `blocks` (hook/body/closing) yang
dipakai langsung oleh stage berikutnya (storyboard, TTS, subtitle, render),
sehingga tidak ada parsing ulang di hilir.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ...story.narrative import (
    MAX_BEATS,
    MAX_BEAT_WORDS,
    MAX_CLOSING_WORDS,
    MAX_HOOK_WORDS,
    MIN_BEATS,
    WORDS_PER_SECOND,
    build_narrative_prompt,
    classify_topic,
    normalize_narrative,
    select_hook_pattern,
)
from ...util.jsonparse import JsonParseError, extract_json_object
from ..orchestrator import StageContext

# Konstanta lama dipertahankan untuk kompatibilitas import test/pipeline.
MAX_NARRATION_WORDS = MAX_BEAT_WORDS
MIN_BLOCK_WORDS = 5
MIN_BLOCKS = MIN_BEATS

SCRIPT_PROMPT = """Prompt naskah lama sudah diganti oleh story.narrative.build_narrative_prompt.
Konstanta ini dipertahankan agar import lama tidak gagal."""


class ScriptStage:
    stage = "script"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        if ctx.providers.llm is None:
            raise RuntimeError("LLM tidak dikonfigurasi; stage naskah membutuhkannya")

        research = _load_research(ctx)
        facts = research.get("facts") or []
        if not facts:
            raise RuntimeError("research.json tidak memuat fakta; jalankan stage riset lebih dulu")

        facts_block = _render_facts(facts)
        archetype = classify_topic(ctx.job.topic)
        hook_pattern = select_hook_pattern(ctx.job.topic, archetype)
        prompt = build_narrative_prompt(
            topic=ctx.job.topic,
            facts=facts,
            target_duration=ctx.config.target_duration_s,
            min_beats=MIN_BEATS,
            max_beats=min(MAX_BEATS, max(MIN_BEATS, ctx.config.script_max_scenes + 2)),
            archetype=archetype,
            hook_pattern=hook_pattern,
            series_character=getattr(ctx.config, "series_character", "") or "",
        )
        # facts_block tetap dihitung untuk kompatibilitas log/diagnostik
        _ = facts_block

        text = self._chat(ctx, prompt)
        try:
            data = extract_json_object(text)
        except JsonParseError as first_error:
            # satu percobaan perbaikan dengan instruksi lebih tegas
            if self.logger:
                self.logger.warning(f"naskah bukan JSON valid, mencoba perbaikan sekali: {first_error}")
            repair_prompt = (
                "Keluaran sebelumnya bukan JSON valid. Ulangi dan balas HANYA JSON valid, "
                "tanpa penjelasan, tanpa markdown fence.\n\n" + prompt
            )
            text = self._chat(ctx, repair_prompt)
            try:
                data = extract_json_object(text)
            except JsonParseError as exc:
                raise RuntimeError(f"naskah tetap bukan JSON valid setelah perbaikan: {exc}") from exc

        script = normalize_narrative(
            data,
            facts,
            topic=ctx.job.topic,
            target_duration=ctx.config.target_duration_s,
            archetype=archetype,
            hook_pattern=hook_pattern,
        )
        script_path = ctx.artifact_path("script.json")
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("script.md")).write_text(render_script_markdown(script), encoding="utf-8")

        return {
            "script": str(script_path),
            "blocks": len(script["blocks"]),
            "beats": len(script["beats"]),
            "hook_pattern": script["narrative"]["hook_pattern"],
            "estimated_duration_s": script["stats"]["estimated_duration_s"],
            "word_count": script["stats"]["total_words"],
            "unknown_fact_refs": script["unknown_fact_refs"],
        }

    # -- internal -----------------------------------------------------

    def _chat(self, ctx: StageContext, prompt: str) -> str:
        result = ctx.providers.llm.chat(
            [
                {"role": "system", "content": "Penulis naskah video pendek yang disiplin pada fakta."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=2600,
        )
        ctx.repo.record_usage(
            ctx.job.id,
            provider=result.provider,
            model=result.model,
            kind="llm",
            units={
                "input_tokens": float(result.usage.get("input_tokens", result.usage.get("prompt_tokens", 0)) or 0),
                "output_tokens": float(result.usage.get("output_tokens", result.usage.get("completion_tokens", 0)) or 0),
            },
            meta={"stage": "script", "latency_ms": result.latency_ms},
        )
        return result.text

    def _normalize(self, data: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any]:
        return normalize_script(data, facts)


def normalize_script(
    data: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    topic: str = "",
    target_duration: int = 30,
) -> dict[str, Any]:
    """Kompatibilitas lama: normalisasi ke struktur baru (beats + blocks)."""
    archetype = classify_topic(topic)
    hook_pattern = select_hook_pattern(topic, archetype)
    return normalize_narrative(
        data,
        facts,
        topic=topic,
        target_duration=target_duration,
        archetype=archetype,
        hook_pattern=hook_pattern,
    )


# -- helper -----------------------------------------------------------------

def _load_research(ctx: StageContext) -> dict[str, Any]:
    record = ctx.repo.get_stage(ctx.job.id, "research")
    candidate: Optional[Path] = None
    if record and record.artifacts.get("research"):
        candidate = Path(record.artifacts["research"])
    if not candidate or not candidate.exists():
        fallback = ctx.artifacts_dir / "research.json"
        candidate = fallback if fallback.exists() else None
    if candidate is None:
        raise RuntimeError("artefak riset tidak ditemukan; jalankan stage riset lebih dulu")
    return json.loads(candidate.read_text(encoding="utf-8"))


def _render_facts(facts: list[dict[str, Any]]) -> str:
    lines = []
    for index, fact in enumerate(facts, start=1):
        confidence = fact.get("confidence", "unverified")
        lines.append(f"- F{index} [{confidence}] {fact.get('claim', '')}")
        if fact.get("quote"):
            lines.append(f"  detail: {fact['quote']}")
    return "\n".join(lines)


def _scene_visual(scenes: list[Any], index: int) -> str:
    if index < len(scenes) and isinstance(scenes[index], dict):
        return str(scenes[index].get("visual_prompt") or "")
    return ""


def _scene_stock_query(scenes: list[Any], index: int) -> str:
    if index < len(scenes) and isinstance(scenes[index], dict):
        return str(scenes[index].get("stock_query") or "")
    return ""


def _first_short(text: str, limit: int = 40) -> str:
    words = text.split()
    return " ".join(words[:6])[:limit]


def _render_markdown(script: dict[str, Any]) -> str:
    return render_script_markdown(script)


def render_script_markdown(script: dict[str, Any]) -> str:
    narrative = script.get("narrative") or {}
    lines = [
        f"# {script['title']}",
        "",
        f"**Hook:** {script['hook']}",
        "",
        f"**Pola hook:** {narrative.get('hook_pattern', '-')} | "
        f"**Arketipe:** {narrative.get('archetype', '-')}",
        "",
        f"**Closing:** {script['closing']}",
        "",
        f"Perkiraan durasi: {script['stats']['estimated_duration_s']} detik "
        f"({script['stats']['total_words']} kata, {script['stats']['beat_count']} beat)",
        "",
        "## Beat narasi",
        "",
        "| # | peran | emosi | klaim | narasi | visual seed | kamera/SFX |",
        "|---|---|---|---|---|---|---|",
    ]
    for beat in script.get("beats") or []:
        lines.append(
            f"| {beat['index'] + 1} | {beat['role']} | {beat['emotion']} ({beat['intensity']}/5) | "
            f"{beat['claim_type']} | {beat['narration']} | {beat['visual_seed'][:60]} | "
            f"{beat['transition']} / {beat['sfx_cue']} |"
        )
    lines += ["", "## Metadata", f"- Tags: {', '.join(script['tags']) or '-'}",
              f"- Fakta dirujuk: {', '.join(script['facts_used']) or '-'}",
              f"- Disclosure: {script['disclosure']}", "",
              "## Deskripsi", script["description"] or "-"]
    if script.get("sources"):
        lines += ["", "## Sumber fakta"]
        for source in script["sources"]:
            lines.append(
                f"- {source['id']} ({source.get('confidence')}, {source.get('verification')}): "
                f"{source.get('claim')} — {source.get('source_url') or source.get('source_title') or 'tanpa URL'}"
            )
    return "\n".join(lines) + "\n"
