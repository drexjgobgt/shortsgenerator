"""Test orkestrator pipeline: state machine, resume, gerbang approval, kuota disk."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from shortsgen.config import Config, load_config
from shortsgen.models import STAGE_ORDER, JobStatus, StageStatus
from shortsgen.pipeline.orchestrator import Pipeline
from shortsgen.providers import build_providers
from shortsgen.providers.base import ProviderBundle


class Recorder:
    """Handler uji yang merekam pemanggilan; bisa dikonfigurasi gagal N kali."""

    def __init__(self, stage: str, fail_times: int = 0):
        self.stage = stage
        self.fail_times = fail_times
        self.calls = 0

    def run(self, ctx: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"kegagalan buatan pada {self.stage}")
        return {"artifact": f"{self.stage}.json", "mock": True}


def make_handlers(**overrides: int) -> dict[str, Recorder]:
    return {stage: Recorder(stage, fail_times=overrides.get(stage, 0)) for stage in STAGE_ORDER}


@pytest.fixture()
def mock_bundle(mock_config: Config) -> ProviderBundle:
    return build_providers(mock_config)


def test_full_run_completes_all_stages(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = make_handlers()
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik Lengkap", "mock")

    result = pipeline.run(job.id)

    assert result.status == JobStatus.DONE
    assert result.stages_done == list(STAGE_ORDER)
    assert all(h.calls == 1 for h in handlers.values())
    stages = repo.get_stages(job.id)
    assert stages["render"].status == StageStatus.DONE
    assert stages["render"].artifacts["artifact"] == "render.json"


def test_second_run_resumes_and_skips_done_stages(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = make_handlers()
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik Resume", "mock")
    pipeline.run(job.id)

    second = make_handlers()
    pipeline2 = Pipeline(repo, mock_config, mock_bundle, second)
    result = pipeline2.run(job.id)

    assert result.status == JobStatus.DONE
    assert result.stages_done == []
    assert set(result.stages_skipped) == set(STAGE_ORDER)
    assert all(h.calls == 0 for h in second.values())


def test_cache_report_is_cumulative_across_runs(repo, mock_config: Config) -> None:
    import json

    class FakeProviders:
        def __init__(self) -> None:
            self.hits = 1

        def cache_stats(self) -> dict:
            return {"llm": {"enabled": True, "hits": self.hits, "misses": 0, "writes": self.hits}}

    providers = FakeProviders()
    pipeline = Pipeline(repo, mock_config, providers, make_handlers())  # type: ignore[arg-type]
    job = repo.create_job("Topik Cache", "mock")
    path = mock_config.jobs_dir / job.id / "artifacts" / "cache.json"

    pipeline.run(job.id)
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["runs"] == 1
    assert first["caches"]["llm"]["hits"] == 1

    providers.hits = 2  # run kedua (semua stage di-skip) menambah statistik
    pipeline.run(job.id)
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["runs"] == 2
    assert second["caches"]["llm"]["hits"] == 3
    assert second["caches"]["llm"]["writes"] == 3


def test_failure_stops_pipeline_and_resume_continues(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = make_handlers(scenes=1)
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik Gagal", "mock")

    result = pipeline.run(job.id)
    assert result.status == JobStatus.FAILED
    assert result.failed_stage == "scenes"
    assert "kegagalan buatan" in (result.error or "")
    assert repo.get_job(job.id).status == JobStatus.FAILED
    assert repo.get_stage(job.id, "research").status == StageStatus.DONE
    assert repo.get_stage(job.id, "scenes").status == StageStatus.FAILED
    assert repo.get_stage(job.id, "render") is None

    # perbaikan: jalankan ulang -> hanya lanjut dari stage yang gagal (bukan dari awal)
    retry_handlers = make_handlers()
    pipeline2 = Pipeline(repo, mock_config, mock_bundle, retry_handlers)
    result2 = pipeline2.run(job.id)

    assert result2.status == JobStatus.DONE
    assert retry_handlers["research"].calls == 0   # tidak diulang
    assert retry_handlers["script"].calls == 0
    assert retry_handlers["review"].calls == 0
    assert retry_handlers["scenes"].calls == 1     # dilanjutkan
    assert retry_handlers["render"].calls == 1


def test_retry_failed_flag_disabled_stops(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = make_handlers(scenes=1)
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik NoRetry", "mock")
    pipeline.run(job.id)

    result = pipeline.run(job.id, retry_failed=False)
    assert result.status == JobStatus.FAILED
    assert result.failed_stage == "scenes"


def test_approval_gate_blocks_in_live_mode(tmp_path: Path) -> None:
    live = load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY": "sk-testkey-1234567890abcdef",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
        }
    )
    mock_providers = build_providers(
        load_config(
            environ={
                "APP_MODE": "mock",
                "DATA_DIR": str(tmp_path / "data2"),
                "LOGS_DIR": str(tmp_path / "logs2"),
            }
        )
    )
    from shortsgen.repository import JobRepository
    from shortsgen.pipeline.stages import build_handlers

    repo = JobRepository(tmp_path / "live.sqlite")
    try:
        handlers = build_handlers(live, mock_providers)
        handlers["upload"] = Recorder("upload")  # jangan memicu OAuth/jaringan di test
        pipeline = Pipeline(repo, live, mock_providers, handlers)
        job = repo.create_job("Topik Live", "live")

        result = pipeline.run(job.id)
        assert result.status == JobStatus.AWAITING_APPROVAL
        assert result.blocked_stage == "approval"
        assert repo.get_job(job.id).status == JobStatus.AWAITING_APPROVAL
        # stage setelah approval belum dijalankan
        assert repo.get_stage(job.id, "upload") is None

        pipeline.approve(job.id, note="uji")
        result2 = pipeline.run(job.id)
        assert result2.status == JobStatus.DONE
        # upload diimplementasikan (Fase F); schedule kini ada dan dilewati
        # bila tidak ada jadwal diminta (bukan gap).
        assert "schedule" not in result2.stages_gap
        assert repo.get_stage(job.id, "upload").status == StageStatus.DONE
        # Tanpa --publish-at, stage schedule selesai tanpa menjadwalkan apa pun.
        assert repo.get_stage(job.id, "schedule").status in (StageStatus.DONE, StageStatus.SKIPPED)
        approval = repo.get_stage(job.id, "approval")
        assert approval.artifacts.get("approved") is True
    finally:
        repo.close()


def test_missing_handler_recorded_as_gap(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = make_handlers()
    del handlers["qa"]
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik Gap", "mock")
    result = pipeline.run(job.id)
    assert result.status == JobStatus.DONE
    assert result.stages_gap == ["qa"]
    assert repo.get_stage(job.id, "qa").status == StageStatus.SKIPPED


def test_auto_handlers_record_llm_usage(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    """Pipeline merakit handler otomatis dari registry; riset+naskah memakai LLM (mock)."""
    pipeline = Pipeline(repo, mock_config, mock_bundle)
    job = repo.create_job("Topik Usage", "mock")
    result = pipeline.run(job.id)
    assert result.status == JobStatus.DONE
    usage = repo.usage_summary(job.id)
    assert "mock:llm" in usage["by_provider"]
    assert usage["by_provider"]["mock:llm"]["calls"] >= 2   # research + script


def test_disk_quota_blocks_run(tmp_path: Path, mock_bundle: ProviderBundle) -> None:
    from shortsgen.repository import JobRepository

    config = load_config(
        environ={
            "APP_MODE": "mock",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            "DISK_QUOTA_MB": "10",
        }
    )
    # buat file 11 MB agar kuota (10 MB) terlampaui
    big = config.data_dir / "besar.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"\x00" * (11 * 1024 * 1024))

    repo = JobRepository(tmp_path / "quota.sqlite")
    try:
        pipeline = Pipeline(repo, config, mock_bundle, make_handlers())
        job = repo.create_job("Topik Kuota", "mock")
        result = pipeline.run(job.id)
        assert result.status == JobStatus.FAILED
        assert "kuota" in (result.error or "").lower()
        assert repo.get_stage(job.id, "research") is None
    finally:
        repo.close()


def test_usage_recorded_per_provider_in_pipeline(repo, mock_config: Config, mock_bundle: ProviderBundle) -> None:
    handlers = {
        stage: Recorder(stage) for stage in STAGE_ORDER
    }
    pipeline = Pipeline(repo, mock_config, mock_bundle, handlers)
    job = repo.create_job("Topik Tanpa Usage", "mock")
    pipeline.run(job.id)
    # handler Recorder tidak mencatat usage -> ringkasan kosong (bukti pemisahan tanggung jawab)
    assert repo.usage_summary(job.id)["by_provider"] == {}
