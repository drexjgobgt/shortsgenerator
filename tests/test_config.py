"""Test validasi konfigurasi."""
from __future__ import annotations

from pathlib import Path

import pytest

from shortsgen.config import discover_local_api_key, load_config, paid_model_hint, resolve_api_key
from shortsgen.errors import ConfigError


def test_default_mode_is_mock_and_requires_no_key(tmp_path: Path) -> None:
    config = load_config(environ={"DATA_DIR": str(tmp_path / "d"), "LOGS_DIR": str(tmp_path / "l")})
    assert config.mode == "mock"
    assert config.nine_router_api_key == ""
    assert config.require_approval is True
    assert config.upload_visibility == "private"
    assert config.allow_paid_providers is False


def test_live_mode_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(
            environ={
                "APP_MODE": "live",
                "DATA_DIR": str(tmp_path / "d"),
                "LOGS_DIR": str(tmp_path / "l"),
            }
        )
    assert any("NINE_ROUTER_API_KEY" in issue for issue in exc_info.value.issues)


def test_live_mode_accepts_local_db_discovery_flag(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY_FROM_LOCAL_DB": "true",
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    assert config.nine_router_api_key_from_local_db is True


def test_invalid_values_collect_multiple_issues(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(
            environ={
                "APP_MODE": "banana",
                "HTTP_TIMEOUT_S": "abc",
                "MAX_VIDEO_SECONDS": "1000",
                "UPLOAD_VISIBILITY": "everyone",
                "REQUIRE_APPROVAL": "false",
                "DATA_DIR": str(tmp_path / "d"),
                "LOGS_DIR": str(tmp_path / "l"),
            }
        )
    issues = " | ".join(exc_info.value.issues)
    assert "APP_MODE" in issues
    assert "HTTP_TIMEOUT_S" in issues
    assert "MAX_VIDEO_SECONDS" in issues
    assert "UPLOAD_VISIBILITY" in issues
    assert "REQUIRE_APPROVAL" in issues
    assert len(exc_info.value.issues) >= 5


def test_public_visibility_rejected_in_live_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(
            environ={
                "APP_MODE": "live",
                "NINE_ROUTER_API_KEY": "sk-x-123456",
                "UPLOAD_VISIBILITY": "public",
                "DATA_DIR": str(tmp_path / "d"),
                "LOGS_DIR": str(tmp_path / "l"),
            }
        )
    assert any("UPLOAD_VISIBILITY" in issue for issue in exc_info.value.issues)


def test_env_file_parsing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_MODE=mock\n"
        f"DATA_DIR={tmp_path / 'envdata'}\n"
        f"LOGS_DIR={tmp_path / 'envlogs'}\n"
        "LLM_MODEL=kios/model-a\n"
        "LLM_MODEL_FALLBACKS=ag/model-b, kios/model-a, gemini/model-c\n",
        encoding="utf-8",
    )
    config = load_config(env_file)
    assert config.llm_model == "kios/model-a"
    # fallback duplikat dengan primary dibersihkan
    assert config.llm_model_fallbacks == ("ag/model-b", "gemini/model-c")
    assert str(config.data_dir).endswith("envdata")


def test_summary_never_leaks_key(tmp_path: Path) -> None:
    secret = "sk-supersecret-abcdef123456"
    config = load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY": secret,
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    summary = config.summary()
    serialized = str(summary)
    assert secret not in serialized
    assert "***" in summary["nine_router_api_key"]


def test_resolve_api_key_prefers_env(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "APP_MODE": "live",
            "NINE_ROUTER_API_KEY": "sk-fromenv-123456",
            "NINE_ROUTER_API_KEY_FROM_LOCAL_DB": "true",
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    assert resolve_api_key(config) == "sk-fromenv-123456"


def test_discover_local_api_key_from_sqlite(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE apiKeys (id TEXT, key TEXT, name TEXT, isActive INTEGER, createdAt TEXT)"
    )
    con.execute(
        "INSERT INTO apiKeys VALUES ('1', 'sk-fromdb-1234567890', 'Test Key', 1, '2026-01-01')"
    )
    con.commit()
    con.close()
    assert discover_local_api_key(db) == "sk-fromdb-1234567890"


def test_discover_local_api_key_missing_db_returns_none(tmp_path: Path) -> None:
    assert discover_local_api_key(tmp_path / "nope.sqlite") is None


def test_summary_masks_stock_and_search_keys(tmp_path: Path) -> None:
    pexels = "pexels-secret-key-abcdef123456"
    pixabay = "00000000-000000000000000000000000"
    config = load_config(
        environ={
            "APP_MODE": "mock",
            "PEXELS_API_KEY": pexels,
            "PIXABAY_API_KEY": pixabay,
            "DATA_DIR": str(tmp_path / "d"),
            "LOGS_DIR": str(tmp_path / "l"),
        }
    )
    serialized = str(config.summary())
    assert pexels not in serialized
    assert pixabay not in serialized
    assert "000***00" in config.summary()["pixabay_api_key"]


def test_stock_prefer_validation(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(
            environ={
                "APP_MODE": "mock",
                "STOCK_PREFER": "shutterstock",
                "DATA_DIR": str(tmp_path / "d"),
                "LOGS_DIR": str(tmp_path / "l"),
            }
        )
    assert any("STOCK_PREFER" in issue for issue in exc_info.value.issues)


def test_paid_model_hint() -> None:
    assert paid_model_hint("xai/grok-video") is True
    assert paid_model_hint("edge-tts/id-ID-ArdiNeural") is False
