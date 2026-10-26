"""Utilitas penyimpanan: ukuran direktori + penegakan kuota disk."""
from __future__ import annotations

import shutil
from pathlib import Path


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def human_mb(value_bytes: int) -> str:
    return f"{value_bytes / (1024 * 1024):.1f} MB"


def free_disk_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def check_disk_quota(data_dir: Path, quota_mb: int) -> None:
    """Lepas DiskQuotaExceeded bila direktori data melewati kuota."""
    from ..errors import DiskQuotaExceeded

    used = dir_size_bytes(data_dir)
    quota_bytes = quota_mb * 1024 * 1024
    if used > quota_bytes:
        raise DiskQuotaExceeded(
            f"Pemakaian data {human_mb(used)} melewati kuota {quota_mb} MB. "
            "Bersihkan data lama atau naikkan DISK_QUOTA_MB."
        )
