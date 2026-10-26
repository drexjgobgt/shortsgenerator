"""Upload video ke YouTube Data API v3 (resumable, default private).

Kontrak:
- `privacyStatus` eksplisit; default private, override publik berasal dari
  approval khusus job di stage upload, bukan perubahan default global.
- `containsSyntheticMedia` diisi `true` bila video memakai aset sintetis/AI.
- Upload bersifat resumable dan retry terbatas untuk error 5xx.
- Fingerprint SHA-256 file video dipakai untuk mencegah upload duplikat.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..errors import ShortsGenError


class YouTubeUploadError(ShortsGenError):
    """Upload YouTube gagal (kuota, otorisasi, jaringan, atau respons invalid)."""


@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy_status: str
    upload_status: str
    processing_status: str
    channel_id: str = ""
    channel_title: str = ""
    processing: dict[str, Any] = field(default_factory=dict)


def video_fingerprint(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 isi file video (streaming; tidak memuat seluruh file ke memori)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trim_tags(tags: Iterable[str], *, max_total: int = 500) -> list[str]:
    result: list[str] = []
    total = 0
    for tag in tags or []:
        text = str(tag).strip()[:100]
        if not text:
            continue
        if total + len(text) + 1 > max_total:
            break
        result.append(text)
        total += len(text) + 1
    return result


def build_video_body(
    *,
    title: str,
    description: str,
    tags: Iterable[str] = (),
    category_id: str = "27",
    default_language: str = "id",
    privacy_status: str = "private",
    made_for_kids: bool = False,
    synthetic_disclosure: bool = True,
) -> dict[str, Any]:
    """Susun body `videos.insert` (snippet + status) yang eksplisit dan aman."""
    if privacy_status not in ("private", "unlisted", "public"):
        raise ValueError("privacyStatus YouTube tidak valid")
    snippet: dict[str, Any] = {
        "title": (title or "Tanpa judul")[:100],
        "description": (description or "")[:5000],
        "tags": _trim_tags(tags),
        "categoryId": str(category_id or "27"),
    }
    if default_language:
        snippet["defaultLanguage"] = default_language
    status: dict[str, Any] = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": bool(made_for_kids),
        "license": "youtube",
        "embeddable": True,
    }
    if synthetic_disclosure:
        status["containsSyntheticMedia"] = True
    return {"snippet": snippet, "status": status}


def _http_error_message(exc: Any) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = str(exc).strip().replace("\n", " ")
    if len(detail) > 500:
        detail = detail[:500] + "..."
    if "youtubeSignupRequired" in detail:
        return (
            "Akun Google yang dipakai belum memiliki channel YouTube. "
            "Login dengan akun pemilik channel (atau buat channel di youtube.com), "
            "lalu jalankan: python -m shortsgen auth --force"
        )
    if status == 403 and "quota" in detail.lower():
        return f"kuota YouTube API habis (HTTP 403): {detail}"
    if status in (401, 403):
        return f"otorisasi YouTube ditolak (HTTP {status}): {detail}"
    return f"upload YouTube gagal (HTTP {status}): {detail}"


def _channel_info(youtube: Any) -> dict[str, str]:
    """Pastikan token milik akun yang punya channel YouTube (cegah youtubeSignupRequired)."""
    try:
        response = youtube.channels().list(part="id,snippet", mine=True).execute()
    except Exception as exc:  # noqa: BLE001
        raise YouTubeUploadError(_http_error_message(exc)) from exc
    items = response.get("items") or []
    if not items:
        raise YouTubeUploadError(
            "Akun Google ini belum memiliki channel YouTube. "
            "Login dengan akun pemilik channel (atau buat channel di youtube.com), "
            "lalu jalankan: python -m shortsgen auth --force"
        )
    item = items[0]
    return {
        "id": str(item.get("id") or ""),
        "title": str((item.get("snippet") or {}).get("title") or ""),
    }


def _check_processing(
    youtube: Any,
    video_id: str,
    *,
    timeout_s: float,
    sleep: Any = time.sleep,
    logger: Any = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    last: dict[str, Any] = {
        "processing_status": "unknown",
        "upload_status": "unknown",
        "privacy_status": "unknown",
        "raw": {},
    }
    while True:
        try:
            response = youtube.videos().list(
                part="status,processingDetails", id=video_id
            ).execute()
            item = (response.get("items") or [{}])[0]
            status = item.get("status") or {}
            details = item.get("processingDetails") or {}
            last = {
                "processing_status": str(
                    details.get("processingStatus")
                    or status.get("uploadStatus")
                    or "unknown"
                ),
                "upload_status": str(status.get("uploadStatus") or "unknown"),
                "privacy_status": str(status.get("privacyStatus") or "unknown"),
                "raw": item,
            }
        except Exception as exc:  # noqa: BLE001 - status bukan alasan menggagalkan upload
            last["error"] = str(exc)[:300]
            if logger is not None:
                logger.warning("Gagal memeriksa status pemrosesan YouTube: %s", exc)
        if last["processing_status"] in ("succeeded", "failed", "terminated"):
            return last
        if time.monotonic() >= deadline:
            return last
        sleep(5)


def upload_video(
    *,
    video_path: Path,
    body: dict[str, Any],
    credentials: Any,
    chunk_size_bytes: int = 8 * 1024 * 1024,
    processing_timeout_s: float = 60.0,
    logger: Any = None,
    sleep: Any = time.sleep,
) -> UploadResult:
    """Upload resumable ke YouTube dan kembalikan status awal.

    Raises:
        YouTubeUploadError: bila upload gagal atau respons tidak memuat video id.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover
        raise YouTubeUploadError(
            "Dependensi Google belum terpasang. Jalankan: "
            "python -m pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    video_path = Path(video_path)
    if not video_path.exists():
        raise YouTubeUploadError(f"file video tidak ditemukan: {video_path}")

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    channel = _channel_info(youtube)
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=max(1, int(chunk_size_bytes)),
        resumable=True,
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response: Optional[dict[str, Any]] = None
    retries = 0
    while response is None:
        try:
            progress, response = request.next_chunk()
        except HttpError as exc:
            status_code = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
            if status_code >= 500 and retries < 5:
                retries += 1
                delay = min(2 ** retries, 30)
                if logger is not None:
                    logger.warning(
                        "Upload YouTube error %s; retry %s/5 dalam %ss",
                        status_code,
                        retries,
                        delay,
                    )
                sleep(delay)
                continue
            raise YouTubeUploadError(_http_error_message(exc)) from exc
        if progress is not None and logger is not None:
            logger.info("Upload YouTube: %d%%", int(progress.progress() * 100))

    video_id = str(response.get("id") or "")
    if not video_id:
        raise YouTubeUploadError("respons YouTube tidak memuat video id")
    status = response.get("status") or {}
    snippet = response.get("snippet") or {}
    processing = _check_processing(
        youtube,
        video_id,
        timeout_s=processing_timeout_s,
        sleep=sleep,
        logger=logger,
    )
    return UploadResult(
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        privacy_status=str(status.get("privacyStatus") or body.get("status", {}).get("privacyStatus") or "private"),
        upload_status=str(status.get("uploadStatus") or "unknown"),
        processing_status=str(processing.get("processing_status") or "unknown"),
        channel_id=str(snippet.get("channelId") or channel.get("id") or ""),
        channel_title=channel.get("title", ""),
        processing=processing,
    )
