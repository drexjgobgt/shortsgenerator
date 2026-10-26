"""Provider stok video: Pexels (gratis), Pixabay (gratis), folder lokal berlisensi."""
from __future__ import annotations

from typing import Any, Optional

from .base import StockClip, StockProvider
from .local import LocalFolderStockProvider
from .pexels import PexelsStockProvider
from .pixabay import PixabayStockProvider

__all__ = [
    "StockClip",
    "StockProvider",
    "LocalFolderStockProvider",
    "PexelsStockProvider",
    "PixabayStockProvider",
    "build_stock_provider",
]


def build_stock_provider(
    *,
    pexels_api_key: str = "",
    pixabay_api_key: str = "",
    local_dir: str = "",
    prefer: str = "",
    logger: Optional[Any] = None,
) -> Optional[StockProvider]:
    """Rakit provider stok; prioritas: STOCK_PREFER -> pexels -> pixabay -> lokal.

    Mengembalikan None bila tidak ada yang dikonfigurasi (status not_configured).
    """
    candidates: list[StockProvider] = []
    if pexels_api_key:
        candidates.append(PexelsStockProvider(pexels_api_key))
    if pixabay_api_key:
        candidates.append(PixabayStockProvider(pixabay_api_key))
    if local_dir:
        candidates.append(LocalFolderStockProvider(local_dir))

    if prefer:
        preferred = prefer.strip().lower()
        candidates.sort(key=lambda provider: 0 if provider.name == preferred else 1)

    if not candidates:
        return None
    return candidates[0]
