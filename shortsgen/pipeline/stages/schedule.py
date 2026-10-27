"""Stage SCHEDULE: jadwalkan penayangan video YouTube (`publishAt`).

Kontrak & pengaman:
- Hanya berjalan di mode live dan setelah stage `upload` DONE.
- Video harus masih `private`/`unlisted`; YouTube menolak `publishAt` untuk video
  yang sudah `public`, jadi visibilitas diubah ke `private` lebih dulu bila perlu.
- `publishAt` wajib minimal ~15 menit ke depan (batas YouTube).
- Waktu diambil dari approval khusus job (`--publish-at`); tanpa itu stage gagal
  dengan pesan jelas, bukan menjadwalkan diam-diam.
- Jadwal bersifat idempoten: menjalankan ulang dengan waktu sama tidak mengubah apa pun.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ...config import Config
from ...models import StageStatus, utcnow_iso
from ...providers.base import ProviderBundle
from ...youtube.auth import load_credentials
from ...youtube.uploader import YouTubeUploadError, _http_error_message
from ..orchestrator import StageBlocked, StageContext

MIN_LEAD = timedelta(minutes=15)


def parse_publish_at(value: str) -> datetime:
    """Terima ISO-8601 (dengan/atau tanpa zona) atau 'YYYY-MM-DD HH:MM' waktu lokal."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("publish_at kosong")
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"format publish_at tidak dikenali: {value!r} (pakai ISO-8601, mis. 2026-10-27T20:30:00+07:00)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def validate_publish_at(publish_at: datetime, *, now: Optional[datetime] = None) -> None:
    """Tolak jadwal yang terlalu dekat atau sudah lewat."""
    current = now or datetime.now(timezone.utc)
    if publish_at <= current:
        raise ValueError(f"publish_at sudah lewat: {publish_at.isoformat()}")
    if publish_at - current < MIN_LEAD:
        raise ValueError(
            "publish_at minimal 15 menit ke depan (batas YouTube); "
            f"diberikan {publish_at.isoformat()}"
        )


class ScheduleStage:
    stage = "schedule"

    def __init__(
        self,
        providers: ProviderBundle,
        config: Config,
        *,
        logger: Any = None,
        credentials_loader: Callable[..., Any] = load_credentials,
    ):
        self.providers = providers
        self.config = config
        self.logger = logger
        self.credentials_loader = credentials_loader

    def run(self, ctx: StageContext) -> dict[str, Any]:
        if ctx.config.is_mock:
            return {"skipped": True, "reason": "mode mock: penjadwalan YouTube tidak dilakukan"}

        approval = ctx.repo.get_stage(ctx.job.id, "approval")
        if approval is None or approval.status != StageStatus.DONE:
            raise StageBlocked(
                "schedule",
                "Penjadwalan membutuhkan persetujuan manusia. Jalankan: "
                f"shortsgen approve --job {ctx.job.id} --publish-at <ISO-8601>",
            )

        upload_record = ctx.repo.get_stage(ctx.job.id, "upload")
        if upload_record is None or upload_record.status != StageStatus.DONE:
            raise StageBlocked("schedule", "Penjadwalan membutuhkan stage upload yang selesai")

        artifacts = approval.artifacts or {}
        raw_publish_at = artifacts.get("publish_at")
        if not raw_publish_at:
            # Penjadwalan bersifat opsional: tanpa --publish-at, stage ini
            # memang tidak diminta, jadi dilewati (bukan error/blocked).
            return {
                "skipped": True,
                "reason": "tidak ada jadwal diminta; gunakan approve --publish-at <ISO-8601> untuk menjadwalkan",
            }

        upload = _load_upload(ctx)
        video_id = str(upload.get("video_id") or "")
        if not video_id:
            raise RuntimeError("upload.json tidak memuat video_id")

        publish_at = parse_publish_at(str(raw_publish_at))
        validate_publish_at(publish_at)
        iso_publish_at = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        existing = ctx.artifacts_dir / "schedule.json"
        if existing.exists():
            try:
                prior = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = {}
            if prior.get("publish_at") == iso_publish_at and prior.get("video_id") == video_id:
                if self.logger is not None:
                    self.logger.info("Jadwal yang sama sudah tercatat; tidak mengubah apa pun")
                return {
                    "schedule": str(existing),
                    "video_id": video_id,
                    "publish_at": iso_publish_at,
                    "privacy_status": prior.get("privacy_status"),
                    "resumed": True,
                }

        credentials = self.credentials_loader(
            self.config.youtube_client_secrets_file,
            self.config.youtube_token_file,
            interactive=True,
            logger=self.logger,
        )
        result = schedule_video(
            video_id=video_id,
            publish_at=iso_publish_at,
            credentials=credentials,
            logger=self.logger,
        )

        payload = {
            "job_id": ctx.job.id,
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "publish_at": iso_publish_at,
            "privacy_status": result.get("privacy_status"),
            "scheduled_at": utcnow_iso(),
            "previous_privacy_status": upload.get("privacy_status"),
        }
        path = ctx.artifact_path("schedule.json")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"# Jadwal tayang: {upload.get('title') or ctx.job.topic}",
            "",
            f"- URL: {payload['url']}",
            f"- Video ID: {video_id}",
            f"- publishAt (UTC): {iso_publish_at}",
            f"- Visibilitas: {payload['privacy_status']}",
            f"- Dicatat: {payload['scheduled_at']}",
        ]
        (ctx.artifact_path("schedule.md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
        ctx.repo.add_event(
            ctx.job.id,
            "info",
            f"Penayangan dijadwalkan {iso_publish_at}: {payload['url']}",
            {"video_id": video_id, "publish_at": iso_publish_at},
        )
        return {
            "schedule": str(path),
            "video_id": video_id,
            "publish_at": iso_publish_at,
            "privacy_status": payload["privacy_status"],
        }


def schedule_video(
    *,
    video_id: str,
    publish_at: str,
    credentials: Any,
    logger: Any = None,
) -> dict[str, Any]:
    """Set `status.publishAt` + `privacyStatus=private` lewat videos.update."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise YouTubeUploadError(
            "Dependensi Google belum terpasang. Jalankan: "
            "python -m pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    try:
        current = youtube.videos().list(part="status", id=video_id).execute()
        items = current.get("items") or []
        if not items:
            raise YouTubeUploadError(f"video {video_id} tidak ditemukan di channel ini")
        status = items[0].get("status") or {}
        if str(status.get("privacyStatus")) == "public":
            raise YouTubeUploadError(
                "video sudah public; YouTube tidak mengizinkan publishAt. "
                "Jadwalkan sebelum tayang (upload private) atau buat video baru."
            )
        body = {
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at,
                "selfDeclaredMadeForKids": bool(status.get("selfDeclaredMadeForKids", False)),
            },
        }
        if "containsSyntheticMedia" in status:
            body["status"]["containsSyntheticMedia"] = bool(status["containsSyntheticMedia"])
        response = youtube.videos().update(part="status", body=body).execute()
    except YouTubeUploadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise YouTubeUploadError(_http_error_message(exc)) from exc

    new_status = response.get("status") or {}
    if logger is not None:
        logger.info("Jadwal tayang diset: %s (%s)", publish_at, new_status.get("privacyStatus"))
    return {
        "video_id": video_id,
        "publish_at": str(new_status.get("publishAt") or publish_at),
        "privacy_status": str(new_status.get("privacyStatus") or "private"),
    }


def _load_upload(ctx: StageContext) -> dict[str, Any]:
    path = Path(ctx.artifacts_dir / "upload.json")
    if not path.exists():
        raise RuntimeError("upload.json tidak ditemukan; jalankan stage upload lebih dulu")
    return json.loads(path.read_text(encoding="utf-8"))
