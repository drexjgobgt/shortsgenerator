"""Stage REVIEW naskah: pemeriksaan deterministik + (opsional) satu putaran revisi LLM.

Pemeriksaan deterministik (tanpa biaya API):
- panjang hook, deteksi clickbait menyesatkan, huruf kapital berlebihan
- durasi estimasi vs target, jumlah/panjang blok
- rujukan fakta tak dikenal, penggunaan fakta belum terverifikasi
- kelengkapan metadata (disclosure, deskripsi, tags) dan heuristik Bahasa Indonesia

Bila ada masalah berat, satu putaran revisi LLM dicoba (bila LLM tersedia);
bila masih berat -> stage GAGAL (tidak boleh lanjut dengan klaim keliru).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...models import utcnow_iso
from ...providers.base import ProviderBundle
from ...story.narrative import validate_narrative
from ...util.jsonparse import JsonParseError, extract_json_object
from ..orchestrator import StageContext
from .script import (
    MAX_CLOSING_WORDS,
    MAX_HOOK_WORDS,
    MAX_NARRATION_WORDS,
    MIN_BLOCKS,
    MIN_BLOCK_WORDS,
    normalize_script,
    render_script_markdown,
)

CLICKBAIT_PHRASES = (
    "tidak akan percaya",
    "tak akan percaya",
    "jangan kaget",
    "shocking",
    "heboh",
    "viral",
    "auto kaya",
    "auto viral",
    "100% ampuh",
    "dijamin",
    "rahasia tersembunyi",
    "para ahli sangat terkejut",
    "bikin merinding",
    "gak nyangka",
    "tidak disangka",
)
INDONESIAN_STOPWORDS = (
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "untuk", "dengan", "karena",
    "adalah", "pada", "sebagai", "tidak", "bisa", "akan", "kita", "kamu",
)


class ReviewStage:
    stage = "review"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        research = _load_json_artifact(ctx, "research", "research.json")
        script = _load_json_artifact(ctx, "script", "script.json")
        facts = research.get("facts") or []

        issues = self._check(script, facts)
        blocking = [i for i in issues if i["severity"] == "high"]
        revision_attempted = False
        revision_error: Optional[str] = None

        if blocking and ctx.providers.llm is not None:
            revision_attempted = True
            revised = self._try_revision(ctx, script, facts, blocking)
            if revised is not None:
                backup = ctx.artifact_path("script_pre_review.json")
                backup.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
                script = revised
                (ctx.artifact_path("script.json")).write_text(
                    json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (ctx.artifact_path("script.md")).write_text(
                    render_script_markdown(script), encoding="utf-8"
                )
                issues = self._check(script, facts)
                blocking = [i for i in issues if i["severity"] == "high"]
            else:
                revision_error = "revisi LLM tidak menghasilkan naskah baru yang valid"

        passed = not blocking
        score = retention_score(issues)
        report = {
            "passed": passed,
            "issues": issues,
            "blocking_count": len(blocking),
            "retention_score": score,
            "revision_attempted": revision_attempted,
            "revision_error": revision_error,
            "stats": script.get("stats", {}),
            "target_duration_s": self.config.target_duration_s,
            "created_at": utcnow_iso(),
        }
        (ctx.artifact_path("review.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ctx.artifact_path("review.md")).write_text(_render_review_markdown(report), encoding="utf-8")

        if not passed:
            summary = "; ".join(f"{i['code']}: {i['message']}" for i in blocking[:3])
            raise RuntimeError(f"review menemukan masalah berat yang tidak terselesaikan: {summary}")

        return {
            "review": str(ctx.artifact_path("review.json")),
            "passed": True,
            "issues": len(issues),
            "retention_score": score,
            "revision_attempted": revision_attempted,
        }

    # -- pemeriksaan ---------------------------------------------------

    def _check(self, script: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []

        def add(severity: str, code: str, message: str) -> None:
            issues.append({"severity": severity, "code": code, "message": message})

        title = script.get("title", "")
        hook = script.get("hook", "")
        blocks = script.get("blocks") or []

        # 1. clickbait & kapital berlebihan
        haystack = f"{title} {hook}".lower()
        for phrase in CLICKBAIT_PHRASES:
            if phrase in haystack:
                add("high", "clickbait", f"frasa clickbait menyesatkan terdeteksi: '{phrase}'")
        letters = [c for c in title + hook if c.isalpha()]
        if letters:
            caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if caps_ratio > 0.35:
                add("high", "caps_abuse", f"huruf kapital berlebihan pada judul/hook ({caps_ratio:.0%})")
        if "!!!" in (title + hook):
            add("medium", "exclamation", "tanda seru berlebihan")

        # 2. hook: panjang + pemicu rasa ingin tahu (retensi detik pertama)
        hook_words = len(hook.split())
        if hook_words == 0:
            add("high", "missing_hook", "hook kosong")
        elif hook_words > MAX_HOOK_WORDS + 4:
            add("high", "hook_too_long", f"hook {hook_words} kata (maks {MAX_HOOK_WORDS})")
        elif hook_words > MAX_HOOK_WORDS:
            add("medium", "hook_too_long", f"hook {hook_words} kata (maks {MAX_HOOK_WORDS})")

        curiosity_markers = ("?", "justru", "ternyata", "padahal", "tapi", "bukan")
        has_curiosity = (
            any(marker in hook.lower() for marker in curiosity_markers)
            or any(ch.isdigit() for ch in hook)
        )
        if hook and not has_curiosity:
            add(
                "medium",
                "hook_no_curiosity",
                "hook tanpa pemicu rasa ingin tahu (pertanyaan/angka/kontras)",
            )

        # 3. tempo blok: pendek & padat (ganti visual tiap 2-4 dtk)
        for block in blocks:
            words = int(block.get("word_count") or 0)
            if words > MAX_NARRATION_WORDS:
                add(
                    "medium",
                    "block_too_long",
                    f"blok #{block.get('index')} {words} kata (maks {MAX_NARRATION_WORDS})",
                )
            elif words < MIN_BLOCK_WORDS and block.get("kind") == "body":
                add("low", "block_too_short", f"blok #{block.get('index')} hanya {words} kata")

        # 3. durasi
        estimated = float(script.get("stats", {}).get("estimated_duration_s") or 0)
        target = float(self.config.target_duration_s)
        low, high = target * 0.7, target * 1.3
        if estimated <= 0:
            add("high", "no_duration", "estimasi durasi tidak tersedia")
        elif not (low <= estimated <= high):
            add(
                "high" if not (target * 0.5 <= estimated <= target * 1.5) else "medium",
                "duration_out_of_range",
                f"estimasi {estimated:.1f} dtk di luar rentang {low:.0f}-{high:.0f} dtk",
            )

        # 4. rujukan fakta
        unknown = script.get("unknown_fact_refs") or []
        if unknown:
            add("high", "unknown_fact_refs", f"merujuk fakta yang tidak ada: {', '.join(unknown)}")
        if not script.get("facts_used"):
            add("medium", "no_facts_used", "naskah tidak merujuk satu pun fakta dari riset")

        # 5. fakta belum terverifikasi (mengacu pada `verification`, bukan hanya confidence)
        verification_by_index: dict[str, str] = {}
        for index, fact in enumerate(facts, start=1):
            verification_by_index[f"F{index}"] = str(fact.get("verification") or "no_source")
        used_unverified = [
            fact_id
            for fact_id in script.get("facts_used") or []
            if verification_by_index.get(fact_id, "no_source") != "fetched_supported"
        ]
        if used_unverified:
            severity = "high" if self.config.block_unverified_claims else "low"
            add(
                severity,
                "unverified_claims",
                "naskah memakai fakta yang belum diverifikasi otomatis: " + ", ".join(used_unverified),
            )

        # 6. metadata
        if not script.get("disclosure"):
            add("medium", "missing_disclosure", "disclosure konten sintetis belum ada")
        tags = script.get("tags") or []
        if len(tags) < 5:
            add("low", "few_tags", f"hanya {len(tags)} tag (disarankan >= 5)")
        if not (script.get("description") or "").strip():
            add("medium", "missing_description", "deskripsi kosong")

        # 7. heuristik Bahasa Indonesia
        text = " ".join(block.get("narration", "") for block in blocks).lower()
        id_hits = sum(1 for word in INDONESIAN_STOPWORDS if re.search(rf"\b{word}\b", text))
        if id_hits < 3:
            add("medium", "language_check", "teks narasi tampak bukan Bahasa Indonesia yang wajar")

        # 8. perangkat retensi (panjang, pattern interrupt, loop)
        stats = script.get("stats") or {}
        if len(blocks) < MIN_BLOCKS:
            add("medium", "too_few_blocks", f"hanya {len(blocks)} blok (disarankan >= {MIN_BLOCKS})")
        if not stats.get("has_rhetorical_question"):
            add("low", "no_question", "tidak ada pertanyaan retoris (pattern interrupt)")
        closing = str(script.get("closing") or "").strip()
        if not closing:
            add("medium", "missing_closing", "closing kosong")
        else:
            closing_words = len(closing.split())
            if closing_words > MAX_CLOSING_WORDS:
                add("low", "closing_too_long", f"closing {closing_words} kata (maks {MAX_CLOSING_WORDS})")
            hook_keys = {w.lower().strip(".,!?;:") for w in hook.split() if len(w) > 4}
            closing_keys = {w.lower().strip(".,!?;:") for w in closing.split() if len(w) > 4}
            if hook_keys and not (hook_keys & closing_keys):
                add("low", "weak_loop", "closing tidak menyambung kembali ke hook (potensi loop lemah)")
            if closing.rstrip().endswith(("...", "…")):
                add("low", "closing_ellipsis", "closing berakhir dengan elipsis (terkesan menggantung)")

        # 9. aturan naratif hiburan + kejujuran klaim (story/narrative.py)
        existing = {(issue["severity"], issue["code"], issue["message"]) for issue in issues}
        for issue in validate_narrative(script):
            key = (issue["severity"], issue["code"], issue["message"])
            if key not in existing:
                issues.append(issue)
                existing.add(key)

        return issues

    def _try_revision(
        self,
        ctx: StageContext,
        script: dict[str, Any],
        facts: list[dict[str, Any]],
        blocking: list[dict[str, str]],
    ) -> Optional[dict[str, Any]]:
        issue_lines = "\n".join(f"- [{i['code']}] {i['message']}" for i in blocking)
        facts_block = "\n".join(
            f"- F{index} [{fact.get('confidence')}] {fact.get('claim')}"
            for index, fact in enumerate(facts, start=1)
        )
        schema = (
            '{"title": "...", "hook": "...", "hook_pattern": "contradiction", '
            '"beats": [{"role": "hook|context|escalation|evidence|twist|payoff|closing", '
            '"narration": "...", "on_screen_text": "...", "fact_refs": ["F1"], '
            '"claim_type": "verified|theory|speculation|dramatization", "emotion": "curious", '
            '"intensity": 3, "emphasis": ["kata"], "visual_seed": "english concrete visual", '
            '"sfx_cue": "whoosh", "transition": "smoothleft", "voice_direction": "..."}], '
            '"closing": "...", "description": "...", "tags": ["..."], "facts_used": ["F1"], '
            '"disclosure": "..."}'
        )
        prompt = f"""Naskah berikut DITOLAK oleh pemeriksa kualitas.
Alasan (wajib diperbaiki semua):
{issue_lines}

Daftar fakta yang boleh dipakai:
{facts_block}

Naskah saat ini (JSON):
{json.dumps({k: v for k, v in script.items() if k in ('title', 'hook', 'closing', 'description', 'tags', 'facts_used')}, ensure_ascii=False)}
Blok narasi: {json.dumps(script.get('blocks') or [], ensure_ascii=False)}

Perbaiki masalah di atas dan balas HANYA JSON valid dengan skema berikut:
{schema}
Jangan menambah fakta di luar daftar."""
        try:
            result = ctx.providers.llm.chat(
                [
                    {"role": "system", "content": "Editor naskah yang disiplin memperbaiki kesalahan."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=1800,
            )
        except Exception as exc:  # revisi bersifat best-effort
            if self.logger:
                self.logger.warning(f"revisi naskah gagal di provider: {exc}")
            return None

        ctx.repo.record_usage(
            ctx.job.id,
            provider=result.provider,
            model=result.model,
            kind="llm",
            units={
                "input_tokens": float(result.usage.get("input_tokens", result.usage.get("prompt_tokens", 0)) or 0),
                "output_tokens": float(result.usage.get("output_tokens", result.usage.get("completion_tokens", 0)) or 0),
            },
            meta={"stage": "review_revision", "latency_ms": result.latency_ms},
        )
        try:
            data = extract_json_object(result.text)
            return normalize_script(
                data,
                facts,
                topic=ctx.job.topic,
                target_duration=self.config.target_duration_s,
            )
        except (JsonParseError, RuntimeError) as exc:
            if self.logger:
                self.logger.warning(f"revisi naskah tidak valid: {exc}")
            return None


# -- helper -----------------------------------------------------------------

SEVERITY_WEIGHTS = {"high": 25, "medium": 12, "low": 4}


def retention_score(issues: list[dict[str, str]]) -> int:
    """Skor retensi heuristik 0-100 dari temuan review (bukan metrik resmi YouTube)."""
    penalty = sum(SEVERITY_WEIGHTS.get(issue.get("severity", "medium"), 6) for issue in issues)
    return max(0, min(100, 100 - penalty))


def _load_json_artifact(ctx: StageContext, stage: str, filename: str) -> dict[str, Any]:
    record = ctx.repo.get_stage(ctx.job.id, stage)
    candidate: Optional[Path] = None
    if record and record.artifacts:
        for value in record.artifacts.values():
            path = Path(str(value))
            if path.suffix == ".json" and path.exists():
                candidate = path
                break
    if candidate is None:
        fallback = ctx.artifacts_dir / filename
        candidate = fallback if fallback.exists() else None
    if candidate is None:
        raise RuntimeError(f"artefak '{filename}' tidak ditemukan; jalankan stage {stage} lebih dulu")
    return json.loads(candidate.read_text(encoding="utf-8"))


def _render_review_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Review naskah",
        "",
        f"Status: **{'LULUS' if report['passed'] else 'GAGAL'}** "
        f"({report['blocking_count']} masalah berat, {len(report['issues'])} total temuan)",
        f"Skor retensi heuristik: **{report.get('retention_score', '-')}/100**",
        f"Revisi LLM dicoba: {'ya' if report['revision_attempted'] else 'tidak'}",
        "",
        "## Temuan",
    ]
    if not report["issues"]:
        lines.append("- (tidak ada temuan)")
    for issue in report["issues"]:
        lines.append(f"- [{issue['severity']}] {issue['code']}: {issue['message']}")
    return "\n".join(lines) + "\n"
