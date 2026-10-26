"""Fixture bersama untuk test suite."""
from __future__ import annotations

from pathlib import Path

import pytest

from shortsgen.config import Config, load_config
from shortsgen.media.ffmpeg import find_ffmpeg
from shortsgen.repository import JobRepository

TEST_FFMPEG_PATH = find_ffmpeg() or ""


def ffmpeg_env() -> dict[str, str]:
    """Variabel env FFMPEG_PATH untuk test (bila FFmpeg ada di mesin)."""
    return {"FFMPEG_PATH": TEST_FFMPEG_PATH} if TEST_FFMPEG_PATH else {}


@pytest.fixture()
def mock_config(tmp_path: Path) -> Config:
    """Config mock lengkap dengan direktori terisolasi per-test."""
    return load_config(
        environ={
            "APP_MODE": "mock",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            **ffmpeg_env(),
        }
    )


@pytest.fixture()
def live_config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY": "sk-testkey-1234567890abcdef",
            "DATA_DIR": str(tmp_path / "data"),
            "LOGS_DIR": str(tmp_path / "logs"),
            **ffmpeg_env(),
        }
    )


@pytest.fixture()
def repo(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "test.sqlite")
    yield repository
    repository.close()
