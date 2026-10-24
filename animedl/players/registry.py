"""Выбор экстрактора под embed-url. Специализированные раньше, yt-dlp — последним."""
from __future__ import annotations

import logging

from ..models import PlayerButton, VideoVariant
from .alloha import AllohaExtractor
from .aniboom import AniBoomExtractor
from .base import ExtractError, PlayerExtractor
from .cvh import CVHExtractor
from .generic_ytdlp import GenericYtDlpExtractor
from .kodik import KodikExtractor
from .plplayer import PLPlayerExtractor

log = logging.getLogger(__name__)

# Порядок важен: специализированные экстракторы идут первыми.
_SPECIALIZED: list[PlayerExtractor] = [
    AllohaExtractor(),
    KodikExtractor(),
    AniBoomExtractor(),
    CVHExtractor(),
    PLPlayerExtractor(),
]
_FALLBACK = GenericYtDlpExtractor()


async def extract_variants(button: PlayerButton) -> list[VideoVariant]:
    """Достать варианты видео для нажатой кнопки (озвучка×плеер)."""
    url = button.url
    for ex in _SPECIALIZED:
        if ex.can_handle(url):
            return await ex.extract(url)
    # запасной путь — yt-dlp
    return await _FALLBACK.extract(url)


def provider_supported(button: PlayerButton) -> bool:
    """Есть ли специализированный экстрактор (иначе будет попытка через yt-dlp)."""
    return any(ex.can_handle(button.url) for ex in _SPECIALIZED)
