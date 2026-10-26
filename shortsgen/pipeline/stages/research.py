"""Stage RISET: kumpulkan fakta + sumber, tandai tingkat keyakinan secara jujur.

Prinsip anti-fabrikasi:
- Model DILARANG mengarang URL; sumber tanpa verifikasi harus confidence rendah.
- Hasil search hanya dianggap kandidat; hasil mock/sintetis tidak pernah menjadi bukti.
- Tanpa provider search/fetch, semua fakta ditandai `verification: unverified`
  beserta alasannya (tidak menyamar sebagai terverifikasi).
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

from ...config import Config
from ...models import FactSource, SearchHit, utcnow_iso
from ...providers.base import ProviderBundle
from ...util.jsonparse import JsonParseError, extract_json_object
from ..orchestrator import StageContext

RESEARCH_PROMPT = """Kamu adalah peneliti faktual untuk video pendek edukasi berbahasa Indonesia.

Topik: {topic}

Tugas: susun {fact_count} fakta yang paling dapat dipertanggungjawabkan tentang topik ini.

Aturan WAJIB (dipatuhi ketat):
1. JANGAN mengarang sumber. Jika kamu tidak yakin ada sumber bernama seperti itu, tulis source_url = null.
2. Bedakan jelas antara fakta mapan, konsensus ilmiah, dan opini/klaim yang diperdebatkan.
3. Jangan menyertakan klaim medis, keuangan, atau hukum yang menyesatkan.
4. confidence hanya boleh "high" bila faktanya sangat mapan (mis. fakta fisika dasar);
   gunakan "medium" atau "low" bila ragu.

Balas HANYA dengan JSON valid (tanpa penjelasan lain) dengan skema:
{{
  "topic_summary": "ringkasan 1-2 kalimat",
  "facts": [
    {{
      "claim": "satu kalimat fakta yang spesifik",
      "detail": "penjelasan tambahan singkat (opsional)",
      "source_name": "nama sumber bila yakin, atau null",
      "source_url": "URL bila sangat yakin, atau null",
      "confidence": "high|medium|low",
      "notes": "catatan kehati-hatian (opsional)"
    }}
  ],
  "uncertainties": ["hal yang belum jelas dan perlu verifikasi"],
  "search_queries": ["3-5 kueri pencarian yang relevan"]
}}"""


class ResearchStage:
    stage = "research"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        if ctx.providers.llm is None:
            raise RuntimeError("LLM tidak dikonfigurasi; stage riset membutuhkannya")

        fact_count = 6
        prompt = RESEARCH_PROMPT.format(topic=ctx.job.topic, fact_count=fact_count)
        llm_result = ctx.providers.llm.chat(
            [
                {"role": "system", "content": "Peneliti faktual yang sangat kehati-hatian."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1400,
        )
        ctx.repo.record_usage(
            ctx.job.id,
            provider=llm_result.provider,
            model=llm_result.model,
            kind="llm",
            units=_token_units(llm_result.usage),
            meta={"stage": "research", "latency_ms": llm_result.latency_ms},
        )

        data = self._parse(llm_result.text)
        facts = self._normalize_facts(data.get("facts") or [])
        if not facts:
            raise RuntimeError("riset tidak menghasilkan fakta yang bisa dipakai")

        search_hits = self._run_search(ctx, data.get("search_queries") or [])
        facts, fetch_notes = self._validate_sources(ctx, facts)

        artifacts = {
            "facts": [f.to_dict() for f in facts],
            "search_candidates": [h.to_dict() for h in search_hits],
            "search_notes": self._search_notes(ctx, search_hits),            "fetch_notes": fetch_notes,
            "uncertainties": data.get("uncertainties") or [],
            "topic_summary": data.get("topic_summary") or "",
            "provider_search": ctx.config.search_provider or "<not_configured>",
            "provider_fetch": ctx.config.fetch_provider or "<not_configured>",
            "created_at": utcnow_iso(),
        }
        research_path = ctx.artifact_path("research.json")
        research_path.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("research.md")).write_text(_render_markdown(ctx.job.topic, artifacts), encoding="utf-8")

        return {
            "research": str(research_path),
            "facts_count": len(facts),
            "unverified_count": sum(
                1 for f in facts if f.verification != "fetched_supported"
            ),
            "search_candidates": len(search_hits),
        }

    # -- internal -----------------------------------------------------

    def _parse(self, text: str) -> dict[str, Any]:
        try:
            return extract_json_object(text)
        except JsonParseError as exc:
            raise RuntimeError(f"keluaran riset bukan JSON valid: {exc}") from exc

    @staticmethod
    def _normalize_facts(raw: list[Any]) -> list[FactSource]:
        facts: list[FactSource] = []
        for index, item in enumerate(raw, start=1):
            if isinstance(item, str):
                item = {"claim": item}
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            if not claim:
                continue
            confidence = str(item.get("confidence") or "medium").lower()
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            source_url = item.get("source_url")
            if source_url is not None:
                source_url = str(source_url).strip() or None
            fact = FactSource(
                claim=claim,
                source_url=source_url,
                source_title=(str(item["source_name"]).strip() if item.get("source_name") else None),
                confidence=confidence,
                quote=str(item.get("detail") or "").strip() or None,
                notes=str(item.get("notes") or "").strip() or None,
            )
            facts.append(fact)
        return facts

    def _run_search(self, ctx: StageContext, queries: list[str]) -> list[SearchHit]:
        if not _search_enabled(ctx):
            return []
        provider = ctx.providers.search
        assert provider is not None  # dijaga oleh _search_enabled
        hits: list[SearchHit] = []
        all_queries = [ctx.job.topic] + [str(q) for q in queries if str(q).strip()][:3]
        for query in all_queries[:3]:
            try:
                found = provider.search(query, max_results=3)
            except Exception as exc:  # search tidak boleh menggagalkan riset
                if self.logger:
                    self.logger.warning(
                        f"search gagal untuk kueri {query!r} (provider "
                        f"'{ctx.config.search_provider}'): {exc}"
                    )
                continue
            for hit in found:
                hit.provider = hit.provider or ctx.config.search_provider or "unknown"
                hits.append(hit)
            ctx.repo.record_usage(
                ctx.job.id,
                provider=hit_provider_name(provider),
                model=ctx.config.search_provider or "search",
                kind="search",
                units={"queries": 1, "results": float(len(found))},
                meta={"query": query},
            )
        return hits

    @staticmethod
    def _search_notes(ctx: StageContext, hits: list[SearchHit]) -> list[str]:
        notes: list[str] = []
        if not _search_enabled(ctx):
            notes.append(
                "SEARCH_PROVIDER belum dikonfigurasi; riset memakai pengetahuan model "
                "dan fakta ditandai belum terverifikasi."
            )
        elif not hits:
            notes.append(
                f"Provider search '{ctx.config.search_provider}' dikonfigurasi tetapi tidak "
                "mengembalikan hasil (lihat log)."
            )
        if any(_is_synthetic_hit(h) for h in hits):
            notes.append(
                "Beberapa hasil pencarian bersifat sintetis (mock) dan TIDAK dianggap bukti."
            )
        return notes

    def _validate_sources(self, ctx: StageContext, facts: list[FactSource]) -> tuple[list[FactSource], list[str]]:
        """Validasi sumber via fetch bila tersedia; selain itu tandai jujur apa adanya.

        `confidence` (keyakinan model) TIDAK dinaikkan tanpa bukti; `verification`
        mencatat apakah fakta benar-benar dicek terhadap sumber.
        """
        fetch = ctx.providers.fetch
        notes: list[str] = []
        validated: list[FactSource] = []

        for fact in facts:
            fact.accessed_at = utcnow_iso()
            has_real_url = bool(fact.source_url) and not _is_synthetic_url(fact.source_url)

            if not has_real_url:
                fact.verification = "no_source"
                if not fact.notes:
                    fact.notes = "Model tidak menyertakan URL sumber; belum ada verifikasi otomatis."
                validated.append(fact)
                continue

            if fetch is None:
                fact.verification = "source_provided_unfetched"
                if not fact.notes:
                    fact.notes = "URL sumber ada, tetapi FETCH_PROVIDER belum dikonfigurasi untuk verifikasi otomatis."
                validated.append(fact)
                continue

            try:
                document = fetch.fetch(fact.source_url, max_chars=20_000)
                overlap = _keyword_overlap(fact.claim, document.text)
                ctx.repo.record_usage(
                    ctx.job.id,
                    provider=document.provider or "fetch",
                    model=ctx.config.fetch_provider or "fetch",
                    kind="fetch",
                    units={"documents": 1, "characters": float(len(document.text))},
                    meta={"url": fact.source_url, "overlap": overlap},
                )
                if overlap >= 0.5:
                    fact.verification = "fetched_supported"
                    fact.quote = (fact.quote or document.text[:400])[:400]
                else:
                    fact.verification = "fetched_unsupported"
                    fact.confidence = "low"
                    fact.notes = (
                        (fact.notes + " " if fact.notes else "")
                        + f"Teks sumber tidak mendukung klaim secara memadai (overlap {overlap:.0%})."
                    )
            except Exception as exc:  # fetch gagal -> jangan mengklaim terverifikasi
                fact.verification = "fetch_failed"
                if not fact.notes:
                    fact.notes = "Pengambilan sumber gagal; klaim tetap belum terverifikasi."
                notes.append(f"Gagal mengambil {fact.source_url}: {exc}")
            validated.append(fact)
        return validated, notes


# -- helper -----------------------------------------------------------------

def _search_enabled(ctx: StageContext) -> bool:
    """True bila provider search ada DAN terkonfigurasi (bukan not_configured)."""
    provider = ctx.providers.search
    if provider is None:
        return False
    try:
        return provider.health().status != "not_configured"
    except Exception:
        return True


def hit_provider_name(provider: Any) -> str:
    return str(getattr(provider, "name", "search"))


def _is_synthetic_url(url: Optional[str]) -> bool:
    if not url:
        return True
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(".invalid") or host.endswith(".test") or host.endswith("example.com")


def _is_synthetic_hit(hit: SearchHit) -> bool:
    return (hit.provider or "").lower() == "mock" or _is_synthetic_url(hit.url)


def _keyword_overlap(claim: str, document: str, *, min_len: int = 5) -> float:
    words = {w.lower().strip(".,!?;:()[]\"'") for w in claim.split()}
    words = {w for w in words if len(w) >= min_len}
    if not words:
        return 0.0
    doc_lower = document.lower()
    matched = sum(1 for w in words if w in doc_lower)
    return matched / len(words)


def _token_units(usage: dict[str, Any]) -> dict[str, float]:
    return {
        "input_tokens": float(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
        "output_tokens": float(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
    }


def _render_markdown(topic: str, artifacts: dict[str, Any]) -> str:
    lines = [f"# Riset: {topic}", "", artifacts.get("topic_summary", ""), "", "## Fakta"]
    for index, fact in enumerate(artifacts["facts"], start=1):
        source = fact.get("source_url") or fact.get("source_title") or "tanpa sumber"
        lines.append(
            f"{index}. **F{index}** (keyakinan: {fact['confidence']}, verifikasi: {fact.get('verification', '-')}) "
            f"{fact['claim']}\n"
            f"   - sumber: {source}\n"
            f"   - catatan: {fact.get('notes') or '-'}"
        )
    if artifacts.get("uncertainties"):
        lines += ["", "## Ketidakpastian"] + [f"- {u}" for u in artifacts["uncertainties"]]
    if artifacts.get("search_notes"):
        lines += ["", "## Catatan pencarian"] + [f"- {n}" for n in artifacts["search_notes"]]
    return "\n".join(lines) + "\n"
