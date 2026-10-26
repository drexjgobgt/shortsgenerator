"""Test integrasi YouTube: body upload aman, fingerprint, dan stage upload tanpa jaringan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortsgen.config import load_config
from shortsgen.models import StageStatus
from shortsgen.pipeline.orchestrator import StageBlocked, StageContext
from shortsgen.pipeline.stages.upload import UploadStage
from shortsgen.providers.base import ProviderBundle
from shortsgen.youtube.auth import YouTubeAuthError, validate_client_secrets
from shortsgen.youtube.uploader import (
    UploadResult,
    build_video_body,
    video_fingerprint,
)


def _live_config(tmp_path: Path):
    return load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY": "sk-testkey-1234567890abcdef",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            "YOUTUBE_CLIENT_SECRETS": str(tmp_path / "client_secrets.json"),
        }
    )


def _ctx(repo, config, job) -> StageContext:
    artifacts = config.jobs_dir / job.id / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return StageContext(
        job=job,
        config=config,
        providers=ProviderBundle(mode=config.mode),
        repo=repo,
        logger=None,
        artifacts_dir=artifacts,
    )


def _write_preview(ctx: StageContext, video: Path) -> None:
    payload = {
        "video": str(video),
        "metadata": {
            "title": "Judul Uji",
            "description": "Deskripsi uji",
            "tags": ["gurita", "laut"],
            "category_id": "27",
            "default_language": "id",
        },
    }
    (ctx.artifacts_dir / "preview.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_build_video_body_private_and_synthetic_disclosure() -> None:
    body = build_video_body(
        title="Judul",
        description="Deskripsi",
        tags=["a", "b"],
        privacy_status="private",
        made_for_kids=False,
        synthetic_disclosure=True,
    )
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["defaultLanguage"] == "id"
    assert body["snippet"]["tags"] == ["a", "b"]


def test_build_video_body_omits_synthetic_flag_when_disabled() -> None:
    body = build_video_body(
        title="Judul",
        description="",
        synthetic_disclosure=False,
    )
    assert "containsSyntheticMedia" not in body["status"]


def test_video_fingerprint_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"video-a")
    second.write_bytes(b"video-b")
    assert video_fingerprint(first) == video_fingerprint(first)
    assert video_fingerprint(first) != video_fingerprint(second)


def test_validate_client_secrets_rejects_web_client(tmp_path: Path) -> None:
    path = tmp_path / "client_secrets.json"
    path.write_text(
        json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8"
    )
    with pytest.raises(YouTubeAuthError):
        validate_client_secrets(path)


def test_upload_stage_mock_skips(repo, mock_config, tmp_path: Path) -> None:
    job = repo.create_job("Topik Mock Upload", "mock")
    ctx = _ctx(repo, mock_config, job)
    result = UploadStage(ProviderBundle(mode="mock"), mock_config).run(ctx)
    assert result["skipped"] is True


def test_upload_stage_requires_approval(repo, tmp_path: Path) -> None:
    config = _live_config(tmp_path)
    job = repo.create_job("Topik Tanpa Approval", "live")
    ctx = _ctx(repo, config, job)
    _write_preview(ctx, tmp_path / "video.mp4")
    with pytest.raises(StageBlocked):
        UploadStage(ProviderBundle(mode="live"), config).run(ctx)


def test_upload_stage_uploads_private_and_records_fingerprint(repo, tmp_path: Path) -> None:
    config = _live_config(tmp_path)
    job = repo.create_job("Topik Upload", "live")
    ctx = _ctx(repo, config, job)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video-bytes")
    _write_preview(ctx, video)
    repo.start_stage(job.id, "approval")
    repo.finish_stage(job.id, "approval", StageStatus.DONE, artifacts={"approved": True})

    calls: list[dict] = []

    def fake_credentials(*args, **kwargs):
        return object()

    def fake_uploader(**kwargs):
        calls.append(kwargs)
        return UploadResult(
            video_id="vid123",
            url="https://youtu.be/vid123",
            privacy_status="private",
            upload_status="uploaded",
            processing_status="succeeded",
            channel_id="chan",
        )

    stage = UploadStage(
        ProviderBundle(mode="live"),
        config,
        credentials_loader=fake_credentials,
        uploader=fake_uploader,
    )
    result = stage.run(ctx)

    assert result["video_id"] == "vid123"
    assert result["privacy_status"] == "private"
    assert calls[0]["body"]["status"]["privacyStatus"] == "private"
    assert calls[0]["body"]["status"]["containsSyntheticMedia"] is True
    assert repo.find_upload(result["fingerprint"])["video_id"] == "vid123"
    artifact = json.loads((ctx.artifacts_dir / "upload.json").read_text(encoding="utf-8"))
    assert artifact["duplicate"] is False
    assert artifact["quota_units_estimate"] == 1600


def test_upload_stage_prevents_duplicate_video(repo, tmp_path: Path) -> None:
    config = _live_config(tmp_path)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"same-video")
    fingerprint = video_fingerprint(video)

    first_job = repo.create_job("Topik Upload Pertama", "live")
    first_ctx = _ctx(repo, config, first_job)
    _write_preview(first_ctx, video)
    repo.start_stage(first_job.id, "approval")
    repo.finish_stage(first_job.id, "approval", StageStatus.DONE)
    repo.record_upload(first_job.id, fingerprint, "vid-existing", "private")

    second_job = repo.create_job("Topik Upload Kedua", "live")
    second_ctx = _ctx(repo, config, second_job)
    _write_preview(second_ctx, video)
    repo.start_stage(second_job.id, "approval")
    repo.finish_stage(second_job.id, "approval", StageStatus.DONE)

    def should_not_upload(**kwargs):  # pragma: no cover - dipanggil hanya bila bug
        raise AssertionError("uploader tidak boleh dipanggil untuk video duplikat")

    stage = UploadStage(
        ProviderBundle(mode="live"),
        config,
        credentials_loader=lambda *a, **k: object(),
        uploader=should_not_upload,
    )
    result = stage.run(second_ctx)

    assert result["duplicate"] is True
    assert result["video_id"] == "vid-existing"
    artifact = json.loads((second_ctx.artifacts_dir / "upload.json").read_text(encoding="utf-8"))
    assert artifact["skipped"] is True


def test_public_visibility_is_per_job_approval_only(repo, tmp_path):
    config = _live_config(tmp_path)
    job = repo.create_job("Public upload approved", "live")
    ctx = _ctx(repo, config, job)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"approved-public-video")
    _write_preview(ctx, video)
    repo.start_stage(job.id, "approval")
    repo.finish_stage(job.id, "approval", StageStatus.DONE,
                      artifacts={"approved": True, "upload_visibility": "public"})
    calls = []

    def upload(**kwargs):
        calls.append(kwargs)
        return UploadResult("public-id", "https://youtu.be/public-id", "public", "uploaded", "succeeded")

    result = UploadStage(ProviderBundle(mode="live"), config,
                         credentials_loader=lambda *a, **k: object(), uploader=upload).run(ctx)
    assert config.upload_visibility == "private"
    assert calls[0]["body"]["status"]["privacyStatus"] == "public"
    assert result["privacy_status"] == "public"


def test_invalid_visibility_rejected():
    with pytest.raises(ValueError):
        build_video_body(title="x", description="", privacy_status="everyone")


def test_cli_public_approval_is_explicit_and_defaults_private():
    from shortsgen.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["approve", "--job", "job_test"]).visibility == "private"
    assert parser.parse_args(["approve", "--job", "job_test", "--visibility", "public"]).visibility == "public"


def test_pipeline_approval_records_public_intent(repo, mock_config):
    from shortsgen.pipeline.orchestrator import Pipeline

    job = repo.create_job("Public intent", "live")
    repo.start_stage(job.id, "approval")
    repo.finish_stage(job.id, "approval", StageStatus.BLOCKED)
    Pipeline(repo, mock_config, ProviderBundle(mode="mock"), handlers={}).approve(
        job.id, note="User explicitly requested public", visibility="public"
    )
    approval = repo.get_stage(job.id, "approval")
    assert approval.artifacts["approved"] is True
    assert approval.artifacts["upload_visibility"] == "public"


def test_public_upload_requires_approved_flag(repo, tmp_path):
    config = _live_config(tmp_path)
    job = repo.create_job("No explicit public approval", "live")
    ctx = _ctx(repo, config, job)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not-approved")
    _write_preview(ctx, video)
    repo.start_stage(job.id, "approval")
    repo.finish_stage(job.id, "approval", StageStatus.DONE,
                      artifacts={"upload_visibility": "public"})
    stage = UploadStage(ProviderBundle(mode="live"), config,
                         credentials_loader=lambda *a, **k: pytest.fail("must not authenticate"))
    with pytest.raises(StageBlocked):
        stage.run(ctx)
