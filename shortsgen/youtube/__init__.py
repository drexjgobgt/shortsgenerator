"""Integrasi YouTube resmi (OAuth Desktop + upload private).

Modul ini tidak pernah mencetak atau menyimpan password Google. Token OAuth
disimpan di `Config.youtube_token_file` dan hanya berisi kredensial terbatas
untuk scope upload/readonly.
"""
from __future__ import annotations

from .auth import SCOPES, YouTubeAuthError, load_credentials, validate_client_secrets
from .uploader import (
    UploadResult,
    build_video_body,
    upload_video,
    video_fingerprint,
)

__all__ = [
    "SCOPES",
    "YouTubeAuthError",
    "load_credentials",
    "validate_client_secrets",
    "UploadResult",
    "build_video_body",
    "upload_video",
    "video_fingerprint",
]
