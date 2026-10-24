"""Экстрактор CVH — плеер cdnvideohub (родной для animego, видео на VK/OK CDN, до 1080p+).

Поток (реверс, июль/авг 2026):
  1. embed animego: //animego.me/cdn-iframe/{titleId}/{voiceStudio}/{season}/{episode}
  2. GET https://plapi.cdnvideohub.com/api/v1/player/sv/playlist?pub={pub}&aggr={aggr}&id={titleId}
     (заголовки Origin/Referer = player.cdnvideohub.com; ОБЯЗАТЕЛЕН параметр aggr,
     иначе 503). Ответ: {titleName, isSerial, items:[{vkId, voiceStudio, season, episode}...]}.
     pub/aggr для animego константны (747 / mali) — берём из embed-страницы, если доступна,
     иначе дефолты.
  3. Находим item по (episode, voiceStudio) → vkId.
  4. GET .../player/sv/video/{vkId} → {sources:{hlsUrl, mpeg*Url...}}. hlsUrl = HLS master
     на okcdn.ru (VK/OK), подписанный и короткоживущий. Качаем через yt-dlp по высоте.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlparse

from ..http import UA, new_client
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor

log = logging.getLogger(__name__)

_API = "https://plapi.cdnvideohub.com/api/v1"
_DEFAULT_PUB = "747"
_DEFAULT_AGGR = "mali"
_PATH_RE = re.compile(r"/cdn-iframe/(\d+)/([^/]+)/(\d+)/(\d+)")
# высоты mpeg*-полей ответа video (fallback, если нет hls master)
_MPEG_HEIGHTS = {
    "mpegLowUrl": 240, "mpegMediumUrl": 480, "mpegHighUrl": 720,
    "mpegFullHdUrl": 1080, "mpegQhdUrl": 1440, "mpeg2kUrl": 1440, "mpeg4kUrl": 2160,
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class CVHExtractor(PlayerExtractor):
    name = "CVH"

    @staticmethod
    def _norm_url(url: str) -> str:
        return "https:" + url if url.startswith("//") else url

    def can_handle(self, embed_url: str) -> bool:
        u = self._norm_url(embed_url)
        return "/cdn-iframe/" in urlparse(u).path or "cdnvideohub.com" in (urlparse(u).hostname or "")

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = self._norm_url(embed_url)
        parsed = urlparse(url)
        m = _PATH_RE.search(parsed.path)
        if not m:
            raise ExtractError(f"CVH: не разобрал embed: {parsed.path}")
        title_id, studio, season, episode = m.group(1), unquote(m.group(2)), m.group(3), m.group(4)
        pub, aggr = _DEFAULT_PUB, _DEFAULT_AGGR

        api_headers = {
            "Referer": "https://player.cdnvideohub.com/",
            "Origin": "https://player.cdnvideohub.com",
            "Accept": "application/json", "User-Agent": UA,
        }
        async with new_client() as client:
            # best-effort: уточнить pub/aggr/title из embed-страницы animego (если доступна)
            try:
                rp = await client.get(url, headers={"Referer": "https://animego.me/"})
                if rp.status_code == 200:
                    for attr, var in (("data-publisher-id", "pub"), ("data-aggregator", "aggr"),
                                      ("data-title-id", "title")):
                        mm = re.search(rf'{attr}="([^"]+)"', rp.text)
                        if mm:
                            if var == "pub":
                                pub = mm.group(1)
                            elif var == "aggr":
                                aggr = mm.group(1)
                            else:
                                title_id = mm.group(1)
            except Exception:  # noqa: BLE001
                pass  # animego может быть недоступен — работаем с дефолтами

            pl = await client.get(f"{_API}/player/sv/playlist",
                                  params={"pub": pub, "aggr": aggr, "id": title_id},
                                  headers=api_headers)
            if pl.status_code != 200:
                raise ExtractError(f"CVH: playlist вернул {pl.status_code}")
            items = (pl.json() or {}).get("items") or []
            if not items:
                raise ExtractError("CVH: пустой playlist")

            same_ep = [it for it in items if str(it.get("episode")) == str(episode)]
            pool = same_ep or items
            it = next((x for x in pool if _norm(x.get("voiceStudio")) == _norm(studio)), None)
            if it is None and same_ep:
                it = same_ep[0]  # эпизод есть, озвучка не совпала по названию — берём что есть
            if it is None:
                raise ExtractError(f"CVH: не нашёл серию {episode} озвучки «{studio}»")
            vk_id = it.get("vkId")
            if not vk_id:
                raise ExtractError("CVH: нет vkId")

            vd = await client.get(f"{_API}/player/sv/video/{vk_id}", headers=api_headers)
            if vd.status_code != 200:
                raise ExtractError(f"CVH: video вернул {vd.status_code}")
            sources = (vd.json() or {}).get("sources") or {}

        hls = sources.get("hlsUrl")
        variants: list[VideoVariant] = []
        if hls:
            try:
                async with new_client() as c2:
                    rm = await c2.get(hls, headers={"User-Agent": UA})
                    heights = sorted({int(h) for _, h in
                                      re.findall(r"RESOLUTION=(\d+)x(\d+)", rm.text)}, reverse=True)
            except Exception:  # noqa: BLE001
                heights = []
            for h in heights:
                variants.append(VideoVariant(quality=h, url=hls, kind="ytdlp",
                                             headers={"User-Agent": UA}))
        if not variants:
            # fallback: прямые mpeg*-ссылки по качествам
            for field, h in sorted(_MPEG_HEIGHTS.items(), key=lambda kv: kv[1], reverse=True):
                u = sources.get(field)
                if u:
                    variants.append(VideoVariant(quality=h, url=u, kind="mp4",
                                                 headers={"User-Agent": UA}))
            # выкидываем дубли высот
            seen: set[int] = set()
            variants = [v for v in variants if not (v.quality in seen or seen.add(v.quality))]

        if not variants:
            raise ExtractError("CVH: не удалось получить ссылки (нет hls/mpeg источников)")
        variants.sort(key=lambda v: v.quality, reverse=True)
        log.info("CVH: title=%s ep=%s studio=%s -> %s", title_id, episode, studio,
                 [v.quality for v in variants])
        return variants
