"""Stage pipeline Fase C: riset, naskah, review, storyboard.

Registry `build_handlers()` merakit handler untuk setiap stage yang sudah
diimplementasikan. Stage yang belum punya handler akan tercatat sebagai "gap"
oleh orkestrator (tidak diklaim berfungsi).
"""
from __future__ import annotations

from typing import Any, Optional

from ...config import Config
from ...logging_setup import get_logger
from ...providers.base import ProviderBundle
from ..orchestrator import ApprovalGate, StageHandler

__all__ = ["build_handlers", "IMPLEMENTED_STAGES"]

IMPLEMENTED_STAGES: tuple[str, ...] = (
    "research",
    "script",
    "review",
    "scenes",
    "assets",
    "tts",
    "subtitles",
    "render",
    "qa",
    "preview",
    "upload",
)


def build_handlers(
    config: Config,
    providers: ProviderBundle,
    *,
    logger: Optional[Any] = None,
    stages: Optional[tuple[str, ...]] = None,
) -> dict[str, StageHandler]:
    """Rakit handler stage yang tersedia. `stages` membatasi bila diinginkan."""
    log = logger or get_logger("stages")
    handlers: dict[str, StageHandler] = {}
    wanted = stages or IMPLEMENTED_STAGES

    if "research" in wanted:
        from .research import ResearchStage

        handlers["research"] = ResearchStage(providers, config, logger=log)
    if "script" in wanted:
        from .script import ScriptStage

        handlers["script"] = ScriptStage(providers, config, logger=log)
    if "review" in wanted:
        from .review import ReviewStage

        handlers["review"] = ReviewStage(providers, config, logger=log)
    if "scenes" in wanted:
        from .scenes import ScenePlannerStage

        handlers["scenes"] = ScenePlannerStage(providers, config, logger=log)
    if "assets" in wanted:
        from .assets import AssetsStage

        handlers["assets"] = AssetsStage(providers, config, logger=log)
    if "tts" in wanted:
        from .tts import TTSStage

        handlers["tts"] = TTSStage(providers, config, logger=log)
    if "subtitles" in wanted:
        from .subtitles import SubtitleStage

        handlers["subtitles"] = SubtitleStage(providers, config, logger=log)
    if "render" in wanted:
        from .render import RenderStage

        handlers["render"] = RenderStage(providers, config, logger=log)
    if "qa" in wanted:
        from .qa import QAStage

        handlers["qa"] = QAStage(providers, config, logger=log)
    if "preview" in wanted:
        from .preview import PreviewStage

        handlers["preview"] = PreviewStage(providers, config, logger=log)
    if "upload" in wanted:
        from .upload import UploadStage

        handlers["upload"] = UploadStage(providers, config, logger=log)

    # Gerbang approval selalu tersedia (mock: lolos otomatis; live: menunggu manusia).
    if "approval" in wanted or stages is None:
        handlers.setdefault("approval", ApprovalGate())

    return handlers
