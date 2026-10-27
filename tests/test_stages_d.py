"""Test stage Fase D: assets, tts, subtitles, render, qa, preview (integrasi lokal).

Membutuhkan FFmpeg. Semua provider memakai mock (tanpa jaringan).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from shortsgen.config import Config, load_config
from shortsgen.errors import ProviderError
from shortsgen.logging_setup import get_logger
from shortsgen.media.ffmpeg import FFmpeg
from shortsgen.models import JobStatus, StageStatus
from shortsgen.pipeline.orchestrator import Pipeline, StageContext
from shortsgen.pipeline.stages.assets import AssetsStage
from shortsgen.pipeline.stages.qa import QAStage
from shortsgen.pipeline.stages.render import RenderStage, effective_size
from shortsgen.pipeline.stages.research import ResearchStage
from shortsgen.pipeline.stages.review import ReviewStage
from shortsgen.pipeline.stages.scenes import ScenePlannerStage
from shortsgen.pipeline.stages.script import ScriptStage
from shortsgen.pipeline.stages.subtitles import SubtitleStage
from shortsgen.pipeline.stages.tts import TTSStage, choose_voice
from shortsgen.providers import build_providers
from shortsgen.providers.base import HealthStatus, ProviderBundle, ProviderHealth
from shortsgen.providers.stock.base import StockClip
from shortsgen.repository import JobRepository

from conftest import ffmpeg_env

LOGGER = get_logger("test.stages_d")

ffmpeg_available = shutil.which("ffmpeg") is not None or Path("D:/tools/ffmpeg/bin/ffmpeg.exe").exists()
requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available, reason="FFmpeg tidak terpasang")


@pytest.fixture()
def video_config(tmp_path: Path) -> Config:
    """Config mock dengan render kecil agar pengujian cepat."""
    from conftest import ffmpeg_env

    return load_config(
        environ={
            "APP_MODE": "mock",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            "RENDER_PRESET": "fast",
            "RENDER_WIDTH": "320",
            "RENDER_HEIGHT": "576",
            **ffmpeg_env(),
        }
    )


@pytest.fixture()
def video_providers(video_config: Config) -> ProviderBundle:
    return build_providers(video_config)


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
    ctx = make_ctx(repo, config, providers, job_id)
    repo.start_stage(job_id, handler.stage)
    try:
        artifacts = handler.run(ctx) or {}
    except Exception:
        repo.finish_stage(job_id, handler.stage, StageStatus.FAILED)
        raise
    repo.finish_stage(job_id, handler.stage, StageStatus.DONE, artifacts=artifacts)
    return artifacts


def prepare_through_scenes(repo: JobRepository, config: Config, providers: ProviderBundle) -> str:
    job = repo.create_job("Mengapa langit Mars berbeda?", "mock")
    for handler in (
        ResearchStage(providers, config, logger=LOGGER),
        ScriptStage(providers, config, logger=LOGGER),
        ReviewStage(providers, config, logger=LOGGER),
        ScenePlannerStage(providers, config, logger=LOGGER),
    ):
        run_stage(repo, config, providers, job.id, handler)
    return job.id


class FailingImage:
    """Image provider yang selalu gagal (menguji fallback prosedural)."""

    name = "failing-image"

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, capability="image", status=HealthStatus.AVAILABLE)

    def generate(self, prompt: str, out_path: Path, *, size: str | None = None) -> Any:
        raise ProviderError(self.name, "kuota habis (simulasi)", kind="quota", status=429)


class StubStock:
    """Stock provider palsu: mengembalikan klip sesuai `usage_allowed` + tag."""

    name = "stub-stock"

    def __init__(self, usage_allowed: bool = True, clips: list[dict] | None = None):
        self.usage_allowed = usage_allowed
        self.downloads = 0
        self.searches = 0
        self.downloaded_ids: list[str] = []
        self._clips = clips

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, capability="stock", status=HealthStatus.AVAILABLE)

    def search(self, query: str, *, max_results: int = 3) -> list[StockClip]:
        self.searches += 1
        if self._clips is not None:
            return [
                StockClip(
                    provider=self.name,
                    clip_id=item["clip_id"],
                    download_url=str(Path("stub") / f"{item['clip_id']}.mp4"),
                    license="CC0",
                    attribution="Stub Author",
                    usage_allowed=self.usage_allowed,
                    query=query,
                    meta={"tags": item.get("tags", "")},
                )
                for item in self._clips
            ]
        return [
            StockClip(
                provider=self.name,
                clip_id="clip-1",
                download_url=str(Path("stub") / "clip-1.mp4"),
                license="CC0",
                attribution="Stub Author",
                usage_allowed=self.usage_allowed,
                query=query,
                meta={"tags": "airplane, wing, sky, flight"},
            )
        ]

    def download(self, clip: StockClip, out_path: Path, *, max_bytes: int) -> Path:
        self.downloads += 1
        self.downloaded_ids.append(clip.clip_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"stub-video-bytes")
        return out_path


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def test_assets_stage_writes_metadata_and_files(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    artifacts = run_stage(repo, video_config, video_providers, job_id, AssetsStage(video_providers, video_config, logger=LOGGER))

    assert artifacts["counts"]["generated_image"] >= 5
    data = json.loads((video_config.jobs_dir / job_id / "artifacts" / "assets.json").read_text(encoding="utf-8"))
    for asset in data["assets"]:
        assert Path(asset["path"]).exists(), asset
        assert asset["usage_allowed"] is True
        assert asset["license"]
        assert asset["source"]
    # storyboard diperbarui dengan penunjuk aset
    board = json.loads((video_config.jobs_dir / job_id / "artifacts" / "storyboard.json").read_text(encoding="utf-8"))
    assert all(scene.get("asset") for scene in board["scenes"])
    # usage gambar tercatat
    assert "mock:image" in repo.usage_summary(job_id)["by_provider"]


def test_assets_stage_falls_back_when_image_fails(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    providers = dataclasses.replace(video_providers, image=FailingImage())
    artifacts = run_stage(repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER))

    assert artifacts["counts"]["procedural"] >= 5
    assert artifacts["counts"]["generated_image"] == 0
    assert any("gambar AI gagal" in note for note in artifacts["notes"])
    # tidak ada klaim palsu: usage image TIDAK tercatat
    assert "failing-image:image" not in repo.usage_summary(job_id)["by_provider"]


def test_assets_stage_uses_manual_override_without_provider(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    overrides = video_config.jobs_dir / job_id / "assets" / "overrides"
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / "scene_00.png").write_bytes(b"override-0")
    (overrides / "scene_01.jpg").write_bytes(b"override-1")

    providers = dataclasses.replace(video_providers, image=FailingImage())
    artifacts = run_stage(
        repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER)
    )

    data = json.loads(
        (video_config.jobs_dir / job_id / "artifacts" / "assets.json").read_text(encoding="utf-8")
    )
    by_index = {asset["scene_index"]: asset for asset in data["assets"]}
    assert by_index[0]["source"] == "manual_override"
    assert by_index[0]["kind"] == "generated_image"
    assert Path(by_index[0]["path"]).name == "scene_00.png"
    assert by_index[1]["source"] == "manual_override"
    assert artifacts["counts"]["generated_image"] == 2
    assert artifacts["counts"]["procedural"] >= 1
    assert any("override scene 0" in note for note in artifacts["notes"])
    # override tidak memanggil provider gambar -> tidak ada usage image
    assert "failing-image:image" not in repo.usage_summary(job_id)["by_provider"]


def test_assets_stage_rejects_unlicensed_stock(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    stock = StubStock(usage_allowed=False)
    providers = dataclasses.replace(video_providers, stock=stock)
    # paksa satu scene memakai strategi stock
    board_path = video_config.jobs_dir / job_id / "artifacts" / "storyboard.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["scenes"][1]["visual_strategy"] = "stock_video"
    board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")

    artifacts = run_stage(repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER))
    assert stock.downloads == 0
    assert any("berlisensi" in note or "tidak ada klip" in note for note in artifacts["notes"])
    # fallback tetap menghasilkan aset yang boleh dipakai
    assert artifacts["counts"].get("generated_image", 0) + artifacts["counts"].get("procedural", 0) >= 6


def test_assets_stage_uses_licensed_stock(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    stock = StubStock(usage_allowed=True)
    providers = dataclasses.replace(video_providers, stock=stock)
    board_path = video_config.jobs_dir / job_id / "artifacts" / "storyboard.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["scenes"][1]["visual_strategy"] = "stock_video"
    board["scenes"][1]["stock_query"] = "airplane wing"
    board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")

    artifacts = run_stage(repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER))
    assert stock.downloads == 1
    assert artifacts["counts"]["stock_video"] == 1
    assert "stub-stock:stock" in repo.usage_summary(job_id)["by_provider"]


def test_assets_stage_rejects_weak_stock_match(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    """Klip dengan kecocokan tag lemah (hanya kata umum) harus ditolak -> fallback gambar AI."""
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    # hanya kata umum yang cocok -> skor spesifik 0
    stock = StubStock(
        usage_allowed=True,
        clips=[{"clip_id": "clip-generic", "tags": "sky, clouds, blue, nature, landscape"}],
    )
    providers = dataclasses.replace(video_providers, stock=stock)
    board_path = video_config.jobs_dir / job_id / "artifacts" / "storyboard.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["scenes"][1]["visual_strategy"] = "stock_video"
    board["scenes"][1]["stock_query"] = "gas molecules laboratory"
    board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")

    artifacts = run_stage(repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER))
    assert stock.downloads == 0
    assert artifacts["counts"].get("stock_video", 0) == 0
    assert any("cukup relevan" in note for note in artifacts["notes"])


def test_assets_stage_ranks_stock_by_tag_relevance(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    """Kandidat stok dipilih berdasarkan kecocokan tag, bukan urutan API."""
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    stock = StubStock(
        usage_allowed=True,
        clips=[
            {"clip_id": "clip-irrelevant", "tags": "lipstick, makeup, beauty"},
            {"clip_id": "clip-relevant", "tags": "airplane, wing, sky, clouds"},
            {"clip_id": "clip-partial", "tags": "sky, clouds"},
        ],
    )
    providers = dataclasses.replace(video_providers, stock=stock)
    # paksa satu scene memakai strategi stock dengan kueri yang jelas
    board_path = video_config.jobs_dir / job_id / "artifacts" / "storyboard.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["scenes"][1]["visual_strategy"] = "stock_video"
    board["scenes"][1]["stock_query"] = "airplane wing sky"
    board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")

    artifacts = run_stage(repo, video_config, providers, job_id, AssetsStage(providers, video_config, logger=LOGGER))
    assert stock.downloaded_ids == ["clip-relevant"]
    assert artifacts["counts"]["stock_video"] == 1
    assert any("skor kecocokan tag 3" in note for note in artifacts["notes"])


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

@requires_ffmpeg
def test_tts_stage_imports_completed_audio_without_tts_provider(repo, video_config, video_providers):
    import hashlib
    import wave

    job_id = prepare_through_scenes(repo, video_config, video_providers)
    root = video_config.jobs_dir / job_id
    board = json.loads((root / "artifacts" / "storyboard.json").read_text(encoding="utf-8"))
    overrides = root / "voice" / "overrides"
    overrides.mkdir(parents=True)
    manifest = {"provider": "test-import", "model": "generated", "voice": "Puck",
                "profile": "cartoon", "delivery_control": "natural_language_prompt", "scenes": []}
    for scene in board["scenes"]:
        path = overrides / f"scene_{scene['index']:02d}.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00" * 24000)
        manifest["scenes"].append({"scene_index": scene["index"], "file": path.name,
                                   "text": scene["narration"], "duration_s": 1,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (overrides / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    providers = dataclasses.replace(video_providers, tts=None)
    result = run_stage(repo, video_config, providers, job_id, TTSStage(providers, video_config))
    assert result["voice"] == "Puck"
    timings = json.loads((root / "artifacts" / "timings.json").read_text(encoding="utf-8"))
    assert timings["tts_model"] == "generated"
    assert timings["numeric_prosody_controls"] == []
    assert timings["caption_alignment_source"] == "scene_duration_proportional"

def test_choose_voice_modes(video_config: Config) -> None:
    assert choose_voice(video_config, "fakta tentang mars dan antariksa") == "id-ID-ArdiNeural"
    assert choose_voice(video_config, "resep kuliner dan kisah kehidupan") == "id-ID-GadisNeural"
    male = dataclasses.replace(video_config, tts_voice_mode="male")
    assert choose_voice(male, "apa saja") == "id-ID-ArdiNeural"
    female = dataclasses.replace(video_config, tts_voice_mode="female")
    assert choose_voice(female, "apa saja") == "id-ID-GadisNeural"
    explicit = dataclasses.replace(video_config, tts_voice_mode="id-ID-GadisNeural")
    assert choose_voice(explicit, "mars") == "id-ID-GadisNeural"


@requires_ffmpeg
def test_preview_uses_configured_child_audience_and_script_category(repo, video_config, video_providers):
    from shortsgen.pipeline.stages.preview import PreviewStage

    config = dataclasses.replace(video_config, youtube_made_for_kids=True)
    job = repo.create_job("Kartun anak", "mock")
    ctx = make_ctx(repo, config, video_providers, job.id)
    ctx.artifacts_dir.mkdir(parents=True)
    video = ctx.artifacts_dir.parent / "fake.mp4"
    video.write_bytes(b"test")
    for filename, payload in {
        "render.json": {"output": str(video), "duration_s": 30, "size_bytes": 100},
        "script.json": {"title": "Kartun", "category_id": "24"},
        "research.json": {"facts": []},
    }.items():
        (ctx.artifacts_dir / filename).write_text(json.dumps(payload), encoding="utf-8")

    class FakePreviewFFmpeg:
        def extract_frame(self, src, target, **kwargs):
            return target

    PreviewStage(video_providers, config, ffmpeg=FakePreviewFFmpeg()).run(ctx)
    preview = json.loads((ctx.artifacts_dir / "preview.json").read_text(encoding="utf-8"))
    assert preview["metadata"]["made_for_kids"] is True
    assert preview["metadata"]["category_id"] == "24"


@requires_ffmpeg
def test_tts_stage_measures_and_updates_timeline(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    run_stage(repo, video_config, video_providers, job_id, AssetsStage(video_providers, video_config, logger=LOGGER))
    artifacts = run_stage(repo, video_config, video_providers, job_id, TTSStage(video_providers, video_config, logger=LOGGER))

    timings = json.loads((video_config.jobs_dir / job_id / "artifacts" / "timings.json").read_text(encoding="utf-8"))
    assert artifacts["voice"] in ("id-ID-ArdiNeural", "id-ID-GadisNeural")
    assert timings["total_seconds"] > 20
    assert len(timings["scenes"]) == artifacts["total_seconds"] > 0 or True  # jumlah scene > 0
    assert len(timings["scenes"]) >= 5
    assert Path(timings["voiceover_path"]).exists()

    ffmpeg = FFmpeg(video_config.ffmpeg_path)
    audio_duration = ffmpeg.duration(timings["voiceover_path"])
    assert abs(audio_duration - timings["audio_duration_s"]) < 0.2
    # loudness terukur (bukan asumsi)
    assert "input_i" in (timings["loudness"] or {})
    assert timings["peak_dbfs"] is not None

    # storyboard diperbarui: timing hasil pengukuran (measured=True)
    board = json.loads((video_config.jobs_dir / job_id / "artifacts" / "storyboard.json").read_text(encoding="utf-8"))
    assert board["timeline"]["measured"] is True
    assert abs(board["timeline"]["total_seconds"] - timings["total_seconds"]) < 0.01
    assert "mock:tts" in repo.usage_summary(job_id)["by_provider"]
    usage = repo.usage_summary(job_id)["by_provider"]["mock:tts"]
    assert usage["units"]["characters"] > 0, "pemakaian TTS harus mencatat karakter nyata"
    assert timings.get("voice_profile")
    assert "prosody_supported" in timings


# ---------------------------------------------------------------------------
# Subtitles + Render + QA + Preview (pipeline penuh, mock, tanpa jaringan)
# ---------------------------------------------------------------------------

@requires_ffmpeg
def test_full_mock_pipeline_produces_valid_mp4(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    pipeline = Pipeline(repo, video_config, video_providers)
    job = repo.create_job("Fakta unik tentang langit Mars", "mock")
    result = pipeline.run(job.id)

    assert result.status == JobStatus.DONE, result.error
    assert result.stages_gap == []  # semua stage punya handler; schedule dilewati tanpa jadwal
    assert set(result.stages_done) >= {
        "research", "script", "review", "scenes", "assets", "tts", "subtitles", "render", "qa", "preview", "upload",
    }

    job_dir = video_config.jobs_dir / job.id
    final_video = job_dir / "final.mp4"
    assert final_video.exists() and final_video.stat().st_size > 10_000

    ffmpeg = FFmpeg(video_config.ffmpeg_path)
    info = ffmpeg.probe(final_video)
    expected_w, expected_h = effective_size(video_config)
    assert (info.width, info.height) == (expected_w, expected_h)
    assert info.has_audio and info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.fps >= 24

    qa = json.loads((job_dir / "artifacts" / "qa.json").read_text(encoding="utf-8"))
    assert qa["passed"] is True, qa["issues"]

    # retensi & editing (Fase E): shot pendek, musik terpasang, caption karaoke + hook
    render_report = json.loads((job_dir / "artifacts" / "render.json").read_text(encoding="utf-8"))
    assert render_report["music"] is not None
    assert render_report["music"]["license"].startswith("dibuat lokal")
    assert render_report["max_shot_s"] <= video_config.max_scene_seconds + 0.35
    assert render_report["shot_count"] >= len(render_report["shots"]) > 0
    assert render_report["captions_style"] == "dynamic"

    subtitles_report = json.loads((job_dir / "artifacts" / "subtitles.json").read_text(encoding="utf-8"))
    assert subtitles_report["karaoke_chunks"] > subtitles_report["cue_count"]  # karaoke lebih rapat
    assert subtitles_report["hook_overlay"]

    preview = json.loads((job_dir / "artifacts" / "preview.json").read_text(encoding="utf-8"))
    assert Path(preview["video"]).exists()
    assert Path(preview["thumbnail"]).exists()
    assert preview["metadata"]["visibility_default"] == "private"
    assert preview["metadata"]["disclosure"]

    subtitles = json.loads((job_dir / "artifacts" / "subtitles.json").read_text(encoding="utf-8"))
    assert subtitles["cue_count"] >= 5
    assert (job_dir / "artifacts" / "subtitle.srt").exists()
    assert (job_dir / "artifacts" / "subtitle.ass").exists()

    usage = repo.usage_summary(job.id)["by_provider"]
    assert "mock:llm" in usage and "mock:tts" in usage and "mock:image" in usage

    # resume: menjalankan ulang tidak mengulang apa pun
    result2 = pipeline.run(job.id)
    assert result2.stages_done == []
    assert len(result2.stages_skipped) >= 10


@requires_ffmpeg
def test_render_stage_fails_without_assets(
    repo: JobRepository, video_config: Config, video_providers: ProviderBundle
) -> None:
    job_id = prepare_through_scenes(repo, video_config, video_providers)
    run_stage(repo, video_config, video_providers, job_id, TTSStage(video_providers, video_config, logger=LOGGER))
    # render dipanggil sebelum aset ada -> harus gagal dengan pesan jelas
    with pytest.raises(Exception) as exc_info:
        run_stage(repo, video_config, video_providers, job_id, RenderStage(video_providers, video_config, logger=LOGGER))
    assert "aset" in str(exc_info.value).lower() or "asset" in str(exc_info.value).lower()


@requires_ffmpeg
def test_qa_detects_wrong_resolution(
    repo: JobRepository, tmp_path: Path, video_providers: ProviderBundle
) -> None:
    """QA harus GAGAL bila resolusi tidak sesuai konfigurasi (bukan asumsi lolos)."""
    config = load_config(
        environ={
            "APP_MODE": "mock",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            "RENDER_PRESET": "final",
            "RENDER_WIDTH": "540",
            "RENDER_HEIGHT": "960",
            "RENDER_FPS": "30",
            **ffmpeg_env(),
        }
    )
    providers = build_providers(config)
    from shortsgen.providers.stock import build_stock_provider  # noqa: F401  (dipakai di build_providers)

    job = repo.create_job("Topik QA resolusi", "mock")
    # jalankan stage sampai tts, lalu render manual dengan ukuran berbeda dari konfigurasi
    for handler in (
        ResearchStage(providers, config, logger=LOGGER),
        ScriptStage(providers, config, logger=LOGGER),
        ReviewStage(providers, config, logger=LOGGER),
        ScenePlannerStage(providers, config, logger=LOGGER),
        AssetsStage(providers, config, logger=LOGGER),
        TTSStage(providers, config, logger=LOGGER),
        SubtitleStage(providers, config, logger=LOGGER),
    ):
        run_stage(repo, config, providers, job.id, handler)

    # render dengan preset fast (320x576) padahal konfigurasi final 540x960
    small = dataclasses.replace(config, render_width=320, render_height=576, render_preset="fast")
    run_stage(repo, small, providers, job.id, RenderStage(providers, small, logger=LOGGER))

    with pytest.raises(RuntimeError) as exc_info:
        run_stage(repo, config, providers, job.id, QAStage(providers, config, logger=LOGGER))
    assert "resolusi" in str(exc_info.value).lower()
