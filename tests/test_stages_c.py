"""Test stage Fase C: research, script, review, scenes (offline, pakai mock)."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from shortsgen.config import Config, load_config
from shortsgen.logging_setup import get_logger
from shortsgen.models import StageStatus
from shortsgen.pipeline.orchestrator import StageContext
from shortsgen.pipeline.stages.research import ResearchStage
from shortsgen.pipeline.stages.review import ReviewStage
from shortsgen.pipeline.stages.scenes import ScenePlannerStage
from shortsgen.pipeline.stages.script import ScriptStage, normalize_script
from shortsgen.providers import build_providers
from shortsgen.providers.base import HealthStatus, ProviderBundle, ProviderHealth
from shortsgen.providers.mock import MOCK_RESEARCH_RESPONSE, MOCK_SCRIPT_RESPONSE, MockLLM
from shortsgen.repository import JobRepository

LOGGER = get_logger("test.stages")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_ctx(repo: JobRepository, config: Config, providers: ProviderBundle, job_id: str) -> StageContext:
    return StageContext(
        job=repo.get_job(job_id),
        config=config,
        providers=providers,
        repo=repo,
        logger=LOGGER,
        artifacts_dir=config.jobs_dir / job_id / "artifacts",
    )


def run_stage(repo: JobRepository, config: Config, providers: ProviderBundle, job_id: str, handler: Any) -> dict[str, Any]:
    """Menjalankan satu handler seperti orkestrator (start -> run -> finish)."""
    ctx = make_ctx(repo, config, providers, job_id)
    repo.start_stage(job_id, handler.stage)
    try:
        artifacts = handler.run(ctx) or {}
    except Exception:
        repo.finish_stage(job_id, handler.stage, StageStatus.FAILED)
        raise
    repo.finish_stage(job_id, handler.stage, StageStatus.DONE, artifacts=artifacts)
    return artifacts


def artifact_json(ctx_artifacts_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((ctx_artifacts_dir / name).read_text(encoding="utf-8"))


class StubStockProvider:
    """Provider stok palsu untuk menguji prioritas strategi visual."""

    name = "stub-stock"

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="stub-stock", capability="stock", status=HealthStatus.AVAILABLE, detail="stub"
        )

    def search(self, query: str, *, max_results: int = 3) -> list[Any]:
        return []


@pytest.fixture()
def mock_providers(mock_config: Config) -> ProviderBundle:
    return build_providers(mock_config)


@pytest.fixture()
def job_with_research(repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle) -> str:
    job = repo.create_job("Mengapa langit Mars berbeda?", "mock")
    run_stage(repo, mock_config, mock_providers, job.id, ResearchStage(mock_providers, mock_config, logger=LOGGER))
    return job.id


@pytest.fixture()
def job_with_script(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_research: str
) -> str:
    run_stage(repo, mock_config, mock_providers, job_with_research, ScriptStage(mock_providers, mock_config, logger=LOGGER))
    return job_with_research


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

def test_research_stage_outputs_facts_and_marks_unverified(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle
) -> None:
    job = repo.create_job("Topik riset", "mock")
    artifacts = run_stage(
        repo, mock_config, mock_providers, job.id, ResearchStage(mock_providers, mock_config, logger=LOGGER)
    )

    assert artifacts["facts_count"] == len(MOCK_RESEARCH_RESPONSE["facts"])
    data = artifact_json(mock_config.jobs_dir / job.id / "artifacts", "research.json")
    facts = data["facts"]
    # tidak ada URL dari model -> semuanya jujur ditandai "no_source"
    assert all(f["verification"] == "no_source" for f in facts)
    assert all(f["confidence"] in ("high", "medium", "low") for f in facts)
    # catatan bahwa search/fetch tidak bisa dipakai sebagai bukti
    assert any("sintetis" in note.lower() or "mock" in note.lower() for note in data["search_notes"])
    # hasil search mock TIDAK dianggap bukti
    assert artifacts["unverified_count"] == len(facts)

    # usage tercatat: llm + search
    usage = repo.usage_summary(job.id)
    assert "mock:llm" in usage["by_provider"]
    assert "mock:search" in usage["by_provider"]


def test_research_rejects_non_json(repo: JobRepository, mock_config: Config) -> None:
    providers = build_providers(mock_config)
    providers.llm = MockLLM(fixed_response="maaf, saya tidak bisa membuat JSON")
    job = repo.create_job("Topik json rusak", "mock")
    with pytest.raises(RuntimeError):
        run_stage(repo, mock_config, providers, job.id, ResearchStage(providers, mock_config, logger=LOGGER))


def test_research_skips_search_when_not_configured(
    repo: JobRepository, mock_config: Config
) -> None:
    """Provider search yang ada tapi not_configured tidak boleh dipanggil/tercatat."""
    from shortsgen.providers.ninerouter.client import NineRouterClient
    from shortsgen.providers.ninerouter.search import SearchAdapter

    providers = build_providers(mock_config)
    client = NineRouterClient("http://127.0.0.1:20128", "sk-dummy-1234567890")
    providers.search = SearchAdapter(client, "")  # provider kosong -> not_configured
    job = repo.create_job("Topik tanpa search", "mock")
    artifacts = run_stage(
        repo, mock_config, providers, job.id, ResearchStage(providers, mock_config, logger=LOGGER)
    )
    assert artifacts["search_candidates"] == 0
    assert "mock:search" not in repo.usage_summary(job.id)["by_provider"]
    data = artifact_json(mock_config.jobs_dir / job.id / "artifacts", "research.json")
    assert any("belum dikonfigurasi" in note for note in data["search_notes"])


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------

def test_script_stage_normalizes_blocks(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_research: str
) -> None:
    run_stage(repo, mock_config, mock_providers, job_with_research, ScriptStage(mock_providers, mock_config, logger=LOGGER))
    script = artifact_json(mock_config.jobs_dir / job_with_research / "artifacts", "script.json")

    kinds = [b["kind"] for b in script["blocks"]]
    assert kinds[0] == "hook" and kinds[-1] == "closing"
    assert kinds.count("body") == len(MOCK_SCRIPT_RESPONSE["scenes"])
    assert script["unknown_fact_refs"] == []
    assert script["stats"]["total_words"] > 50
    # durasi estimasi masuk rentang target ± 30% (target default kini 30 dtk)
    low = mock_config.target_duration_s * 0.7
    high = mock_config.target_duration_s * 1.3
    assert low <= script["stats"]["estimated_duration_s"] <= high
    assert len(script["tags"]) >= 5
    assert script["disclosure"]
    # perangkat retensi baru
    assert script["hook_variants"] and script["hook_variants"][0] == script["hook"]
    assert script["stats"]["has_rhetorical_question"] is True
    assert script["stats"]["hook_words"] <= 12


def test_script_stage_repairs_invalid_json_once(
    repo: JobRepository, mock_config: Config, job_with_research: str
) -> None:
    providers = build_providers(mock_config)
    good = json.dumps(MOCK_SCRIPT_RESPONSE, ensure_ascii=False)
    providers.llm = MockLLM(responses=["tentu, ini naskahnya (bukan json)", good])
    run_stage(repo, mock_config, providers, job_with_research, ScriptStage(providers, mock_config, logger=LOGGER))
    assert len(providers.llm.calls) == 2  # satu gagal parse -> satu perbaikan


def test_script_stage_fails_after_two_invalid_json(
    repo: JobRepository, mock_config: Config, job_with_research: str
) -> None:
    providers = build_providers(mock_config)
    providers.llm = MockLLM(fixed_response="tetap bukan json")
    with pytest.raises(RuntimeError):
        run_stage(repo, mock_config, providers, job_with_research, ScriptStage(providers, mock_config, logger=LOGGER))


def test_normalize_script_rejects_empty_scenes() -> None:
    with pytest.raises(RuntimeError):
        normalize_script({"hook": "halo", "scenes": []}, [])


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

CLICKBAIT_SCRIPT = {
    "title": "SHOCKING!!! JANGAN KAGET LANGIT MARS",
    "hook": "TIDAK AKAN PERCAYA apa yang terjadi di langit Mars!!!",
    "scenes": [
        {"narration": "Ini fakta yang membuat semua orang heboh dan tidak disangka.", "visual_prompt": "v"},
        {"narration": "Para ahli sangat terkejut dengan penemuan ini.", "visual_prompt": "v"},
    ],
    "closing": "Bagaimana menurutmu?",
    "description": "d",
    "tags": ["mars"],
    "facts_used": ["F1"],
}


def test_review_passes_clean_script(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_script: str
) -> None:
    artifacts = run_stage(repo, mock_config, mock_providers, job_with_script, ReviewStage(mock_providers, mock_config, logger=LOGGER))
    assert artifacts["passed"] is True
    report = artifact_json(mock_config.jobs_dir / job_with_script / "artifacts", "review.json")
    # fakta belum terverifikasi -> temuan ringan (low), bukan pemblokir
    codes = {i["code"]: i["severity"] for i in report["issues"]}
    assert codes.get("unverified_claims") == "low"
    assert report["blocking_count"] == 0


def test_review_blocks_clickbait_and_llm_revision_fixes_it(
    repo: JobRepository, mock_config: Config, job_with_research: str
) -> None:
    providers = build_providers(mock_config)
    bad = json.dumps(CLICKBAIT_SCRIPT, ensure_ascii=False)
    good = json.dumps(MOCK_SCRIPT_RESPONSE, ensure_ascii=False)
    # 1) naskah buruk masuk lewat stage script, 2) revisi mengembalikan naskah bersih
    providers.llm = MockLLM(responses=[bad, good])
    run_stage(repo, mock_config, providers, job_with_research, ScriptStage(providers, mock_config, logger=LOGGER))
    artifacts = run_stage(repo, mock_config, providers, job_with_research, ReviewStage(providers, mock_config, logger=LOGGER))

    assert artifacts["passed"] is True
    assert artifacts["revision_attempted"] is True
    report = artifact_json(mock_config.jobs_dir / job_with_research / "artifacts", "review.json")
    assert report["blocking_count"] == 0
    # naskah lama disimpan untuk audit
    assert (mock_config.jobs_dir / job_with_research / "artifacts" / "script_pre_review.json").exists()


def test_review_fails_when_revision_impossible(
    repo: JobRepository, mock_config: Config, job_with_research: str
) -> None:
    providers = build_providers(mock_config)
    bad = json.dumps(CLICKBAIT_SCRIPT, ensure_ascii=False)
    providers.llm = MockLLM(responses=[bad, bad, bad])  # naskah + revisi tetap buruk
    run_stage(repo, mock_config, providers, job_with_research, ScriptStage(providers, mock_config, logger=LOGGER))
    with pytest.raises(RuntimeError) as exc_info:
        run_stage(repo, mock_config, providers, job_with_research, ReviewStage(providers, mock_config, logger=LOGGER))
    assert "review" in str(exc_info.value).lower()


def test_review_blocks_unverified_when_configured(
    repo: JobRepository, tmp_path: Path, mock_providers: ProviderBundle
) -> None:
    strict = load_config(
        environ={
            "APP_MODE": "mock",
            "BLOCK_UNVERIFIED_CLAIMS": "true",
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    job = repo.create_job("Topik strict", "mock")
    run_stage(repo, strict, mock_providers, job.id, ResearchStage(mock_providers, strict, logger=LOGGER))
    run_stage(repo, strict, mock_providers, job.id, ScriptStage(mock_providers, strict, logger=LOGGER))
    with pytest.raises(RuntimeError):
        run_stage(repo, strict, mock_providers, job.id, ReviewStage(mock_providers, strict, logger=LOGGER))


# ---------------------------------------------------------------------------
# Scenes / storyboard
# ---------------------------------------------------------------------------

def test_scene_planner_builds_timeline_and_ai_image_plan(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_script: str
) -> None:
    artifacts = run_stage(
        repo, mock_config, mock_providers, job_with_script, ScenePlannerStage(mock_providers, mock_config, logger=LOGGER)
    )
    board = artifact_json(mock_config.jobs_dir / job_with_script / "artifacts", "storyboard.json")
    scenes = board["scenes"]
    assert artifacts["scenes"] == len(scenes) >= 8
    # timeline monoton dan berurutan
    for previous, current in zip(scenes, scenes[1:]):
        assert current["start_s"] >= previous["end_s"]
    assert scenes[0]["start_s"] == 0.0
    # image mock tersedia -> strategi ai_image sampai anggaran, sisanya prosedural
    budget = mock_config.max_ai_images_per_job
    ai_scenes = [s for s in scenes if s["visual_strategy"] == "ai_image"]
    procedural_scenes = [s for s in scenes if s["visual_strategy"] == "procedural"]
    assert len(ai_scenes) == min(len(scenes), budget)
    if len(scenes) > budget:
        assert procedural_scenes
        assert any("anggaran" in s["strategy_reason"] for s in procedural_scenes)
    assert board["visual_plan"]["ai_images_planned"] == len(ai_scenes)
    assert board["timeline"]["total_seconds"] > 0
    # kueri stok konkret (bukan kata abstrak) untuk setiap scene
    for scene in scenes:
        query = scene["stock_query"]
        assert query and len(query.split()) <= 4
        assert not (set(query.lower().split()) & {"mysterious", "cinematic", "concept", "diagram"})


def test_scene_planner_falls_back_when_no_image_provider(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_script: str
) -> None:
    providers = dataclasses.replace(mock_providers, image=None)
    run_stage(repo, mock_config, providers, job_with_script, ScenePlannerStage(providers, mock_config, logger=LOGGER))
    board = artifact_json(mock_config.jobs_dir / job_with_script / "artifacts", "storyboard.json")
    assert all(s["visual_strategy"] == "procedural" for s in board["scenes"])


def test_scene_planner_respects_ai_image_budget(
    repo: JobRepository, tmp_path: Path, mock_providers: ProviderBundle, job_with_script: str
) -> None:
    limited = load_config(
        environ={
            "APP_MODE": "mock",
            "MAX_AI_IMAGES_PER_JOB": "2",
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    run_stage(repo, limited, mock_providers, job_with_script, ScenePlannerStage(mock_providers, limited, logger=LOGGER))
    board = artifact_json(limited.jobs_dir / job_with_script / "artifacts", "storyboard.json")
    strategies = [s["visual_strategy"] for s in board["scenes"]]
    assert strategies.count("ai_image") == 2
    assert "procedural" in strategies
    assert any("anggaran" in s["strategy_reason"] for s in board["scenes"] if s["visual_strategy"] == "procedural")


def test_scene_planner_limits_stock_to_relevant_body_scenes(
    repo: JobRepository, mock_config: Config, mock_providers: ProviderBundle, job_with_script: str
) -> None:
    """Refactor: AI image utama; stok hanya untuk context/evidence dengan kueri kuat."""
    providers = dataclasses.replace(mock_providers, stock=StubStockProvider())
    run_stage(repo, mock_config, providers, job_with_script, ScenePlannerStage(providers, mock_config, logger=LOGGER))
    board = artifact_json(mock_config.jobs_dir / job_with_script / "artifacts", "storyboard.json")
    scenes = board["scenes"]
    stock_scenes = [scene for scene in scenes if scene["visual_strategy"] == "stock_video"]
    ai_scenes = [scene for scene in scenes if scene["visual_strategy"] == "ai_image"]
    assert stock_scenes, "setidaknya satu scene context/evidence memakai stok relevan"
    assert len(stock_scenes) <= 2, "stok dibatasi agar video tidak jadi kolase footage"
    assert all(scene["role"] in ("context", "evidence") for scene in stock_scenes)
    assert all(len(scene["stock_query"].split()) >= 2 for scene in stock_scenes)
    assert ai_scenes, "gambar AI tetap menjadi mayoritas"
    assert all(
        scene["visual_strategy"] != "stock_video"
        for scene in scenes
        if scene["role"] in ("hook", "twist", "payoff", "closing")
    )
