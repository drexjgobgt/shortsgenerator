"""Stage UPLOAD: unggah video ke YouTube (OAuth resmi), default private.

Prasyarat:
- Stage `approval` sudah DONE (gerbang manusia).
- `client_secrets.json` tipe Desktop tersedia; token OAuth dibuat saat pertama kali.
- Video berasal dari `preview.json` yang dihasilkan stage preview.

Pengaman:
- Visibilitas non-private hanya dari approval khusus job; default global private.
- Fingerprint SHA-256 video mencegah upload file yang sama dua kali.
- `containsSyntheticMedia` diisi sesuai konfigurasi (default true).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from ...config import Config
from ...models import StageStatus, utcnow_iso
from ...providers.base import ProviderBundle
from ...youtube.auth import load_credentials
from ...youtube.uploader import build_video_body, upload_video, video_fingerprint
from ..orchestrator import StageBlocked, StageContext


class UploadStage:
    stage = "upload"

    def __init__(
        self,
        providers: ProviderBundle,
        config: Config,
        *,
        logger: Any = None,
        credentials_loader: Callable[..., Any] = load_credentials,
        uploader: Callable[..., Any] = upload_video,
    ):
        self.providers = providers
        self.config = config
        self.logger = logger
        self.credentials_loader = credentials_loader
        self.uploader = uploader

    def run(self, ctx: StageContext) -> dict[str, Any]:
        if ctx.config.is_mock:
            return {
                "skipped": True,
                "reason": "mode mock: upload YouTube tidak dilakukan",
            }

        approval = ctx.repo.get_stage(ctx.job.id, "approval")
        if approval is None or approval.status != StageStatus.DONE:
            raise StageBlocked(
                "upload",
                "Upload membutuhkan persetujuan manusia. Jalankan: "
                f"shortsgen approve --job {ctx.job.id}",
            )

        preview = _load_json(ctx, "preview.json", ["preview"])
        video = Path(str(preview.get("video") or ""))
        if not video.is_file():
            raise RuntimeError(f"video pratinjau tidak ditemukan: {video}")

        # Resume aman: kalau upload sebelumnya sudah sukses tetapi stage gagal
        # mencatat (mis. DB terkunci), jangan upload ulang video yang sama.
        prior_artifact = ctx.artifacts_dir / "upload.json"
        if prior_artifact.exists():
            try:
                prior = json.loads(prior_artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = {}
            if prior.get("video_id") and not prior.get("duplicate"):
                if self.logger is not None:
                    self.logger.info("Upload sebelumnya ditemukan; memakai hasil yang ada")
                return {
                    "upload": str(prior_artifact),
                    "video_id": prior.get("video_id"),
                    "url": prior.get("url"),
                    "privacy_status": prior.get("privacy_status"),
                    "processing_status": prior.get("processing_status"),
                    "fingerprint": prior.get("fingerprint"),
                    "resumed": True,
                }

        fingerprint = video_fingerprint(video)
        existing = ctx.repo.find_upload(fingerprint)
        if existing is not None:
            payload = {
                "job_id": ctx.job.id,
                "skipped": True,
                "duplicate": True,
                "reason": "video identik sudah pernah diupload; upload kedua dicegah",
                "video": str(video),
                "fingerprint": fingerprint,
                "duplicate_of": existing,
                "video_id": existing.get("video_id"),
                "url": f"https://youtu.be/{existing.get('video_id')}",
                "privacy_status": existing.get("privacy_status"),
                "uploaded_at": existing.get("uploaded_at"),
            }
            self._write_artifacts(ctx, payload)
            ctx.repo.add_event(
                ctx.job.id,
                "warning",
                "Upload dilewati: video identik sudah ada di YouTube",
                {"video_id": existing.get("video_id"), "fingerprint": fingerprint[:16]},
            )
            return payload

        metadata = preview.get("metadata") or {}
        visibility = (approval.artifacts or {}).get("upload_visibility") or self.config.upload_visibility
        if visibility not in ("private", "unlisted", "public"):
            raise RuntimeError("Visibilitas approval tidak valid")
        if visibility != "private" and (approval.artifacts or {}).get("approved") is not True:
            raise StageBlocked("upload", "Upload non-private memerlukan approval eksplisit khusus job")
        body = build_video_body(
            title=str(metadata.get("title") or ctx.job.topic),
            description=str(metadata.get("description") or ""),
            tags=metadata.get("tags") or [],
            category_id=str(metadata.get("category_id") or "27"),
            default_language=str(metadata.get("default_language") or "id"),
            privacy_status=visibility,
            made_for_kids=self.config.youtube_made_for_kids,
            synthetic_disclosure=self.config.youtube_synthetic_disclosure,
        )

        if self.logger is not None:
            self.logger.info("Memeriksa OAuth YouTube (token: %s)", self.config.youtube_token_file)
        credentials = self.credentials_loader(
            self.config.youtube_client_secrets_file,
            self.config.youtube_token_file,
            interactive=True,
            logger=self.logger,
        )

        if self.logger is not None:
            self.logger.info(
                "Mengunggah %s ke YouTube sebagai %s...",
                video.name,
                visibility,
            )
        result = self.uploader(
            video_path=video,
            body=body,
            credentials=credentials,
            chunk_size_bytes=max(1, self.config.youtube_upload_chunk_mb) * 1024 * 1024,
            processing_timeout_s=self.config.youtube_processing_timeout_s,
            logger=self.logger,
        )

        payload = {
            "job_id": ctx.job.id,
            "skipped": False,
            "duplicate": False,
            "video": str(video),
            "fingerprint": fingerprint,
            "video_id": result.video_id,
            "url": result.url,
            "privacy_status": result.privacy_status,
            "upload_status": result.upload_status,
            "processing_status": result.processing_status,
            "channel_id": result.channel_id,
            "channel_title": result.channel_title,
            "title": body["snippet"]["title"],
            "made_for_kids": self.config.youtube_made_for_kids,
            "synthetic_disclosure": self.config.youtube_synthetic_disclosure,
            "quota_units_estimate": 1600,
            "uploaded_at": utcnow_iso(),
            "processing": result.processing,
        }
        try:
            ctx.repo.record_upload(
                ctx.job.id,
                fingerprint,
                result.video_id,
                result.privacy_status,
                meta={"url": result.url, "title": body["snippet"]["title"]},
            )
        except sqlite3.IntegrityError:
            # Race: proses lain menyelesaikan upload yang sama lebih dulu.
            existing = ctx.repo.find_upload(fingerprint)
            payload["duplicate"] = True
            payload["duplicate_of"] = existing
            payload["reason"] = "video identik sudah tercatat oleh proses lain"
        self._write_artifacts(ctx, payload)
        ctx.repo.add_event(
            ctx.job.id,
            "info",
            f"Video diunggah ke YouTube ({payload['privacy_status']}): {payload['url']}",
            {
                "video_id": result.video_id,
                "privacy_status": payload["privacy_status"],
                "processing_status": payload["processing_status"],
            },
        )
        return {
            "upload": str(ctx.artifacts_dir / "upload.json"),
            "video_id": payload["video_id"],
            "url": payload["url"],
            "privacy_status": payload["privacy_status"],
            "processing_status": payload["processing_status"],
            "fingerprint": fingerprint,
        }

    def _write_artifacts(self, ctx: StageContext, payload: dict[str, Any]) -> None:
        path = ctx.artifact_path("upload.json")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"# Upload YouTube: {payload.get('title') or ctx.job.topic}",
            "",
            f"- Status: {'dilewati (duplikat)' if payload.get('duplicate') else 'diunggah'}",
            f"- URL: {payload.get('url') or '-'}",
            f"- Video ID: {payload.get('video_id') or '-'}",
            f"- Visibilitas: {payload.get('privacy_status') or '-'}",
            f"- Status upload: {payload.get('upload_status') or '-'}",
            f"- Status pemrosesan: {payload.get('processing_status') or '-'}",
            f"- Fingerprint: `{str(payload.get('fingerprint') or '')[:16]}...`",
            f"- Made for kids: {payload.get('made_for_kids')}",
            f"- Disclosure media sintetis: {payload.get('synthetic_disclosure')}",
        ]
        if payload.get("duplicate_of"):
            lines += ["", f"- Duplikat dari: `{payload['duplicate_of']}`"]
        (ctx.artifact_path("upload.md")).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_json(ctx: StageContext, filename: str, stages: list[str]) -> dict[str, Any]:
    for stage in stages:
        record = ctx.repo.get_stage(ctx.job.id, stage)
        if not record:
            continue
        for value in (record.artifacts or {}).values():
            path = Path(str(value))
            if path.name == filename and path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    fallback = ctx.artifacts_dir / filename
    if fallback.exists():
        return json.loads(fallback.read_text(encoding="utf-8"))
    raise RuntimeError(f"artefak '{filename}' tidak ditemukan")
