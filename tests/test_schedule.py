"""Test stage schedule: parsing waktu, validasi, dan kontrak API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shortsgen.pipeline.stages.schedule import parse_publish_at, validate_publish_at


def test_parse_publish_at_accepts_offset_and_zulu() -> None:
    assert parse_publish_at("2026-10-27T20:30:00+07:00") == datetime(
        2026, 10, 27, 13, 30, tzinfo=timezone.utc
    )
    assert parse_publish_at("2026-10-27T13:30:00Z") == datetime(
        2026, 10, 27, 13, 30, tzinfo=timezone.utc
    )


def test_parse_publish_at_treats_naive_as_local_time() -> None:
    parsed = parse_publish_at("2026-10-27 20:30")
    assert parsed.tzinfo is timezone.utc


def test_parse_publish_at_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_publish_at("besok sore")


def test_validate_rejects_past_and_too_soon() -> None:
    now = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        validate_publish_at(now - timedelta(minutes=1), now=now)
    with pytest.raises(ValueError):
        validate_publish_at(now + timedelta(minutes=5), now=now)


def test_validate_accepts_fifteen_minutes_ahead() -> None:
    now = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    validate_publish_at(now + timedelta(minutes=15), now=now)
    validate_publish_at(now + timedelta(hours=3), now=now)


def test_schedule_stage_requires_approval_and_upload(tmp_path) -> None:
    from shortsgen.pipeline.orchestrator import StageBlocked
    from shortsgen.pipeline.stages.schedule import ScheduleStage

    class _Ctx:
        def __init__(self) -> None:
            self.config = type("C", (), {"is_mock": False})()
            self.artifacts_dir = tmp_path

            class _Repo:
                def get_stage(self, job_id, stage):
                    return None

            self.repo = _Repo()
            self.job = type("J", (), {"id": "job_test", "topic": "x"})()

    stage = ScheduleStage(providers=None, config=None)
    with pytest.raises(StageBlocked):
        stage.run(_Ctx())


def test_schedule_skipped_when_not_requested() -> None:
    """Tanpa --publish-at, stage schedule harus dilewati, bukan memblokir job."""
    from shortsgen.models import StageStatus
    from shortsgen.pipeline.stages.schedule import ScheduleStage

    approval = type("A", (), {"status": StageStatus.DONE, "artifacts": {"approved": True}})()
    upload = type("U", (), {"status": StageStatus.DONE, "artifacts": {}})()

    class _Repo:
        def get_stage(self, job_id, stage):
            return approval if stage == "approval" else upload

    ctx = type("Ctx", (), {})()
    ctx.config = type("C", (), {"is_mock": False})()
    ctx.repo = _Repo()
    ctx.job = type("J", (), {"id": "job_test", "topic": "x"})()
    result = ScheduleStage(providers=None, config=None).run(ctx)
    assert result["skipped"] is True
    assert "publish-at" in result["reason"]


def test_schedule_skipped_in_mock(tmp_path) -> None:
    from shortsgen.pipeline.stages.schedule import ScheduleStage

    ctx = type("Ctx", (), {})()
    ctx.config = type("C", (), {"is_mock": True})()
    ctx.artifacts_dir = tmp_path
    result = ScheduleStage(providers=None, config=None).run(ctx)
    assert result["skipped"] is True
