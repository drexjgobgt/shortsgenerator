"""Stage ASSETS: akuisisi aset visual per scene sesuai rencana storyboard v2.

Urutan strategi (dari storyboard, dengan fallback bertingkat):
  1. stock_video  -> klip stok berlisensi (hanya usage_allowed=True), dengan
                     gerbang relevansi tag (skor + kata spesifik)
  2. ai_image     -> gambar AI 9:16 dengan **reference chain** agar subjek/palet
                     konsisten antar scene; anggaran per job
  3. procedural   -> grafis lokal Pillow (selalu berhasil, tanpa biaya)

Setiap aset mencatat lisensi/atribusi (MediaAsset). Tidak ada aset dari sumber
tanpa izin. Hasil pemeriksaan relevansi disimpan di `relevance.json`; skor di
bawah ambang tidak disembunyikan, melainkan ditandai `needs_human_review`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ...config import Config
from ...errors import ProviderError
from ...media.graphics import create_background
from ...models import MediaAsset, utcnow_iso
from ...providers.base import HealthStatus, ProviderBundle
from ...providers.stock.base import StockClip
from ...story.relevance import (
    improve_prompt,
    score_visual_plan,
    summarize_relevance,
    vision_check,
)
from ..orchestrator import StageContext

STOCK_QUERY_FALLBACK_WORDS = 6
_ABSTRACT_TERMS = {
    "mysterious", "cinematic", "concept", "conceptual", "abstract", "diagram",
    "illustration", "glow", "subtle", "editorial", "artistic", "stylized",
    "visualization", "metaphor", "symbolic",
}
# Kata tag terlalu umum (cocok dengan hampir semua klip) -> tidak dihitung sebagai bukti relevansi.
_GENERIC_TAG_TOKENS = {
    "sky", "blue", "light", "sun", "sunset", "sunlight", "nature", "background",
    "landscape", "sea", "ocean", "water", "cloud", "clouds", "cloudy", "weather",
    "outdoor", "outdoors", "beautiful", "color", "colors", "colour", "view",
    "horizon", "atmosphere", "air", "video", "footage", "4k", "hd",
}
# Minimal kecocokan agar klip stok diterima; di bawah ini -> fallback gambar AI.
MIN_STOCK_TAG_SCORE = 2
MIN_STOCK_SPECIFIC_TOKENS = 1
MAX_AI_RETRIES_PER_SCENE = 1


class AssetsStage:
    stage = "assets"

    def __init__(self, providers: ProviderBundle, config: Config, *, logger: Any = None):
        self.providers = providers
        self.config = config
        self.logger = logger

    def run(self, ctx: StageContext) -> dict[str, Any]:
        storyboard = _load_storyboard(ctx)
        scenes = storyboard.get("scenes") or []
        if not scenes:
            raise RuntimeError("storyboard.json tidak memuat scene")

        assets_dir = ctx.artifacts_dir.parent / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, Any]] = []
        notes: list[str] = []
        relevance_entries: list[dict[str, Any]] = []
        counters = {"stock_video": 0, "generated_image": 0, "procedural": 0}
        reference_path: Optional[Path] = None

        for scene in scenes:
            override = self._override_asset(scene, ctx)
            if override is not None:
                asset, relevance = override
                notes.append(
                    f"override scene {scene.get('index')}: {Path(asset.path).name} "
                    "(tanpa panggilan provider)"
                )
            else:
                reused = self._reuse_existing(scene, ctx)
                if reused is not None:
                    asset, relevance = reused
                    notes.append(
                        f"aset scene {scene.get('index')} dipakai ulang: {Path(asset.path).name}"
                    )
                else:
                    asset, scene_notes, relevance = self._acquire(
                        ctx, scene, assets_dir, reference_path=reference_path
                    )
                    notes.extend(scene_notes)
            counters[asset.kind] = counters.get(asset.kind, 0) + 1
            scene["asset"] = {
                "kind": asset.kind,
                "path": str(asset.path),
                "license": asset.license,
                "attribution": asset.attribution,
                "usage_allowed": asset.usage_allowed,
                "source": asset.source,
                "relevance_score": (relevance or {}).get("score"),
                "needs_human_review": bool((relevance or {}).get("needs_human_review")),
                "reference_of": (relevance or {}).get("reference_of"),
                "reference_used": (relevance or {}).get("reference_used"),
            }
            results.append(scene["asset"] | {"scene_index": scene.get("index"), "meta": asset.meta})
            relevance_entries.append(
                {
                    "scene_index": scene.get("index"),
                    "scene_id": scene.get("id"),
                    "asset_kind": asset.kind,
                    "relevance": relevance,
                }
            )
            if asset.kind == "generated_image" and reference_path is None:
                reference_path = Path(asset.path)

        # simpan storyboard terbaru (dengan penunjuk aset) + ringkasan aset
        storyboard_path = ctx.artifact_path("storyboard.json")
        storyboard_path.write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")

        assets_summary = {
            "assets": results,
            "counts": counters,
            "notes": notes,
            "created_at": utcnow_iso(),
        }
        assets_path = ctx.artifact_path("assets.json")
        assets_path.write_text(json.dumps(assets_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("assets.md")).write_text(_render_assets_markdown(assets_summary), encoding="utf-8")

        relevance = summarize_relevance(relevance_entries, self.config.visual_relevance_min)
        relevance_path = ctx.artifact_path("relevance.json")
        relevance_path.write_text(json.dumps(relevance, ensure_ascii=False, indent=2), encoding="utf-8")
        (ctx.artifact_path("relevance.md")).write_text(_render_relevance_markdown(relevance), encoding="utf-8")

        if relevance["needs_human_review"]:
            notes.append(
                "scene perlu ditinjau manusia: "
                + ", ".join(str(index) for index in relevance["needs_human_review"])
            )

        return {
            "assets": str(assets_path),
            "relevance": str(relevance_path),
            "counts": counters,
            "notes": notes,
            "average_relevance": relevance["average_score"],
            "needs_human_review": relevance["needs_human_review"],
        }

    # -- internal -----------------------------------------------------

    def _override_asset(
        self, scene: dict[str, Any], ctx: StageContext
    ) -> Optional[tuple[MediaAsset, dict[str, Any]]]:
        """Gunakan gambar yang disediakan manusia/agen dari `assets/overrides/`.

        Ini jalur tanpa biaya provider: berguna saat kuota gambar habis atau
        saat pengguna ingin memakai hasil generator eksternal (mis. MCP).
        Berkas dicari sebagai `scene_XX.png|jpg|jpeg|webp`.
        """
        index = scene.get("index")
        if index is None:
            return None
        try:
            number = int(index)
        except (TypeError, ValueError):
            return None
        overrides_dir = ctx.artifacts_dir.parent / "assets" / "overrides"
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            path = overrides_dir / f"scene_{number:02d}{suffix}"
            if not path.is_file():
                continue
            relevance = score_visual_plan(
                str(scene.get("narration") or ""),
                str(scene.get("visual_seed") or ""),
                subject=str(scene.get("subject") or ""),
                action=str(scene.get("action") or ""),
                topic=ctx.job.topic,
            )
            relevance["method"] = "manual_override_plan"
            relevance["override"] = True
            relevance["reference_used"] = False
            relevance["reference_of"] = None
            if relevance.get("score", 0) < self.config.visual_relevance_min:
                relevance["needs_human_review"] = True
            return (
                MediaAsset(
                    kind="generated_image",
                    path=str(path),
                    source="manual_override",
                    license="AI-generated (tunduk pada ketentuan penyedia model)",
                    attribution=None,
                    usage_allowed=True,
                    meta={"override": True, "filename": path.name},
                ),
                relevance,
            )
        return None

    def _reuse_existing(
        self, scene: dict[str, Any], ctx: StageContext
    ) -> Optional[tuple[MediaAsset, Optional[dict[str, Any]]]]:
        """Pakai ulang aset yang sudah ada di disk (resume tanpa biaya provider)."""
        existing = scene.get("asset") or {}
        kind = str(existing.get("kind") or "")
        path = Path(str(existing.get("path") or ""))
        if kind not in ("stock_video", "generated_image", "procedural") or not path.exists():
            return None
        asset = MediaAsset(
            kind=kind,
            path=str(path),
            source=str(existing.get("source") or ""),
            license=str(existing.get("license") or "unknown"),
            attribution=existing.get("attribution"),
            usage_allowed=bool(existing.get("usage_allowed")),
            meta={"reused": True},
        )
        relevance: Optional[dict[str, Any]] = None
        if kind == "generated_image":
            relevance = score_visual_plan(
                str(scene.get("narration") or ""),
                str(scene.get("visual_seed") or ""),
                subject=str(scene.get("subject") or ""),
                action=str(scene.get("action") or ""),
                topic=ctx.job.topic,
            )
            relevance["reused"] = True
            if existing.get("reference_used") is not None:
                relevance["reference_used"] = bool(existing["reference_used"])
            else:
                relevance["reference_used"] = None  # tidak diketahui; jangan mengklaim
            relevance["reference_of"] = existing.get("reference_of")
        elif kind == "stock_video" and existing.get("relevance_score") is not None:
            relevance = {
                "score": float(existing["relevance_score"]),
                "method": "stock_tags_reused",
                "reasons": ["skor dari pemeriksaan sebelumnya"],
                "needs_human_review": bool(existing.get("needs_human_review")),
                "reused": True,
            }
        return asset, relevance

    def _acquire(
        self,
        ctx: StageContext,
        scene: dict[str, Any],
        assets_dir: Path,
        *,
        reference_path: Optional[Path] = None,
    ) -> tuple[MediaAsset, list[str], Optional[dict[str, Any]]]:
        index = int(scene.get("index", 0))
        strategy = str(scene.get("visual_strategy") or "procedural")
        notes: list[str] = []

        if strategy == "stock_video":
            asset, stock_notes, relevance = self._try_stock(ctx, scene, assets_dir, index)
            notes.extend(stock_notes)
            if asset is not None:
                return asset, notes, relevance
            strategy = "ai_image"  # fallback bertingkat

        if strategy == "ai_image":
            asset, image_notes, relevance = self._try_ai_image(
                ctx, scene, assets_dir, index, reference_path=reference_path
            )
            notes.extend(image_notes)
            if asset is not None:
                return asset, notes, relevance

        asset = self._procedural(ctx, scene, assets_dir, index)
        return asset, notes, None

    def _try_stock(
        self, ctx: StageContext, scene: dict[str, Any], assets_dir: Path, index: int
    ) -> tuple[Optional[MediaAsset], list[str], Optional[dict[str, Any]]]:
        provider = getattr(ctx.providers, "stock", None)
        notes: list[str] = []
        if provider is None:
            return None, ["stock tidak dikonfigurasi; memakai fallback gambar/grafis"], None
        try:
            if provider.health().status != HealthStatus.AVAILABLE:
                return None, [f"provider stok tidak siap (status {provider.health().status})"], None
        except Exception as exc:
            return None, [f"health provider stok gagal: {exc}"], None

        query = _stock_query(scene, ctx.job.topic)
        try:
            clips = provider.search(query, max_results=3)
        except ProviderError as exc:
            return None, [f"pencarian stok gagal: {exc}"], None

        usable = [clip for clip in clips if clip.usage_allowed]
        if not usable:
            return None, [f"tidak ada klip stok berlisensi untuk kueri '{query}'"], None
        ranked = _rank_stock_clips(usable, query)
        best = ranked[0]
        if not _is_stock_relevant(best, query):
            return None, [
                f"tidak ada klip stok yang cukup relevan untuk kueri '{query}' "
                f"(skor {_tag_overlap(best, query)}, minimum {MIN_STOCK_TAG_SCORE} + "
                f"{MIN_STOCK_SPECIFIC_TOKENS} kata spesifik); memakai gambar AI"
            ], None
        clip = best
        notes.append(
            f"stok scene {index}: kueri '{query}' -> {len(usable)} kandidat, "
            f"dipilih {clip.clip_id} (skor kecocokan tag {_tag_overlap(clip, query)})"
        )
        out_path = assets_dir / f"scene_{index:02d}_stock{Path(clip.download_url).suffix or '.mp4'}"
        try:
            provider.download(clip, out_path, max_bytes=self.config.max_asset_mb * 1024 * 1024)
        except ProviderError as exc:
            return None, [f"unduhan klip stok gagal: {exc}"], None

        ctx.repo.record_usage(
            ctx.job.id,
            provider=clip.provider,
            model=f"stock:{clip.clip_id}",
            kind="stock",
            units={"clips": 1, "bytes": float(out_path.stat().st_size)},
            meta={"query": query, "license": clip.license},
        )
        asset = MediaAsset(
            kind="stock_video",
            path=str(out_path),
            source=clip.provider,
            license=clip.license,
            attribution=clip.attribution,
            usage_allowed=True,
            meta={"clip_id": clip.clip_id, "page_url": clip.page_url, "query": query},
        )
        # Skor stok dari tag: bukti relevansi yang bisa diperiksa manusia.
        overlap = _tag_overlap(clip, query)
        specific = _specific_overlap(clip, query)
        relevance = {
            "score": min(10.0, 5.0 + specific * 2.0),
            "method": "stock_tags",
            "shared_tokens": sorted(_query_tokens(query) & _clip_tags(clip)),
            "generic_tokens": max(0, overlap - specific),
            "reasons": [f"tag cocok {overlap}, spesifik {specific}"],
            "needs_human_review": specific < MIN_STOCK_SPECIFIC_TOKENS,
        }
        return asset, notes, relevance

    def _try_ai_image(
        self,
        ctx: StageContext,
        scene: dict[str, Any],
        assets_dir: Path,
        index: int,
        *,
        reference_path: Optional[Path] = None,
    ) -> tuple[Optional[MediaAsset], list[str], Optional[dict[str, Any]]]:
        provider = ctx.providers.image
        if provider is None:
            return None, ["image provider tidak dikonfigurasi; memakai grafis prosedural"], None
        prompt = str(scene.get("visual_prompt") or scene.get("on_screen_text") or ctx.job.topic)
        narration = str(scene.get("narration") or "")
        visual_seed = str(scene.get("visual_seed") or "")
        plan = score_visual_plan(
            narration,
            visual_seed,
            subject=str(scene.get("subject") or ""),
            action=str(scene.get("action") or ""),
            topic=ctx.job.topic,
        )
        notes: list[str] = []
        relevance: dict[str, Any] = dict(plan)
        use_reference = bool(self.config.reference_chain) and reference_path is not None
        if use_reference:
            notes.append(f"gambar AI scene {index} memakai reference {Path(reference_path).name}")
        out_path = assets_dir / f"scene_{index:02d}_ai.png"
        try:
            result = self._generate_image(
                ctx, provider, prompt, out_path, reference_path=reference_path if use_reference else None
            )
        except ProviderError as exc:
            return None, [f"gambar AI gagal (scene {index}): {exc.__class__.__name__} - {exc}"], None

        relevance["reference_used"] = use_reference
        relevance["reference_of"] = str(reference_path.name) if use_reference else None

        if self.config.visual_relevance_check == "llm":
            relevance = self._vision_pass(
                ctx,
                provider,
                scene,
                out_path,
                prompt=prompt,
                plan=plan,
                reference_path=reference_path if use_reference else None,
                notes=notes,
            )
        elif relevance.get("score", 0) < self.config.visual_relevance_min:
            relevance["needs_human_review"] = True
            notes.append(
                f"scene {index}: skor relevansi rencana {relevance['score']}/10 "
                f"di bawah ambang {self.config.visual_relevance_min}"
            )

        ctx.repo.record_usage(
            ctx.job.id,
            provider=result.provider,
            model=result.model,
            kind="image",
            units={"images": 1, "bytes": float(result.bytes_written)},
            meta={
                "prompt": prompt[:200],
                "mime": result.mime,
                "latency_ms": result.latency_ms,
                "reference": use_reference,
            },
        )
        asset = MediaAsset(
            kind="generated_image",
            path=str(result.path),
            source=f"{result.provider}:{result.model}",
            license="AI-generated (tunduk pada ketentuan penyedia model)",
            attribution=None,
            usage_allowed=True,
            meta={
                "prompt": prompt[:300],
                "mime": result.mime,
                "reference_used": use_reference,
                "relevance_score": relevance.get("score"),
            },
        )
        return asset, notes, relevance

    def _generate_image(
        self,
        ctx: StageContext,
        provider: Any,
        prompt: str,
        out_path: Path,
        *,
        reference_path: Optional[Path] = None,
    ) -> Any:
        """Panggil provider image; hanya kirim argumen yang benar-benar didukung."""
        import inspect

        try:
            parameters = inspect.signature(provider.generate).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "aspect_ratio" in parameters:
            kwargs["aspect_ratio"] = "9:16"
        if "reference_image" in parameters:
            kwargs["reference_image"] = reference_path
        return provider.generate(prompt, out_path, **kwargs)

    def _vision_pass(
        self,
        ctx: StageContext,
        provider: Any,
        scene: dict[str, Any],
        image_path: Path,
        *,
        prompt: str,
        plan: dict[str, Any],
        reference_path: Optional[Path],
        notes: list[str],
    ) -> dict[str, Any]:
        """Pemeriksaan vision opsional + satu regenerasi bila di bawah ambang."""
        llm = getattr(ctx.providers, "llm", None)
        index = int(scene.get("index", 0))
        if llm is None:
            notes.append(f"scene {index}: vision check diminta tetapi LLM tidak tersedia; memakai heuristik")
            plan["needs_human_review"] = plan.get("score", 0) < self.config.visual_relevance_min
            return plan
        style = str((scene.get("palette") or ""))
        try:
            checked = vision_check(
                llm,
                image_path,
                narration=str(scene.get("narration") or ""),
                visual_seed=str(scene.get("visual_seed") or ""),
                style=style,
                model=self.config.vision_model or None,
            )
        except Exception as exc:
            notes.append(f"scene {index}: vision check gagal ({exc.__class__.__name__}); memakai heuristik")
            plan["vision_status"] = f"failed:{exc.__class__.__name__}"
            plan["needs_human_review"] = plan.get("score", 0) < self.config.visual_relevance_min
            return plan

        if checked["score"] >= self.config.visual_relevance_min or not checked.get("better_prompt"):
            checked["heuristic_score"] = plan.get("score")
            checked["needs_human_review"] = checked["score"] < self.config.visual_relevance_min
            if checked["needs_human_review"]:
                notes.append(
                    f"scene {index}: vision {checked['score']}/10 di bawah ambang "
                    f"{self.config.visual_relevance_min}"
                )
            return checked

        # satu regenerasi dengan prompt yang lebih konkret (biaya tambahan dicatat)
        better = improve_prompt(prompt, str(scene.get("narration") or ""), str(scene.get("visual_seed") or ""))
        retry_path = image_path.with_name(image_path.stem + "_retry" + image_path.suffix)
        notes.append(
            f"scene {index}: vision {checked['score']}/10 -> regenerasi sekali dengan prompt lebih konkret"
        )
        try:
            result = self._generate_image(
                ctx, provider, better, retry_path, reference_path=reference_path
            )
            ctx.repo.record_usage(
                ctx.job.id,
                provider=result.provider,
                model=result.model,
                kind="image",
                units={"images": 1, "bytes": float(result.bytes_written)},
                meta={"prompt": better[:200], "retry": True, "scene": index},
            )
            recheck = vision_check(
                llm,
                retry_path,
                narration=str(scene.get("narration") or ""),
                visual_seed=str(scene.get("visual_seed") or ""),
                style=style,
                model=self.config.vision_model or None,
            )
        except Exception as exc:
            notes.append(f"scene {index}: regenerasi/vision ulang gagal ({exc.__class__.__name__}); memakai hasil pertama")
            checked["heuristic_score"] = plan.get("score")
            checked["needs_human_review"] = checked["score"] < self.config.visual_relevance_min
            return checked

        # ganti berkas utama bila hasil kedua lebih baik
        if recheck["score"] >= checked["score"]:
            image_path.write_bytes(retry_path.read_bytes())
            recheck["heuristic_score"] = plan.get("score")
            recheck["retried"] = True
            recheck["first_score"] = checked["score"]
            recheck["needs_human_review"] = recheck["score"] < self.config.visual_relevance_min
            return recheck
        checked["heuristic_score"] = plan.get("score")
        checked["retried"] = True
        checked["second_score"] = recheck["score"]
        checked["needs_human_review"] = checked["score"] < self.config.visual_relevance_min
        return checked

    def _procedural(
        self, ctx: StageContext, scene: dict[str, Any], assets_dir: Path, index: int
    ) -> MediaAsset:
        out_path = assets_dir / f"scene_{index:02d}_procedural.png"
        create_background(
            out_path,
            width=self.config.render_width,
            height=self.config.render_height,
            seed_text=f"{ctx.job.topic}-{index}",
            title=str(scene.get("on_screen_text") or "") or None,
            subtitle=str(scene.get("narration") or "")[:120] or None,
        )
        return MediaAsset(
            kind="procedural",
            path=str(out_path),
            source="shortsgen:graphics",
            license="dibuat lokal (bebas dipakai)",
            attribution=None,
            usage_allowed=True,
            meta={"generator": "Pillow", "width": self.config.render_width, "height": self.config.render_height},
        )


def _stock_query(scene: dict[str, Any], topic: str) -> str:
    """Kueri stok konkret: utamakan `stock_query` dari naskah; buang kata abstrak."""
    explicit = str(scene.get("stock_query") or "").strip()
    if explicit:
        return " ".join(explicit.split()[:4])
    base = str(scene.get("visual_prompt") or "").strip() or str(scene.get("on_screen_text") or topic)
    words = [
        w for w in base.lower().replace(",", " ").split()
        if w not in _ABSTRACT_TERMS and len(w) > 2
    ]
    return " ".join(words[:3]) or topic[:40]


def _query_tokens(query: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", query.lower()) if len(token) > 2}


def _clip_tags(clip: StockClip) -> set[str]:
    meta = clip.meta if isinstance(clip.meta, dict) else {}
    raw = meta.get("tags")
    if isinstance(raw, str):
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    if isinstance(raw, (list, tuple)):
        return {str(t).strip().lower() for t in raw if str(t).strip()}
    return set()


def _tag_overlap(clip: StockClip, query: str) -> int:
    return len(_query_tokens(query) & _clip_tags(clip))


def _specific_overlap(clip: StockClip, query: str) -> int:
    """Jumlah kata kueri yang cocok DAN tidak termasuk kata tag umum."""
    matches = _query_tokens(query) & _clip_tags(clip)
    return len(matches - _GENERIC_TAG_TOKENS)


def _is_stock_relevant(clip: StockClip, query: str) -> bool:
    """Gerbang relevansi: cukup cocok DAN minimal satu kata spesifik.

    Mencegah kejadian seperti klip kosmetik/pantai untuk topik pesawat/gas —
    lebih baik memakai gambar AI yang dibuat dari prompt (pasti sesuai konteks).
    """
    return (
        _tag_overlap(clip, query) >= MIN_STOCK_TAG_SCORE
        and _specific_overlap(clip, query) >= MIN_STOCK_SPECIFIC_TOKENS
    )


def _rank_stock_clips(clips: list[StockClip], query: str) -> list[StockClip]:
    """Urutkan kandidat: kata spesifik lebih berbobot daripada kata umum."""
    return sorted(
        clips,
        key=lambda clip: (
            -_specific_overlap(clip, query),
            -_tag_overlap(clip, query),
            clip.clip_id,
        ),
    )


def _load_storyboard(ctx: StageContext) -> dict[str, Any]:
    record = ctx.repo.get_stage(ctx.job.id, "scenes")
    candidate: Optional[Path] = None
    if record and record.artifacts.get("storyboard"):
        path = Path(record.artifacts["storyboard"])
        if path.exists():
            candidate = path
    if candidate is None:
        fallback = ctx.artifacts_dir / "storyboard.json"
        candidate = fallback if fallback.exists() else None
    if candidate is None:
        raise RuntimeError("storyboard.json tidak ditemukan; jalankan stage scenes lebih dulu")
    return json.loads(candidate.read_text(encoding="utf-8"))


def _render_assets_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Aset visual", "", f"Ringkasan: {summary['counts']}", "", "| # | jenis | sumber | lisensi | atribusi |",
             "|---|---|---|---|---|"]
    for item in summary["assets"]:
        lines.append(
            f"| {item.get('scene_index')} | {item['kind']} | {item['source']} | {item['license']} | "
            f"{item.get('attribution') or '-'} |"
        )
    if summary["notes"]:
        lines += ["", "## Catatan"] + [f"- {note}" for note in summary["notes"]]
    return "\n".join(lines) + "\n"


def _render_relevance_markdown(relevance: dict[str, Any]) -> str:
    lines = [
        "# Relevansi visual (heuristik + vision opsional)",
        "",
        f"- Scene dinilai: {relevance['scenes_scored']}",
        f"- Skor rata-rata: {relevance['average_score'] if relevance['average_score'] is not None else '-'} / 10",
        f"- Ambang: {relevance['threshold']}",
        f"- Metode: {relevance['methods']}",
        f"- Perlu tinjauan manusia: {relevance['needs_human_review'] or 'tidak ada'}",
        "",
        "| scene | aset | skor | metode | alasan |",
        "|---|---|---|---|---|",
    ]
    for entry in relevance.get("entries") or []:
        item = entry.get("relevance") or {}
        reason = "; ".join(item.get("reasons") or []) or item.get("reason") or "-"
        lines.append(
            f"| {entry.get('scene_id') or entry.get('scene_index')} | {entry.get('asset_kind')} | "
            f"{item.get('score', '-')} | {item.get('method', '-')} | {reason[:160]} |"
        )
    lines += [
        "",
        "Catatan: skor ini adalah heuristik yang bisa diperiksa, bukan penilaian estetika objektif.",
    ]
    return "\n".join(lines) + "\n"
