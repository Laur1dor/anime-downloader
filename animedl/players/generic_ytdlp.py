"""Универсальный экстрактор через yt-dlp.

Fallback для плееров, которые yt-dlp понимает из коробки (Sibnet, VK, OK.ru, mail.ru
и др.) и для всего, что не покрыто отдельными экстракторами (AniBoom, CVH — с
переменным успехом). Пробуем extract_info без скачивания и собираем доступные высоты.
"""
from __future__ import annotations

import asyncio
import logging

from ..http import UA
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor

log = logging.getLogger(__name__)


def _probe(url: str) -> dict:
    import yt_dlp  # локальный импорт: тяжёлая зависимость
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": {"User-Agent": UA},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


class GenericYtDlpExtractor(PlayerExtractor):
    name = "yt-dlp"

    def can_handle(self, embed_url: str) -> bool:
        # Пробуем последним — пусть отдельные экстракторы имеют приоритет.
        return True

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = "https:" + embed_url if embed_url.startswith("//") else embed_url
        try:
            info = await asyncio.to_thread(_probe, url)
        except Exception as e:  # noqa: BLE001
            raise ExtractError(f"yt-dlp не смог обработать {url}: {e}") from e

        formats = info.get("formats") or []
        # группируем по высоте, берём для каждой высоты один формат
        by_height: dict[int, str] = {}
        for f in formats:
            h = f.get("height")
            if not h:
                continue
            # предпочитаем формат со звуком, иначе любой
            fid = f.get("format_id", "")
            by_height.setdefault(int(h), fid)

        variants: list[VideoVariant] = []
        for h, fid in by_height.items():
            variants.append(VideoVariant(
                quality=int(h), url=url, kind="ytdlp", format_id=fid,
                headers={"User-Agent": UA},
            ))
        if not variants:
            # некоторые источники отдают единый прогрессивный url без height
            if info.get("url"):
                variants.append(VideoVariant(
                    quality=int(info.get("height") or 0), url=url, kind="ytdlp",
                    headers={"User-Agent": UA},
                ))
        variants.sort(key=lambda v: v.quality, reverse=True)
        if not variants:
            raise ExtractError("yt-dlp: форматы не найдены")
        log.info("yt-dlp: %s -> качества %s", url, [v.quality for v in variants])
        return variants
