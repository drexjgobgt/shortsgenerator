"""Factory provider: merakit ProviderBundle dari konfigurasi.

- `APP_MODE=mock`  -> seluruh provider mock (offline penuh)
- `APP_MODE=live`  -> adapter 9Router; kapabilitas tanpa konfigurasi = None
                      (status not_configured, bukan fallback diam-diam)
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, resolve_api_key
from ..errors import ConfigError
from ..logging_setup import get_logger, get_redactor
from .base import ProviderBundle
from .mock import (
    MockFetch,
    MockImage,
    MockLLM,
    MockSearch,
    MockSTT,
    MockTTS,
    MockVideo,
)
from .ninerouter.client import NineRouterClient
from .ninerouter.fetch import FetchAdapter
from .ninerouter.image import ImageAdapter
from .ninerouter.llm import LLMAdapter
from .ninerouter.search import SearchAdapter
from .ninerouter.stt import STTAdapter
from .ninerouter.tts import TTSAdapter
from .ninerouter.video import VideoAdapter
from .stock import build_stock_provider
from ..util.cache import build_cache

__all__ = ["build_providers", "ProviderBundle"]


def build_providers(config: Config, *, logger: Optional[object] = None) -> ProviderBundle:
    """Rakit provider sesuai mode. Tidak melakukan panggilan jaringan."""
    log = logger or get_logger("providers")

    if config.is_mock:
        log.info("Mode mock aktif: seluruh provider offline (tanpa jaringan)")
        return ProviderBundle(
            llm=MockLLM(),
            tts=MockTTS(),
            stt=MockSTT(),
            image=MockImage(),
            video=MockVideo(),
            search=MockSearch(),
            fetch=MockFetch(),
            stock=None,
            mode=config.mode,
        )

    api_key = resolve_api_key(config)
    if not api_key:
        raise ConfigError(
            "Mode live membutuhkan API key 9Router. Isi NINE_ROUTER_API_KEY di .env "
            "atau aktifkan NINE_ROUTER_API_KEY_FROM_LOCAL_DB=true."
        )

    client = NineRouterClient(
        config.nine_router_base_url,
        api_key,
        timeout_s=config.http_timeout_s,
        max_retries=config.http_max_retries,
        redactor=get_redactor(),
        logger=log,
    )

    bundle = ProviderBundle(mode=config.mode)
    cache_root = config.cache_dir
    llm_cache = build_cache(
        cache_root, enabled=config.cache_enabled, ttl_hours=config.cache_ttl_hours, namespace="llm"
    )
    image_cache = build_cache(
        cache_root, enabled=config.cache_enabled, ttl_hours=config.cache_ttl_hours, namespace="image"
    )
    tts_cache = build_cache(
        cache_root, enabled=config.cache_enabled, ttl_hours=config.cache_ttl_hours, namespace="tts"
    )
    bundle.llm = LLMAdapter(
        client,
        config.llm_model,
        config.llm_model_fallbacks,
        logger=log,
        cache=llm_cache,
    )
    bundle.tts = TTSAdapter(client, config.tts_model, cache=tts_cache)
    engine = (config.tts_engine or "auto").lower()
    if engine in ("auto", "edge-local"):
        from .edge_tts_local import EdgeTTSLocal, edge_tts_available

        if edge_tts_available():
            bundle.tts = EdgeTTSLocal(
                default_voice=_default_local_voice(config),
                timeout_s=config.http_timeout_s,
                fallback=bundle.tts,
                cache=tts_cache,
            )
            log.info("TTS lokal edge-tts aktif (prosody); 9Router menjadi fallback")
        elif engine == "edge-local":
            log.warning("TTS_ENGINE=edge-local tetapi paket edge-tts tidak terpasang; memakai 9Router")
    elif engine == "mock":
        bundle.tts = MockTTS()
    bundle.stt = STTAdapter(client, config.stt_model)
    bundle.image = ImageAdapter(
        client,
        config.image_model,
        max_bytes=config.max_asset_mb * 1024 * 1024,
        cache=image_cache,
    )
    bundle.video = VideoAdapter(
        client, config.video_model, max_bytes=config.max_asset_mb * 1024 * 1024
    )
    bundle.search = SearchAdapter(client, config.search_provider)
    bundle.fetch = FetchAdapter(client, config.fetch_provider)
    bundle.stock = build_stock_provider(
        pexels_api_key=config.pexels_api_key,
        pixabay_api_key=config.pixabay_api_key,
        local_dir=config.stock_local_dir,
        prefer=config.stock_prefer,
        logger=log,
    )
    return bundle


def _default_local_voice(config: Config) -> str:
    mode = (config.tts_voice_mode or "auto").lower()
    if mode == "female":
        return "id-ID-GadisNeural"
    if mode != "auto" and "/" in mode:
        return mode.split("/", 1)[-1]
    if mode not in ("auto", "male"):
        return config.tts_voice_mode
    return "id-ID-ArdiNeural"
