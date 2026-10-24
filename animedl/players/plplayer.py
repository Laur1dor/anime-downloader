"""PLPlayer: read the public JSON configuration and its HLS master playlist."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from ..http import UA, new_client
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor


def player_data(html: str) -> dict:
    match = re.search(r"window\.playerData\s*=\s*", html)
    if not match:
        raise ExtractError("PLPlayer: отсутствует конфигурация видео.")
    try:
        data = json.JSONDecoder().raw_decode(html[match.end():])[0]
    except ValueError as exc:
        raise ExtractError("PLPlayer: повреждена конфигурация видео.") from exc
    if not isinstance(data, dict):
        raise ExtractError("PLPlayer: некорректная конфигурация видео.")
    return data


class PLPlayerExtractor(PlayerExtractor):
    name = "PLPlayer"

    def can_handle(self, embed_url: str) -> bool:
        parsed = urlparse("https:" + embed_url if embed_url.startswith("//") else embed_url)
        return parsed.hostname == "plplayer.online" and bool(re.fullmatch(r"/s/\d+/?", parsed.path))

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = "https:" + embed_url if embed_url.startswith("//") else embed_url
        requested = parse_qs(urlparse(url).query)
        headers = {"User-Agent": UA, "Referer": "https://plplayer.online/", "Origin": "https://plplayer.online"}
        async with new_client(trust_env=False) as client:
            response = await client.get(url, headers={"Referer": "https://watch-the-sopranos-online.org/"})
            if response.status_code != 200:
                raise ExtractError(f"PLPlayer: плеер вернул {response.status_code}.")
            data = player_data(response.text)
            current = ((data.get("playlist") or {}).get("serial") or {}).get("current") or {}
            for query, field in [("season", "season"), ("episode", "episode"), ("voice", "voiceId")]:
                if requested.get(query) and str(current.get(field)) != requested[query][0]:
                    raise ExtractError("PLPlayer: плеер вернул другую серию, сезон или озвучку.")
            master = (data.get("config") or {}).get("video", "")
            if not isinstance(master, str) or urlparse(master).scheme not in ("http", "https"):
                raise ExtractError("PLPlayer: нет ссылки на видео.")
            playlist = await client.get(master, headers=headers)
            if playlist.status_code != 200 or not playlist.text.lstrip().startswith("#EXTM3U"):
                raise ExtractError("PLPlayer: недоступен HLS-плейлист.")
            heights = sorted({int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", playlist.text)}, reverse=True)
        if not heights:
            raise ExtractError("PLPlayer: в плейлисте не найдены варианты качества.")
        return [VideoVariant(quality=height, url=master, kind="ytdlp", headers=headers, direct=True) for height in heights]
