"""Hierarki error aplikasi."""
from __future__ import annotations

from typing import Iterable, Optional


class ShortsGenError(Exception):
    """Basis semua error aplikasi."""


class ConfigError(ShortsGenError):
    """Konfigurasi tidak valid; membawa daftar masalah."""

    def __init__(self, issues: Iterable[str] | str):
        if isinstance(issues, str):
            issues = [issues]
        self.issues = list(issues)
        super().__init__("Konfigurasi tidak valid: " + "; ".join(self.issues))


class DedupeConflict(ShortsGenError):
    """Job dengan topik identik sudah ada."""

    def __init__(self, existing_job_id: str, topic: str):
        self.existing_job_id = existing_job_id
        self.topic = topic
        super().__init__(
            f"Job untuk topik ini sudah ada (job_id={existing_job_id}). "
            "Gunakan --force untuk membuat tetap."
        )


class NotFoundError(ShortsGenError):
    """Entitas tidak ditemukan (job, stage, dsb)."""


class ProviderError(ShortsGenError):
    """Kegagalan provider (jaringan/HTTP/validasi respons)."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        kind: str = "error",
        status: Optional[int] = None,
        retryable: bool = False,
        model: Optional[str] = None,
    ):
        self.provider = provider
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.model = model
        detail = f"[{provider}]" + (f" [{model}]" if model else "")
        super().__init__(f"{detail} {message}")


class NotConfiguredError(ProviderError):
    """Kapabilitas tidak tersedia karena provider/kredensial belum dikonfigurasi."""

    def __init__(self, provider: str, capability: str, message: str = ""):
        self.capability = capability
        msg = message or f"kapabilitas '{capability}' belum dikonfigurasi"
        super().__init__(provider, msg, kind="not_configured", retryable=False)


class StageError(ShortsGenError):
    """Kegagalan saat menjalankan satu stage pipeline."""

    def __init__(self, stage: str, message: str, *, cause: Optional[BaseException] = None):
        self.stage = stage
        self.cause = cause
        super().__init__(f"stage '{stage}' gagal: {message}")


class StageBlocked(ShortsGenError):
    """Stage berhenti terkendali menunggu aksi manusia (mis. approval, kredensial)."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


class DiskQuotaExceeded(ShortsGenError):
    """Pemakaian disk melebihi batas konfigurasi."""
