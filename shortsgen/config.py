"""Pemuatan & validasi konfigurasi dari environment / file .env.

Prinsip:
- Mode default `mock` (tanpa jaringan) agar aman.
- Mode `live` wajib punya API key (dari env atau discovery DB lokal 9Router).
- Rahasia tidak pernah dicetak; gunakan `Config.summary()`.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from dotenv import load_dotenv

from .errors import ConfigError
from .util.secrets import mask

APP_MODES = ("mock", "live")
UPLOAD_VISIBILITIES = ("private", "unlisted", "public")
RENDER_PRESETS = ("fast", "final")
_PAID_PROVIDER_HINTS = ("openai", "xai", "fal-ai", "black-forest-labs", "huggingface")


def _env_paths_for_local_db() -> list[Path]:
    """Lokasi umum DB 9Router pada Windows / macOS / Linux."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "9router" / "db" / "data.sqlite")
    home = Path.home()
    paths.append(home / ".9router" / "db" / "data.sqlite")
    return paths


def discover_local_api_key(db_path: Optional[Path] = None) -> Optional[str]:
    """Baca API key 9Router dari DB lokal (untuk kemudahan; harus opt-in).

    Selalu dibuka read-only. Tidak menulis/mengubah apa pun.
    """
    candidates = [db_path] if db_path else _env_paths_for_local_db()
    for path in candidates:
        if not path or not path.exists():
            continue
        try:
            uri = path.as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            continue
    return None


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    value = env.get(key)
    return default if value is None or value.strip() == "" else value.strip()


_VOICE_ID_RE = re.compile(r"^(?:[a-z0-9\-]+/)?[a-z]{2,3}-[A-Za-z]{2}[A-Za-z0-9\-]*$")


def _looks_like_voice_id(value: str) -> bool:
    """Validasi ringan id suara (mis. id-ID-GadisNeural / edge-tts/id-ID-ArdiNeural)."""
    return bool(_VOICE_ID_RE.match(value.strip()))


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _get(env, key)
    if raw == "":
        return default
    return int(raw)


def _get_float(env: Mapping[str, str], key: str, default: float, issues: list[str]) -> float:
    raw = _get(env, key)
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        issues.append(f"{key}: harus berupa angka")
        return default


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key).lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"{key}: nilai boolean tidak valid: {raw!r}")


@dataclass(frozen=True)
class Config:
    """Konfigurasi aplikasi yang sudah tervalidasi."""

    mode: str
    data_dir: Path
    logs_dir: Path

    nine_router_base_url: str
    nine_router_api_key: str
    nine_router_api_key_from_local_db: bool

    llm_model: str
    llm_model_fallbacks: tuple[str, ...]
    tts_model: str
    image_model: str
    video_model: str
    stt_model: str
    search_provider: str
    fetch_provider: str

    http_timeout_s: int
    http_max_retries: int
    max_video_seconds: int
    max_asset_mb: int
    max_parallel_jobs: int
    disk_quota_mb: int

    target_duration_s: int
    script_max_scenes: int
    review_llm_pass: bool
    block_unverified_claims: bool
    max_ai_images_per_job: int
    tts_voice_mode: str
    tts_engine: str
    tts_voice_profile: str
    tts_emotion: bool
    tts_postprocess: bool
    max_scene_seconds: float
    captions_style: str
    hook_overlay: bool
    music_mode: str
    music_dir: str
    music_volume_db: float
    music_bpm: int
    sfx_enabled: bool
    sfx_mode: str
    render_preset: str
    render_engine: str
    motion_intensity: str
    transitions_enabled: bool
    music_sections: bool
    render_width: int
    render_height: int
    render_fps: int

    series_character: str
    reference_chain: bool
    visual_style: str
    stock_policy: str
    stock_max_clips_per_job: int
    visual_relevance_check: str
    visual_relevance_min: float
    vision_model: str
    evaluation_report: bool
    cache_enabled: bool
    cache_dir: Path
    cache_ttl_hours: float

    pexels_api_key: str
    pixabay_api_key: str
    stock_local_dir: str
    stock_prefer: str

    allow_paid_providers: bool
    upload_visibility: str
    require_approval: bool

    ffmpeg_path: str

    python_path: str = field(default="", compare=False)

    youtube_client_secrets: str = ""
    youtube_token_path: str = ""
    youtube_made_for_kids: bool = False
    youtube_synthetic_disclosure: bool = True
    youtube_upload_chunk_mb: int = 8
    youtube_processing_timeout_s: int = 60

    # -- helper ------------------------------------------------------

    @property
    def youtube_client_secrets_file(self) -> Path:
        if self.youtube_client_secrets:
            return Path(self.youtube_client_secrets).expanduser()
        return Path(__file__).resolve().parent.parent / "client_secrets.json"

    @property
    def youtube_token_file(self) -> Path:
        if self.youtube_token_path:
            return Path(self.youtube_token_path).expanduser()
        return self.data_dir / "auth" / "token.json"

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shortsgen.sqlite"

    def summary(self) -> dict[str, Any]:
        """Ringkasan aman untuk log/laporan (tanpa rahasia)."""
        return {
            "mode": self.mode,
            "data_dir": str(self.data_dir),
            "logs_dir": str(self.logs_dir),
            "nine_router_base_url": self.nine_router_base_url,
            "nine_router_api_key": mask(self.nine_router_api_key),
            "api_key_source": (
                "env"
                if os.environ.get("NINE_ROUTER_API_KEY")
                else ("local_db" if self.nine_router_api_key else "none")
            ),
            "llm_model": self.llm_model,
            "llm_model_fallbacks": list(self.llm_model_fallbacks),
            "tts_model": self.tts_model,
            "image_model": self.image_model or "<not_configured>",
            "video_model": self.video_model or "<not_configured>",
            "stt_model": self.stt_model or "<not_configured>",
            "search_provider": self.search_provider or "<not_configured>",
            "fetch_provider": self.fetch_provider or "<not_configured>",
            "http_timeout_s": self.http_timeout_s,
            "http_max_retries": self.http_max_retries,
            "max_video_seconds": self.max_video_seconds,
            "max_asset_mb": self.max_asset_mb,
            "target_duration_s": self.target_duration_s,
            "script_max_scenes": self.script_max_scenes,
            "review_llm_pass": self.review_llm_pass,
            "block_unverified_claims": self.block_unverified_claims,
            "max_ai_images_per_job": self.max_ai_images_per_job,
            "tts_voice_mode": self.tts_voice_mode,
            "tts_engine": self.tts_engine,
            "tts_voice_profile": self.tts_voice_profile,
            "tts_emotion": self.tts_emotion,
            "tts_postprocess": self.tts_postprocess,
            "max_scene_seconds": self.max_scene_seconds,
            "captions_style": self.captions_style,
            "hook_overlay": self.hook_overlay,
            "music_mode": self.music_mode,
            "music_volume_db": self.music_volume_db,
            "music_bpm": self.music_bpm,
            "sfx_enabled": self.sfx_enabled,
            "sfx_mode": self.sfx_mode,
            "render_preset": self.render_preset,
            "render_engine": self.render_engine,
            "motion_intensity": self.motion_intensity,
            "transitions_enabled": self.transitions_enabled,
            "music_sections": self.music_sections,
            "visual_style": self.visual_style or "<auto>",
            "series_character": self.series_character or "<none>",
            "reference_chain": self.reference_chain,
            "stock_policy": self.stock_policy,
            "stock_max_clips_per_job": self.stock_max_clips_per_job,
            "visual_relevance_check": self.visual_relevance_check,
            "visual_relevance_min": self.visual_relevance_min,
            "evaluation_report": self.evaluation_report,
            "cache_enabled": self.cache_enabled,
            "cache_dir": str(self.cache_dir),
            "cache_ttl_hours": self.cache_ttl_hours,
            "render": f"{self.render_width}x{self.render_height}@{self.render_fps}",
            "pexels_api_key": mask(self.pexels_api_key) if self.pexels_api_key else "<none>",
            "pixabay_api_key": mask(self.pixabay_api_key) if self.pixabay_api_key else "<none>",
            "stock_local_dir": self.stock_local_dir or "<none>",
            "stock_prefer": self.stock_prefer or "<auto>",
            "max_parallel_jobs": self.max_parallel_jobs,
            "disk_quota_mb": self.disk_quota_mb,
            "allow_paid_providers": self.allow_paid_providers,
            "upload_visibility": self.upload_visibility,
            "require_approval": self.require_approval,
            "youtube_client_secrets": str(self.youtube_client_secrets_file),
            "youtube_token_path": str(self.youtube_token_file),
            "youtube_made_for_kids": self.youtube_made_for_kids,
            "youtube_synthetic_disclosure": self.youtube_synthetic_disclosure,
            "youtube_upload_chunk_mb": self.youtube_upload_chunk_mb,
            "ffmpeg_path": self.ffmpeg_path or "<not_found>",
        }


def load_config(
    env_file: Optional[Path | str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Config:
    """Muat konfigurasi dari .env + environment, lalu validasi.

    Raises:
        ConfigError: bila ada satu atau lebih masalah konfigurasi.
    """
    if environ is None:
        if env_file is not None:
            # File .env eksplisit (mis. --env-file) menang atas environment proses.
            load_dotenv(env_file, override=True)
        else:
            default_env = Path(".env")
            if default_env.exists():
                # .env default tidak menimpa environment yang sudah di-set (Docker/CI).
                load_dotenv(default_env, override=False)
        env: Mapping[str, str] = os.environ
    else:
        env = environ

    issues: list[str] = []

    def safe_int(key: str, default: int) -> int:
        try:
            return _get_int(env, key, default)
        except ValueError:
            issues.append(f"{key}: harus berupa bilangan bulat")
            return default

    def safe_bool(key: str, default: bool) -> bool:
        try:
            return _get_bool(env, key, default)
        except ValueError as exc:
            issues.append(str(exc))
            return default

    mode = _get(env, "APP_MODE", "mock").lower()
    if mode not in APP_MODES:
        issues.append(f"APP_MODE harus salah satu dari {APP_MODES}, bukan {mode!r}")

    base_url = _get(env, "NINE_ROUTER_BASE_URL", "http://localhost:20128").rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        issues.append("NINE_ROUTER_BASE_URL harus diawali http:// atau https://")

    api_key = _get(env, "NINE_ROUTER_API_KEY")
    key_from_db = safe_bool("NINE_ROUTER_API_KEY_FROM_LOCAL_DB", False)
    if mode == "live" and not api_key and not key_from_db:
        issues.append(
            "Mode live membutuhkan NINE_ROUTER_API_KEY (atau set "
            "NINE_ROUTER_API_KEY_FROM_LOCAL_DB=true untuk membaca DB lokal 9Router)"
        )

    llm_model = _get(env, "LLM_MODEL", "kios/deepseek-v4.1-flash-free")
    llm_fallbacks = tuple(
        m.strip() for m in _get(env, "LLM_MODEL_FALLBACKS").split(",") if m.strip()
    )
    if llm_model in llm_fallbacks:
        llm_fallbacks = tuple(m for m in llm_fallbacks if m != llm_model)

    tts_model = _get(env, "TTS_MODEL", "edge-tts/id-ID-ArdiNeural")
    image_model = _get(env, "IMAGE_MODEL", "ag/gemini-3.1-flash-image")
    video_model = _get(env, "VIDEO_MODEL")
    stt_model = _get(env, "STT_MODEL")
    search_provider = _get(env, "SEARCH_PROVIDER")
    fetch_provider = _get(env, "FETCH_PROVIDER")

    data_dir = Path(_get(env, "DATA_DIR", "./data")).expanduser()
    logs_dir = Path(_get(env, "LOGS_DIR", "./logs")).expanduser()

    http_timeout_s = safe_int("HTTP_TIMEOUT_S", 180)
    http_max_retries = safe_int("HTTP_MAX_RETRIES", 2)
    max_video_seconds = safe_int("MAX_VIDEO_SECONDS", 120)
    max_asset_mb = safe_int("MAX_ASSET_MB", 200)
    max_parallel_jobs = safe_int("MAX_PARALLEL_JOBS", 1)
    disk_quota_mb = safe_int("DISK_QUOTA_MB", 2000)

    target_duration_s = safe_int("TARGET_DURATION_S", 30)
    script_max_scenes = safe_int("SCRIPT_MAX_SCENES", 10)
    review_llm_pass = safe_bool("REVIEW_LLM_PASS", False)
    block_unverified = safe_bool("BLOCK_UNVERIFIED_CLAIMS", False)
    max_ai_images = safe_int("MAX_AI_IMAGES_PER_JOB", 8)
    tts_voice_mode = _get(env, "TTS_VOICE_MODE", "auto").lower()
    tts_engine = _get(env, "TTS_ENGINE", "auto").lower()
    tts_voice_profile = _get(env, "TTS_VOICE_PROFILE", "auto").lower()
    tts_emotion = safe_bool("TTS_EMOTION", True)
    tts_postprocess = safe_bool("TTS_POSTPROCESS", True)
    max_scene_seconds = _get_float(env, "MAX_SCENE_SECONDS", 4.0, issues)
    captions_style = _get(env, "CAPTIONS_STYLE", "dynamic").lower()
    hook_overlay = safe_bool("HOOK_OVERLAY", True)
    music_mode = _get(env, "MUSIC_MODE", "procedural").lower()
    music_dir = _get(env, "MUSIC_DIR")
    music_volume_db = _get_float(env, "MUSIC_VOLUME_DB", -18.0, issues)
    music_bpm = safe_int("MUSIC_BPM", 120)
    sfx_enabled = safe_bool("SFX_ENABLED", True)
    sfx_mode = _get(env, "SFX_MODE", "narrative").lower()
    render_preset = _get(env, "RENDER_PRESET", "fast").lower()
    render_engine = _get(env, "RENDER_ENGINE", "cinematic").lower()
    motion_intensity = _get(env, "MOTION_INTENSITY", "medium").lower()
    transitions_enabled = safe_bool("TRANSITIONS", True)
    music_sections = safe_bool("MUSIC_SECTIONS", True)
    render_width = safe_int("RENDER_WIDTH", 1080)
    render_height = safe_int("RENDER_HEIGHT", 1920)
    render_fps = safe_int("RENDER_FPS", 30)

    series_character = _get(env, "SERIES_CHARACTER")
    reference_chain = safe_bool("REFERENCE_CHAIN", True)
    visual_style = _get(env, "VISUAL_STYLE")
    stock_policy = _get(env, "STOCK_POLICY", "ai_first").lower()
    stock_max_clips = safe_int("STOCK_MAX_CLIPS_PER_JOB", 2)
    visual_relevance_check = _get(env, "VISUAL_RELEVANCE_CHECK", "heuristic").lower()
    visual_relevance_min = _get_float(env, "VISUAL_RELEVANCE_MIN", 6.0, issues)
    vision_model = _get(env, "VISION_MODEL", "ag/gemini-3.8-flash")
    evaluation_report = safe_bool("EVALUATION_REPORT", True)
    cache_enabled = safe_bool("CACHE_ENABLED", True)
    cache_dir = Path(_get(env, "CACHE_DIR", str(data_dir / "cache"))).expanduser()
    cache_ttl_hours = _get_float(env, "CACHE_TTL_HOURS", 168.0, issues)

    pexels_api_key = _get(env, "PEXELS_API_KEY")
    pixabay_api_key = _get(env, "PIXABAY_API_KEY")
    stock_local_dir = _get(env, "STOCK_LOCAL_DIR")
    stock_prefer = _get(env, "STOCK_PREFER").lower()

    allow_paid = safe_bool("ALLOW_PAID_PROVIDERS", False)

    upload_visibility = _get(env, "UPLOAD_VISIBILITY", "private").lower()
    if upload_visibility not in UPLOAD_VISIBILITIES:
        issues.append(f"UPLOAD_VISIBILITY harus salah satu dari {UPLOAD_VISIBILITIES}")

    require_approval = safe_bool("REQUIRE_APPROVAL", True)

    project_root = Path(__file__).resolve().parent.parent
    youtube_client_secrets = _get(
        env, "YOUTUBE_CLIENT_SECRETS", str(project_root / "client_secrets.json")
    )
    youtube_token_path = _get(env, "YOUTUBE_TOKEN_PATH", str(data_dir / "auth" / "token.json"))
    youtube_made_for_kids = safe_bool("YOUTUBE_MADE_FOR_KIDS", False)
    youtube_synthetic_disclosure = safe_bool("YOUTUBE_SYNTHETIC_DISCLOSURE", True)
    youtube_upload_chunk_mb = _get_int(env, "YOUTUBE_UPLOAD_CHUNK_MB", 8)
    youtube_processing_timeout_s = _get_int(env, "YOUTUBE_PROCESSING_TIMEOUT_S", 60)

    ffmpeg_path = _get(env, "FFMPEG_PATH")

    # Aturan rentang
    if http_timeout_s < 5:
        issues.append("HTTP_TIMEOUT_S minimal 5 detik")
    if not 0 <= http_max_retries <= 10:
        issues.append("HTTP_MAX_RETRIES harus antara 0 dan 10")
    if not 5 <= max_video_seconds <= 600:
        issues.append("MAX_VIDEO_SECONDS harus antara 5 dan 600")
    if max_asset_mb < 1:
        issues.append("MAX_ASSET_MB minimal 1")
    if not 1 <= max_parallel_jobs <= 8:
        issues.append("MAX_PARALLEL_JOBS harus antara 1 dan 8")
    if disk_quota_mb < 10:
        issues.append("DISK_QUOTA_MB minimal 10")
    if not 15 <= target_duration_s <= 120:
        issues.append("TARGET_DURATION_S harus antara 15 dan 120")
    if not 3 <= script_max_scenes <= 12:
        issues.append("SCRIPT_MAX_SCENES harus antara 3 dan 12")
    if not 0 <= max_ai_images <= 30:
        issues.append("MAX_AI_IMAGES_PER_JOB harus antara 0 dan 30")
    if render_preset not in RENDER_PRESETS:
        issues.append(f"RENDER_PRESET harus salah satu dari {RENDER_PRESETS}")
    if not 320 <= render_width <= 2160:
        issues.append("RENDER_WIDTH harus antara 320 dan 2160")
    if not 480 <= render_height <= 3840:
        issues.append("RENDER_HEIGHT harus antara 480 dan 3840")
    if render_height < render_width:
        issues.append("RENDER_HEIGHT harus >= RENDER_WIDTH (format vertikal 9:16)")
    if not 15 <= render_fps <= 60:
        issues.append("RENDER_FPS harus antara 15 dan 60")
    if not 1 <= youtube_upload_chunk_mb <= 64:
        issues.append("YOUTUBE_UPLOAD_CHUNK_MB harus antara 1 dan 64")
    if not 0 <= youtube_processing_timeout_s <= 600:
        issues.append("YOUTUBE_PROCESSING_TIMEOUT_S harus antara 0 dan 600")
    if tts_voice_mode not in ("auto", "male", "female") and not _looks_like_voice_id(tts_voice_mode):
        issues.append(
            "TTS_VOICE_MODE harus 'auto', 'male', 'female', atau id suara yang valid "
            "(mis. id-ID-GadisNeural atau edge-tts/id-ID-GadisNeural)"
        )
    if stock_prefer and stock_prefer not in ("pexels", "pixabay", "local-stock"):
        issues.append("STOCK_PREFER harus 'pexels', 'pixabay', atau 'local-stock'")
    if not 1.5 <= max_scene_seconds <= 8.0:
        issues.append("MAX_SCENE_SECONDS harus antara 1.5 dan 8 (ganti visual tiap 2-4 dtk)")
    if captions_style not in ("static", "dynamic"):
        issues.append("CAPTIONS_STYLE harus 'static' atau 'dynamic'")
    if music_mode not in ("procedural", "folder", "off"):
        issues.append("MUSIC_MODE harus 'procedural', 'folder', atau 'off'")
    if music_mode == "folder" and not music_dir:
        issues.append("MUSIC_MODE=folder membutuhkan MUSIC_DIR")
    if not -40.0 <= music_volume_db <= -6.0:
        issues.append("MUSIC_VOLUME_DB harus antara -40 dan -6 (disarankan -18)")
    if not 60 <= music_bpm <= 180:
        issues.append("MUSIC_BPM harus antara 60 dan 180")
    if tts_engine not in ("auto", "edge-local", "9router", "mock"):
        issues.append("TTS_ENGINE harus 'auto', 'edge-local', '9router', atau 'mock'")
    if render_engine not in ("cinematic", "legacy"):
        issues.append("RENDER_ENGINE harus 'cinematic' atau 'legacy'")
    if motion_intensity not in ("low", "medium", "high"):
        issues.append("MOTION_INTENSITY harus 'low', 'medium', atau 'high'")
    if stock_policy not in ("ai_first", "stock_first", "off"):
        issues.append("STOCK_POLICY harus 'ai_first', 'stock_first', atau 'off'")
    if not 0 <= stock_max_clips <= 10:
        issues.append("STOCK_MAX_CLIPS_PER_JOB harus antara 0 dan 10")
    if visual_relevance_check not in ("off", "heuristic", "llm"):
        issues.append("VISUAL_RELEVANCE_CHECK harus 'off', 'heuristic', atau 'llm'")
    if not 0.0 <= visual_relevance_min <= 10.0:
        issues.append("VISUAL_RELEVANCE_MIN harus antara 0 dan 10")
    if sfx_mode not in ("off", "basic", "narrative"):
        issues.append("SFX_MODE harus 'off', 'basic', atau 'narrative'")
    if cache_ttl_hours < 0:
        issues.append("CACHE_TTL_HOURS tidak boleh negatif")

    # Keamanan default
    if not require_approval:
        issues.append(
            "REQUIRE_APPROVAL=false tidak diizinkan pada versi ini: persetujuan manual wajib"
        )
    if upload_visibility != "private" and mode == "live":
        issues.append(
            "Default UPLOAD_VISIBILITY harus private; gunakan approve --visibility public "
            "untuk persetujuan publik khusus satu job"
        )

    if allow_paid and mode == "live":
        # Bukan error, hanya pengingat yang dicatat pemanggil lewat log.
        pass

    if issues:
        raise ConfigError(issues)

    return Config(
        mode=mode,
        data_dir=data_dir,
        logs_dir=logs_dir,
        nine_router_base_url=base_url,
        nine_router_api_key=api_key,
        nine_router_api_key_from_local_db=key_from_db,
        llm_model=llm_model,
        llm_model_fallbacks=llm_fallbacks,
        tts_model=tts_model,
        image_model=image_model,
        video_model=video_model,
        stt_model=stt_model,
        search_provider=search_provider,
        fetch_provider=fetch_provider,
        http_timeout_s=http_timeout_s,
        http_max_retries=http_max_retries,
        max_video_seconds=max_video_seconds,
        max_asset_mb=max_asset_mb,
        max_parallel_jobs=max_parallel_jobs,
        disk_quota_mb=disk_quota_mb,
        target_duration_s=target_duration_s,
        script_max_scenes=script_max_scenes,
        review_llm_pass=review_llm_pass,
        block_unverified_claims=block_unverified,
        max_ai_images_per_job=max_ai_images,
        tts_voice_mode=tts_voice_mode,
        tts_engine=tts_engine,
        tts_voice_profile=tts_voice_profile,
        tts_emotion=tts_emotion,
        tts_postprocess=tts_postprocess,
        max_scene_seconds=max_scene_seconds,
        captions_style=captions_style,
        hook_overlay=hook_overlay,
        music_mode=music_mode,
        music_dir=music_dir,
        music_volume_db=music_volume_db,
        music_bpm=music_bpm,
        sfx_enabled=sfx_enabled,
        sfx_mode=sfx_mode,
        render_preset=render_preset,
        render_engine=render_engine,
        motion_intensity=motion_intensity,
        transitions_enabled=transitions_enabled,
        music_sections=music_sections,
        render_width=render_width,
        render_height=render_height,
        render_fps=render_fps,
        series_character=series_character,
        reference_chain=reference_chain,
        visual_style=visual_style,
        stock_policy=stock_policy,
        stock_max_clips_per_job=stock_max_clips,
        visual_relevance_check=visual_relevance_check,
        visual_relevance_min=visual_relevance_min,
        vision_model=vision_model,
        evaluation_report=evaluation_report,
        cache_enabled=cache_enabled,
        cache_dir=cache_dir,
        cache_ttl_hours=cache_ttl_hours,
        pexels_api_key=pexels_api_key,
        pixabay_api_key=pixabay_api_key,
        stock_local_dir=stock_local_dir,
        stock_prefer=stock_prefer,
        allow_paid_providers=allow_paid,
        upload_visibility=upload_visibility,
        require_approval=require_approval,
        ffmpeg_path=ffmpeg_path,
        youtube_client_secrets=youtube_client_secrets,
        youtube_token_path=youtube_token_path,
        youtube_made_for_kids=youtube_made_for_kids,
        youtube_synthetic_disclosure=youtube_synthetic_disclosure,
        youtube_upload_chunk_mb=youtube_upload_chunk_mb,
        youtube_processing_timeout_s=youtube_processing_timeout_s,
    )


def resolve_api_key(config: Config) -> str:
    """Tentukan API key efektif (env dulu, lalu DB lokal bila diizinkan)."""
    if config.nine_router_api_key:
        return config.nine_router_api_key
    if config.nine_router_api_key_from_local_db:
        key = discover_local_api_key()
        if key:
            return key
    return ""


def paid_model_hint(model: str) -> bool:
    """Heuristik apakah model mengarah ke provider berbayar (untuk peringatan)."""
    lowered = (model or "").lower()
    return any(h in lowered for h in _PAID_PROVIDER_HINTS)
