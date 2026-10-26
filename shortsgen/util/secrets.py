"""Redaksi rahasia.

Aturan penting: API key TIDAK BOLEH muncul di log, pesan error, atau laporan.
Modul ini menyediakan pola deteksi generik plus registry nilai rahasia yang
diketahui (didaftarkan saat konfigurasi dimuat) agar bisa diredaksi di mana pun.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Pola rahasia umum (9Router key, Google API key, OAuth token, dsb.)
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{10,}"),
    re.compile(r"sk_9router[0-9A-Za-z_\-]*"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[\"']?([A-Za-z0-9_\-\.]{12,})"),
)

_MASK_TOKEN = "***REDACTED***"


def mask(secret: str | None, *, keep: int = 3) -> str:
    """Tampilkan bentuk aman sebuah rahasia: `abc***xy (len=35)`."""
    if not secret:
        return "<none>"
    s = str(secret)
    if len(s) <= keep * 2:
        return f"*** (len={len(s)})"
    return f"{s[:keep]}***{s[-2:]} (len={len(s)})"


class SecretRedactor:
    """Menyimpan nilai rahasia yang diketahui dan meredaksinya dari teks/objek."""

    def __init__(self, secrets: Iterable[str] | None = None):
        self._secrets: list[str] = []
        for s in secrets or ():
            self.register(s)

    def register(self, secret: str | None) -> None:
        if secret and isinstance(secret, str) and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: Any) -> str:
        s = str(text)
        for secret in self._secrets:
            if secret in s:
                s = s.replace(secret, _MASK_TOKEN)
        for pat in _PATTERNS:
            s = pat.sub(self._sub, s)
        return s

    @staticmethod
    def _sub(match: re.Match[str]) -> str:
        if match.re.groups == 2:  # pola key=value -> pertahankan labelnya
            return f"{match.group(1)}={_MASK_TOKEN}"
        return _MASK_TOKEN

    def redact_obj(self, obj: Any) -> Any:
        """Redaksi rekursif untuk struktur dict/list/str (aman untuk serialisasi)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj
