"""Logging: konsol + JSONL, dengan filter redaksi rahasia.

- Semua pesan melewati SecretRedactor sebelum ditulis.
- File log: <logs_dir>/app.jsonl (satu JSON per baris) untuk audit.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .util.secrets import SecretRedactor

LOGGER_NAME = "shortsgen"
_REDACTOR: SecretRedactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    """Redactor global yang dipakai logging + provider + laporan."""
    return _REDACTOR


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pesan rusak tidak boleh menjatuhkan aplikasi
            return True
        redacted = _REDACTOR.redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _REDACTOR.redact(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload["extra"] = _REDACTOR.redact_obj(extra)
        if record.exc_info:
            payload["exc"] = _REDACTOR.redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    logs_dir: Optional[Path] = None,
    *,
    level: int = logging.INFO,
    console: bool = True,
    console_level: int = logging.WARNING,
    jsonl_file: bool = True,
) -> logging.Logger:
    """Konfigurasi logger aplikasi. Aman dipanggil berulang.

    - Konsol: hanya WARNING+ (agar output CLI bersih).
    - File JSONL: INFO+ (jejak lengkap, rahasia diredaksi).
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    # Hindari duplikasi handler saat dipanggil ulang.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    redact = _RedactFilter()

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setLevel(console_level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        console_handler.addFilter(redact)
        logger.addHandler(console_handler)

    if jsonl_file and logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / "app.jsonl", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(_JsonlFormatter())
        file_handler.addFilter(redact)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """Ambil logger anak dari logger aplikasi."""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
