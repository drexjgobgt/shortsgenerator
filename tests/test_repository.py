"""Test SQLite job repository: state machine, resume, dedupe, usage."""
from __future__ import annotations

import pytest

from shortsgen.errors import DedupeConflict, NotFoundError
from shortsgen.models import JobStatus, StageStatus
from shortsgen.repository import JobRepository, compute_dedupe_key, normalize_topic


def test_create_and_get_job(repo: JobRepository) -> None:
    job = repo.create_job("Topik Uji", "mock", {"mode": "mock"})
    assert job.id.startswith("job_")
    assert job.status == JobStatus.CREATED
    assert job.mode == "mock"
    fetched = repo.get_job(job.id)
    assert fetched.topic == "Topik Uji"
    assert fetched.config_snapshot == {"mode": "mock"}


def test_delete_job_cascades_children(repo: JobRepository) -> None:
    job = repo.create_job("Topik Hapus", "mock")
    repo.start_stage(job.id, "research")
    repo.finish_stage(job.id, "research", StageStatus.DONE)
    repo.record_usage(job.id, "mock", "model", "llm", units={"tokens": 1})
    repo.record_upload(job.id, "fp-1", "vid-1", "private")

    assert repo.delete_job(job.id) is True
    with pytest.raises(NotFoundError):
        repo.get_job(job.id)
    assert repo.get_stage(job.id, "research") is None
    assert repo.get_usage(job.id) == []
    assert repo.find_upload("fp-1") is None


def test_delete_job_missing_raises(repo: JobRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.delete_job("job_tidak_ada")


def test_dedupe_blocks_same_topic(repo: JobRepository) -> None:
    first = repo.create_job("Langit Mars", "mock")
    with pytest.raises(DedupeConflict) as exc_info:
        repo.create_job("  langit   MARS ", "mock")  # normalisasi sama
    assert exc_info.value.existing_job_id == first.id


def test_dedupe_force_and_different_mode(repo: JobRepository) -> None:
    repo.create_job("Topik Duplikat", "mock")
    other_mode = repo.create_job("Topik Duplikat", "live")  # mode berbeda -> boleh
    forced = repo.create_job("Topik Duplikat", "mock", force=True)
    assert other_mode.id != forced.id
    assert repo.job_count() == 3


def test_dedupe_allows_new_after_failure(repo: JobRepository) -> None:
    first = repo.create_job("Topik Gagal", "mock")
    repo.set_job_status(first.id, JobStatus.FAILED, error="boom")
    second = repo.create_job("Topik Gagal", "mock")
    assert second.id != first.id


def test_normalize_and_key() -> None:
    assert normalize_topic("  A   B ") == "a b"
    assert compute_dedupe_key("A B", "mock") == compute_dedupe_key("a  b", "mock")
    assert compute_dedupe_key("A B", "mock") != compute_dedupe_key("A B", "live")


def test_stage_lifecycle_and_resume(repo: JobRepository) -> None:
    job = repo.create_job("Topik Stage", "mock")
    assert repo.next_pending_stage(job.id) == "research"

    repo.start_stage(job.id, "research")
    record = repo.get_stage(job.id, "research")
    assert record is not None and record.status == StageStatus.RUNNING and record.attempt == 1

    repo.finish_stage(job.id, "research", StageStatus.DONE, artifacts={"artifact": "a.json"})
    assert repo.next_pending_stage(job.id) == "script"

    repo.start_stage(job.id, "script")
    repo.finish_stage(job.id, "script", StageStatus.FAILED, error="provider down")
    # stage gagal -> tetap menjadi titik resume
    assert repo.next_pending_stage(job.id) == "script"

    repo.start_stage(job.id, "script")  # retry menaikkan attempt
    assert repo.get_stage(job.id, "script").attempt == 2
    repo.finish_stage(job.id, "script", StageStatus.DONE)
    assert repo.next_pending_stage(job.id) == "review"

    stages = repo.get_stages(job.id)
    assert stages["research"].artifacts == {"artifact": "a.json"}


def test_unknown_job_raises(repo: JobRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.get_job("job_tidak_ada")


def test_events_and_usage(repo: JobRepository) -> None:
    job = repo.create_job("Topik Audit", "mock")
    repo.add_event(job.id, "info", "halo", {"x": 1})
    events = repo.get_events(job.id)
    assert any(e.message == "halo" for e in events)

    repo.record_usage(job.id, "mock", "mock-llm", "llm", units={"input_tokens": 10, "output_tokens": 5})
    repo.record_usage(job.id, "9router", "edge-tts/id-ID-ArdiNeural", "tts", units={"characters": 120})
    summary = repo.usage_summary(job.id)
    assert summary["by_provider"]["mock:llm"]["calls"] == 1
    assert summary["by_provider"]["mock:llm"]["units"]["input_tokens"] == 10
    assert summary["by_provider"]["9router:tts"]["units"]["characters"] == 120
    assert summary["total_cost_estimate"] == 0.0


def test_reset_stages_from_point(repo: JobRepository) -> None:
    job = repo.create_job("Topik Reset", "mock")
    for stage in ("research", "script", "scenes", "assets"):
        repo.start_stage(job.id, stage)
        repo.finish_stage(job.id, stage, StageStatus.DONE, artifacts={})
    assert repo.get_stage(job.id, "assets").status == StageStatus.DONE
    reset = repo.reset_stages(job.id, "scenes")
    assert reset[:3] == ["scenes", "assets", "tts"]
    assert repo.get_stage(job.id, "scenes") is None
    assert repo.get_stage(job.id, "assets") is None
    assert repo.get_stage(job.id, "script").status == StageStatus.DONE
    assert repo.get_job(job.id).status == JobStatus.RUNNING


def test_jobs_by_status_and_list(repo: JobRepository) -> None:
    a = repo.create_job("Topik A", "mock")
    repo.create_job("Topik B", "mock")
    repo.set_job_status(a.id, JobStatus.RUNNING)
    running = repo.jobs_by_status([JobStatus.RUNNING])
    assert [j.id for j in running] == [a.id]
    assert len(repo.list_jobs()) == 2
