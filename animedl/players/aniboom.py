"""Экстрактор AniBoom — родной плеер animego, даёт до 1080p.

Поток (проверено вживую, июль 2026):
  1. GET embed https://aniboom.one/embed/{code}?episode=N&translation=T&parent=...
     с Referer домена-родителя (animego). В HTML есть атрибут data-parameters —
     HTML-escaped JSON.
  2. В нём вложенные JSON-строки "hls" и "dash". hls.src = master.m3u8 на CDN
     (poppy.ya-ligh.com / boom-img и т.п.).
  3. master.m3u8 — обычный HLS master: варианты 360/480/720/1080 + отдельная
     аудиодорожка. Скачиваем через yt-dlp (он сам выбирает нужную высоту и муксит
     видео+аудио), передавая Referer/Origin = aniboom.one.
"""
from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

from ..http import UA, new_client
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor

log = logging.getLogger(__name__)

_HOSTS = ("aniboom.one", "aniboom.tv", "aniboom.ru")
_PARAMS_RE = re.compile(r'data-parameters="([^"]+)"')


class AniBoomExtractor(PlayerExtractor):
    name = "AniBoom"

    @staticmethod
    def _norm(url: str) -> str:
        return "https:" + url if url.startswith("//") else url

    def can_handle(self, embed_url: str) -> bool:
        host = urlparse(self._norm(embed_url)).hostname or ""
        return any(host == h or host.endswith("." + h) for h in _HOSTS)

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = self._norm(embed_url)
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.hostname}"

        # Referer — домен-родитель (animego), берём из ?parent=, иначе аниме-домен.
        parent = parse_qs(parsed.query).get("parent", [""])[0]
        referer = parent or "https://animego.me/"

        async with new_client(headers={"Referer": referer}) as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise ExtractError(f"AniBoom: embed вернул {r.status_code}")
            m = _PARAMS_RE.search(r.text)
            if not m:
                raise ExtractError("AniBoom: не нашёл data-parameters на embed-странице")
            params = json.loads(html.unescape(m.group(1)))
            hls_field = params.get("hls")
            if not hls_field:
                raise ExtractError("AniBoom: нет hls-ссылки (возможно, только dash)")
            master = json.loads(hls_field).get("src")
            if not master:
                raise ExtractError("AniBoom: пустой hls.src")

            cdn_headers = {"Referer": origin + "/", "Origin": origin, "User-Agent": UA}
            rm = await client.get(master, headers=cdn_headers)
            if rm.status_code != 200:
                raise ExtractError(f"AniBoom: master.m3u8 вернул {rm.status_code}")
            heights = sorted({int(h) for _, h in
                              re.findall(r"RESOLUTION=(\d+)x(\d+)", rm.text)}, reverse=True)

        if not heights:
            # редко: master без явных RESOLUTION — отдаём одну «ytdlp»-запись
            heights = [int(params.get("qualityVideo") or 0)]

        variants = [
            VideoVariant(quality=h, url=master, kind="ytdlp",
                         headers={"Referer": origin + "/", "User-Agent": UA})
            for h in heights if h
        ]
        if not variants:
            raise ExtractError("AniBoom: не удалось определить качества")
        log.info("AniBoom: %s -> качества %s", params.get("id"), [v.quality for v in variants])
        return variants
